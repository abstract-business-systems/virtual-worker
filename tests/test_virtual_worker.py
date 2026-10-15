"""Unit tests for the pieces that must never be wrong: ACL enforcement,
reminder deduplication, reminder parsing, and the socket protocol."""
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "common"))
sys.path.insert(0, os.path.join(REPO, "backend"))
sys.path.insert(0, os.path.join(REPO, "agent"))

from vw_backend.kb import KnowledgeBase, fts_escape          # noqa: E402
from vw_backend.server import Backend                        # noqa: E402
from vw_common import send_msg, recv_msg                     # noqa: E402
from vw_agent import parse as parse_mod                      # noqa: E402


class TestKbAcl(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.kb = KnowledgeBase(os.path.join(self.tmp, "kb.db"))

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_fail_closed_on_unknown_user(self):
        self.assertEqual(self.kb.allowed_team_ids("ghost"), set())
        self.assertEqual(self.kb.kb_search("anything", set()), [])

    def test_member_reads_own_team_only(self):
        self.kb.ensure_team("Product")
        self.kb.ensure_team("Sales")
        self.kb.add_membership("alice", "Product")
        pid = self.kb.ensure_team("Product")
        sid = self.kb.ensure_team("Sales")
        self.kb.kb_put("launch on October 20", pid)
        self.kb.kb_put("sales pipeline review", sid)
        allowed = self.kb.allowed_team_ids("alice")
        hits = self.kb.kb_search("launch october", allowed)
        self.assertEqual(len(hits), 1)
        self.assertIn("launch", hits[0]["text"])
        # no cross-team leak
        self.assertEqual(self.kb.kb_search("pipeline", allowed), [])

    def test_kb_put_refuses_unowned_data(self):
        with self.assertRaises(ValueError):
            self.kb.kb_put("orphan data", None)

    def test_reminder_fires_once(self):
        rid = self.kb.create_reminder("bob", "prepare report",
                                      time.time() - 1, "UTC", "k1")
        self.assertTrue(rid)
        due = self.kb.due_reminders()
        self.assertEqual(len(due), 1)
        self.kb.mark_fired(due[0]["id"])
        self.assertEqual(self.kb.due_reminders(), [])
        # creating with the same dedup key does not duplicate
        rid2 = self.kb.create_reminder("bob", "prepare report",
                                       time.time() + 60, "UTC", "k1")
        self.assertIsNone(rid2)

    def test_segments_search_respects_acl(self):
        self.kb.ensure_team("Product")
        self.kb.add_membership("alice", "Product")
        mid1, _ = self.kb.start_meeting("meet", "url1", "Product")
        self.kb.append_transcript(mid1, time.time(), "bob", "we chose option B")
        # a later meeting becomes current; the past one is searchable
        mid2, _ = self.kb.start_meeting("meet", "url2", "Product")
        allowed = self.kb.allowed_team_ids("alice")
        self.assertEqual(len(self.kb.segments_search("option", mid2, allowed)), 1)
        self.assertEqual(self.kb.segments_search("option", mid2, set()), [])
        # the current meeting is excluded (its transcript is live context)
        self.assertEqual(self.kb.segments_search("option", mid1, allowed), [])

    def test_meeting_team_isolation(self):
        tid = self.kb.ensure_team("Product")
        mid, team = self.kb.start_meeting("meet", "u", "Product")
        self.assertEqual(team, tid)
        meeting = self.kb.active_meeting()
        self.assertEqual(meeting["id"], mid)


class TestReminderParsing(unittest.TestCase):
    def test_in_minutes(self):
        r = parse_mod.parse_reminder(
            "Alex, remind me in 20 minutes to prepare the report", "UTC")
        self.assertIsNotNone(r)
        self.assertEqual(r["who"], None)
        self.assertIn("prepare the report", r["what"])
        self.assertAlmostEqual(r["due_utc"] - time.time(), 1200, delta=5)

    def test_tomorrow_at(self):
        r = parse_mod.parse_reminder(
            "remind me tomorrow at 10:00 that I need to prepare the report",
            "UTC")
        self.assertIsNotNone(r)
        local = time.strftime("%H:%M", time.gmtime(r["due_utc"]))
        self.assertEqual(local, "10:00")
        self.assertIn("prepare the report", r["what"])

    def test_no_match(self):
        self.assertIsNone(parse_mod.parse_reminder(
            "what did we decide about the budget?", "UTC"))

    def test_named_target(self):
        r = parse_mod.parse_reminder("remind bob at 09:30 about the review",
                                     "UTC")
        self.assertEqual(r["who"], "bob")


class TestBackendProtocol(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.sock_path = os.path.join(self.tmp, "agent.sock")
        os.environ["VW_DB"] = os.path.join(self.tmp, "kb.db")
        # keep the brain offline in tests
        self._brain = Backend.__init__
        self.be = Backend(socket_path=self.sock_path)
        self.be.brain = _StubBrain()
        self.be.start()

    def tearDown(self):
        self.be.stop()
        shutil.rmtree(self.tmp)

    def _cli(self):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(15)
        s.connect(self.sock_path)
        return s, bytearray()

    def _rpc(self, s, buf, msg):
        send_msg(s, {"type": "hello", "role": "cli"})
        send_msg(s, msg)
        return recv_msg(s, buf)

    def test_ping_and_acls(self):
        s, buf = self._cli()
        try:
            r = self._rpc(s, buf, {"type": "ping", "req_id": "1"})
            self.assertTrue(r["ok"])
            r = self._rpc(s, buf, {"type": "question", "req_id": "2",
                                   "requester": "stranger",
                                   "text": "what did we agree?"})
            self.assertTrue(r["ok"])
            self.assertFalse(r["data"]["allowed"])
        finally:
            s.close()

    def test_question_with_meeting_membership(self):
        s, buf = self._cli()
        try:
            self._rpc(s, buf, {"type": "meeting.start", "req_id": "0",
                               "platform": "meet", "url": "u", "team": "Prod"})
            # CLI requesters must already exist in the ACL store
            self._rpc(s, buf, {"type": "user.ensure", "req_id": "00",
                               "name": "carol", "teams": ["Prod"]})
            # member asks -> allowed
            r = self._rpc(s, buf, {"type": "question", "req_id": "1",
                                   "requester": "carol",
                                   "text": "anything about the launch?"})
            self.assertTrue(r["data"]["allowed"])
            # a non-member stays locked out (fail closed)
            r = self._rpc(s, buf, {"type": "question", "req_id": "2",
                                   "requester": "mallory",
                                   "text": "anything about the launch?"})
            self.assertFalse(r["data"]["allowed"])
            # in-meeting participant (agent-sent) IS auto-provisioned
            r = self._rpc(s, buf, {"type": "question", "req_id": "3",
                                   "requester": "dana", "in_meeting": True,
                                   "text": "anything about the launch?"})
            self.assertTrue(r["data"]["allowed"])
        finally:
            s.close()

    def test_unknown_type_rejected(self):
        s, buf = self._cli()
        try:
            r = self._rpc(s, buf, {"type": "nope", "req_id": "3"})
            self.assertFalse(r["ok"])
        finally:
            s.close()


class _StubBrain:
    def compose_answer(self, q, recent, kb_hits, seg_hits):
        return "stub answer"

    def summarize(self, lines):
        return "stub summary"


class TestProtocolFraming(unittest.TestCase):
    def test_roundtrip(self):
        import tempfile as tf
        path = os.path.join(tf.mkdtemp(), "s.sock")
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(path)
        srv.listen(1)
        client_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client_sock.connect(path)
        conn, _ = srv.accept()
        send_msg(client_sock, {"type": "x", "n": 42})
        buf = bytearray()
        msg = recv_msg(conn, buf)
        self.assertEqual(msg["n"], 42)


if __name__ == "__main__":
    unittest.main(verbosity=2)