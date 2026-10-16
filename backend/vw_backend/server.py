"""Backend hub: Unix socket server + brain + KB + reminder scheduler.

Topology (implementation note): the backend OWNS the socket at
/run/virtual-worker/agent.sock. The agent connects and stays connected so the
backend can push `speak` / `reminder.due` / `meeting.join` to it. Operator
CLIs connect, send one request, get one result.

Access control lives entirely in kb.py (fail-closed).
"""
import os
import socket
import threading
import time
import traceback

from vw_common import send_msg, recv_msg
from .brain import Brain
from .kb import KnowledgeBase
from .scheduler import ReminderScheduler

from vw_common import default_socket_path

SOCKET_PATH = default_socket_path()


class AgentLink:
    """The connected agent (0 or 1 at a time in the PoC)."""

    def __init__(self):
        self.sock = None
        self.buf = bytearray()
        self.send_lock = threading.Lock()
        self.pending = {}  # req_id -> threading.Event / result box
        self.pending_lock = threading.Lock()

    def send(self, msg):
        with self.send_lock:
            send_msg(self.sock, msg)

    def forward_request(self, msg, timeout=180):
        """Send a command to the agent and wait for its result."""
        req_id = msg.get("req_id")
        ev = threading.Event()
        box = {}
        with self.pending_lock:
            self.pending[req_id] = (ev, box)
        try:
            self.send(msg)
            if not ev.wait(timeout):
                raise TimeoutError("agent did not answer in %ss" % timeout)
            if not box.get("ok"):
                raise RuntimeError(box.get("error", "agent error"))
            return box.get("data", {})
        finally:
            with self.pending_lock:
                self.pending.pop(req_id, None)


class Backend:
    def __init__(self, socket_path=SOCKET_PATH):
        self.socket_path = socket_path
        self.kb = KnowledgeBase(
            os.environ.get("VW_DB", "/var/lib/virtual-worker/kb.db"))
        self.brain = Brain()
        self.agent = AgentLink()
        self.scheduler = ReminderScheduler(self.kb, self._push_to_agent)
        self.server_sock = None
        self._summary_lock = threading.Lock()
        self._stop = threading.Event()

    # ---- lifecycle -------------------------------------------------------------
    def start(self):
        d = os.path.dirname(self.socket_path)
        os.makedirs(d, exist_ok=True)
        if os.path.exists(self.socket_path):
            os.unlink(self.socket_path)
        self.server_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server_sock.bind(self.socket_path)
        os.chmod(self.socket_path, 0o660)
        os.chmod(d, 0o750)
        self.server_sock.listen(8)
        threading.Thread(target=self._accept_loop, daemon=True,
                         name="socket-accept").start()
        self.scheduler.start()
        threading.Thread(target=self._summary_loop, daemon=True,
                         name="summary-loop").start()
        print("[backend] listening on %s" % self.socket_path, flush=True)

    def stop(self):
        self._stop.set()
        self.scheduler.stop()

    # ---- connections -----------------------------------------------------------
    def _accept_loop(self):
        while not self._stop.is_set():
            try:
                conn, _ = self.server_sock.accept()
            except OSError:
                break
            threading.Thread(target=self._client_loop, args=(conn,),
                             daemon=True).start()

    def _client_loop(self, conn):
        buf = bytearray()
        role = None
        try:
            while not self._stop.is_set():
                msg = recv_msg(conn, buf)
                if msg is None:
                    break
                if msg.get("type") == "hello":
                    role = msg.get("role", "cli")
                    print("[backend] %s connected" % role, flush=True)
                    if role == "agent":
                        self.agent.sock = conn
                        self.agent.buf = buf
                    continue
                if role == "agent":
                    self._handle_agent_msg(conn, msg)
                else:
                    self._handle_cli_msg(conn, msg)
        except (ConnectionError, OSError):
            pass
        except Exception:
            traceback.print_exc()
        finally:
            if self.agent.sock is conn:
                print("[backend] agent disconnected", flush=True)
                self.agent.sock = None
            try:
                conn.close()
            except OSError:
                pass

    def _handle_agent_msg(self, conn, msg):
        t = msg.get("type")
        if t == "result":
            with self.agent.pending_lock:
                entry = self.agent.pending.pop(msg.get("req_id"), None)
            if entry:
                ev, box = entry
                box.update(ok=msg.get("ok", True),
                           data=msg.get("data", {}),
                           error=msg.get("error"))
                ev.set()
            return
        # agent-sent request (ping, question, transcript.append, ...) — handle
        # in a worker thread so slow brain calls never block the push stream
        req_id = msg.get("req_id")

        def run():
            try:
                data = self.dispatch(msg)
                reply = {"type": "result", "req_id": req_id, "ok": True,
                         "data": data}
            except Exception as exc:
                reply = {"type": "result", "req_id": req_id, "ok": False,
                         "error": str(exc)}
            try:
                with self.agent.send_lock:
                    send_msg(conn, reply)
            except OSError:
                pass
        threading.Thread(target=run, daemon=True).start()

    def _push_to_agent(self, msg):
        if self.agent.sock is None:
            return False
        try:
            self.agent.send(msg)
            return True
        except OSError:
            return False

    # ---- CLI handlers ----------------------------------------------------------
    def _handle_cli_msg(self, conn, msg):
        req_id = msg.get("req_id")
        try:
            data = self.dispatch(msg)
            send_msg(conn, {"type": "result", "req_id": req_id, "ok": True,
                            "data": data})
        except Exception as exc:
            send_msg(conn, {"type": "result", "req_id": req_id, "ok": False,
                            "error": str(exc)})

    def dispatch(self, msg):
        t = msg["type"]
        h = getattr(self, "h_" + t.replace(".", "_"), None)
        if h is None:
            raise ValueError("unknown request type: %s" % t)
        return h(msg)

    # ---- operator commands -------------------------------------------------------
    def h_ping(self, msg):
        return {"pong": True, "stats": self.kb.stats()}

    def h_team_ensure(self, msg):
        tid = self.kb.ensure_team(msg["name"])
        return {"team_id": tid}

    def h_user_ensure(self, msg):
        uid = self.kb.ensure_user(msg["name"])
        for team in msg.get("teams", []):
            self.kb.add_membership(msg["name"], team)
        return {"user_id": uid,
                "teams": sorted(self.kb.allowed_team_ids(msg["name"]))}

    def h_meeting_start(self, msg):
        mid, tid = self.kb.start_meeting(msg.get("platform", "meet"),
                                         msg.get("url", ""), msg["team"])
        return {"meeting_id": mid, "team_id": tid,
                "team": self.kb.team_name(tid)}

    def h_meeting_end(self, msg):
        meeting = self.kb.active_meeting()
        mid = msg.get("meeting_id") or (meeting["id"] if meeting else None)
        if mid:
            self.kb.end_meeting(mid)
            self.summarize_meeting(mid)
        return {"ended": mid}

    def h_meeting_join(self, msg):
        if self.agent.sock is None:
            raise RuntimeError("agent is not connected")
        fwd = {"type": "meeting.join", "url": msg["url"],
               "req_id": "fwd-%s" % msg.get("req_id", "x")}
        return self.agent.forward_request(fwd, timeout=180)

    def h_agent_status(self, msg):
        if self.agent.sock is None:
            return {"connected": False}
        fwd = {"type": "status", "req_id": "fwd-%s" % msg.get("req_id", "x")}
        data = self.agent.forward_request(fwd, timeout=20)
        data["connected"] = True
        return data

    def h_agent_say(self, msg):
        return self._push_or_forward({"type": "speak", "text": msg["text"],
                                      "once_key": msg.get("once_key")})

    def _push_or_forward(self, fwd):
        """speak: fire-and-forget push (returns dispatched bool)."""
        if self.agent.sock is None:
            raise RuntimeError("agent is not connected")
        ok = self._push_to_agent(fwd)
        return {"dispatched": ok}

    def h_agent_demo(self, msg):
        if self.agent.sock is None:
            raise RuntimeError("agent is not connected")
        fwd = {"type": "demo", "req_id": "fwd-%s" % msg.get("req_id", "x")}
        return self.agent.forward_request(fwd, timeout=240)

    def h_agent_selftest(self, msg):
        if self.agent.sock is None:
            raise RuntimeError("agent is not connected")
        fwd = {"type": "selftest", "req_id": "fwd-%s" % msg.get("req_id", "x")}
        return self.agent.forward_request(fwd, timeout=300)

    # ---- agent-sent data ----------------------------------------------------------
    def h_transcript_append(self, msg):
        meeting = self.kb.active_meeting()
        mid = msg.get("meeting_id") or (meeting["id"] if meeting else None)
        if mid is None:
            raise RuntimeError("no active meeting")
        seg_id = self.kb.append_transcript(mid, msg.get("ts", time.time()),
                                           msg.get("speaker", "unknown"),
                                           msg["text"])
        return {"segment_id": seg_id, "meeting_id": mid}

    def h_action_run(self, msg):
        if self.agent.sock is None:
            raise RuntimeError("agent is not connected")
        fwd = {"type": "action", "name": msg["name"],
               "url": msg.get("url"),
               "req_id": "fwd-%s" % msg.get("req_id", "x")}
        return self.agent.forward_request(fwd, timeout=180)

    def h_meeting_url(self, msg):
        meeting = self.kb.active_meeting()
        if not meeting:
            return {"url": None}
        return {"url": meeting["join_url"] or None}

    def h_context_get(self, msg):
        """Assembled, ACL-checked context for the streaming answer path:
        recent transcript + relevant past segments + KB hits for the question."""
        requester = msg.get("requester", "unknown")
        question = msg.get("question", "")
        meeting = self.kb.active_meeting()
        meeting_id = msg.get("meeting_id") or (meeting["id"] if meeting else None)
        meeting_team = meeting["team_id"] if meeting else None
        if meeting_team is not None and msg.get("in_meeting"):
            self.kb.ensure_member_of_team(requester, meeting_team)
        allowed = self.kb.allowed_team_ids(requester)
        if not allowed:
            return {"transcript": [], "kb_hits": [], "segment_hits": [],
                    "allowed": False}
        summaries = []
        if meeting_id:
            with self.kb.lock, self.kb._conn() as c:
                row = c.execute("SELECT text FROM summaries WHERE meeting_id=?",
                                (meeting_id,)).fetchone()
            if row and row["text"]:
                summaries.append(row["text"])
        return {
            "transcript": self.kb.recent_transcript(meeting_id) if meeting_id else [],
            "summaries": summaries,
            "kb_hits": self.kb.kb_search(question, allowed),
            "segment_hits": (self.kb.segments_search(question, meeting_id, allowed)
                             if meeting_id else []),
            "allowed": True,
        }

    def h_transcript_get(self, msg):
        meeting = self.kb.active_meeting()
        mid = msg.get("meeting_id") or (meeting["id"] if meeting else None)
        if mid is None:
            return {"transcript": []}
        return {"transcript": self.kb.recent_transcript(mid, limit=30)}

    def h_reminder_create(self, msg):
        rid = self.kb.create_reminder(
            msg["who"], msg["what"], msg["due_utc"],
            msg.get("requester_tz", "UTC"), msg["dedup_key"])
        return {"reminder_id": rid,
                "created": rid is not None,
                "due_utc": msg["due_utc"]}

    def h_reminders_list(self, msg):
        return {"reminders": self.kb.list_reminders(msg.get("pending", True))}

    def h_kb_put(self, msg):
        meeting = self.kb.active_meeting()
        mid = msg.get("meeting_id") or (meeting["id"] if meeting else None)
        team_id = None
        if mid is not None:
            with self.kb.lock, self.kb._conn() as c:
                row = c.execute("SELECT team_id FROM meetings WHERE id=?",
                                (mid,)).fetchone()
            team_id = row["team_id"] if row else None
        if team_id is None:
            raise ValueError("kb_put requires an active meeting team")
        kid = self.kb.kb_put(msg["text"], team_id,
                             source_meeting=mid)
        return {"kb_id": kid, "team_id": team_id}

    def h_kb_search(self, msg):
        requester = msg["requester"]
        allowed = self.kb.allowed_team_ids(requester)
        hits = self.kb.kb_search(msg["query"], allowed)
        return {"hits": hits, "allowed": bool(allowed)}

    # ---- the question flow (requirements 2, 3, 7) ------------------------------------
    def h_brain_act(self, msg):
        requester = msg.get("requester", "operator")
        text = msg["text"]
        meeting = self.kb.active_meeting()
        meeting_id = meeting["id"] if meeting else None
        meeting_team = meeting["team_id"] if meeting else None
        # Same auto-provisioning as h_question: an in-meeting speaker is a
        # member of the meeting's team (the host admitted them).
        if meeting_team is not None and msg.get("in_meeting"):
            self.kb.ensure_member_of_team(requester, meeting_team)
        allowed = self.kb.allowed_team_ids(requester)
        ctx = []
        if meeting_id:
            recent = self.kb.recent_transcript(meeting_id, limit=12)
            if recent:
                ctx.append("Recent meeting transcript:")
                ctx.extend("%s: %s" % (r["speaker"], r["text"])
                           for r in recent)
        # NOTE: no memory pre-injection - Luna must recall only when the
        # user actually asks about past discussions or stored facts.
        return self.brain.act(text, agent=self.agent, kb=self.kb,
                              requester=requester,
                              context="\n".join(ctx))

    def h_question(self, msg):
        requester = msg.get("requester", "unknown")
        text = msg["text"]
        meeting = self.kb.active_meeting()
        meeting_id = msg.get("meeting_id") or (meeting["id"] if meeting else None)
        meeting_team = meeting["team_id"] if meeting else None

        # Only speakers inside the meeting (sent by the agent, which the host
        # admitted) are auto-provisioned into the meeting's team. External
        # requesters (operator CLI) must exist in the ACL store already.
        if meeting_team is not None and msg.get("in_meeting"):
            self.kb.ensure_member_of_team(requester, meeting_team)

        allowed = self.kb.allowed_team_ids(requester)
        if not allowed:
            return {"answer": "I am sorry, I do not have access to any "
                              "information for you.", "allowed": False}

        recent = self.kb.recent_transcript(meeting_id) if meeting_id else []
        kb_hits = self.kb.kb_search(text, allowed)
        seg_hits = (self.kb.segments_search(text, meeting_id, allowed)
                    if meeting_id else [])
        answer = self.brain.compose_answer(text, recent, kb_hits, seg_hits)
        self.kb.log_qa(meeting_id, requester, text)
        return {"answer": answer, "allowed": True, "requester": requester,
                "sources": {"kb": len(kb_hits), "past_segments": len(seg_hits)}}

    # ---- periodic summarization (requirement 3: memory without being asked) -----------
    def _summary_loop(self):
        last_seg = {}
        while not self._stop.is_set():
            self._stop.wait(300)
            if self._stop.is_set():
                break
            try:
                meeting = self.kb.active_meeting()
                if not meeting:
                    continue
                mid = meeting["id"]
                if self.kb.meeting_has_segments_since(mid, last_seg.get(mid, 0)):
                    self.summarize_meeting(mid)
                    last_seg[mid] = self.kb.last_segment_id(mid)
            except Exception:
                traceback.print_exc()

    def summarize_meeting(self, meeting_id):
        with self._summary_lock:
            segments = self.kb.recent_transcript(meeting_id, limit=200)
            if not segments:
                return None
            lines = ["%s: %s" % (s.get("speaker", "?"), s.get("text", ""))
                     for s in segments]
            try:
                summary = self.brain.summarize(lines)
            except Exception as exc:
                print("[backend] summarize failed: %s" % exc, flush=True)
                return None
            meeting = self.kb.active_meeting()
            team_id = meeting["team_id"] if meeting and meeting["id"] == meeting_id else None
            if team_id is None:
                with self.kb.lock, self.kb._conn() as c:
                    row = c.execute("SELECT team_id FROM meetings WHERE id=?",
                                    (meeting_id,)).fetchone()
                team_id = row["team_id"] if row else None
            self.kb.save_summary(meeting_id, summary)
            if team_id:
                self.kb.kb_put("Meeting summary: " + summary, team_id,
                               source_meeting=meeting_id)
            print("[backend] meeting %d summarized (%d segments)"
                  % (meeting_id, len(segments)), flush=True)
            return summary


def main():
    backend = Backend()
    backend.start()
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        backend.stop()


if __name__ == "__main__":
    main()