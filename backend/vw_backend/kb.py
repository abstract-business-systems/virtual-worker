"""SQLite-backed knowledge base with team/channel ACL enforcement.

All access control is enforced HERE, on both write and read. The
filesystem permissions are only a second layer. Fail closed: an empty
allowed-team set yields no results.
"""
import os
import re
import sqlite3
import threading
import time

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY, name TEXT UNIQUE);
CREATE TABLE IF NOT EXISTS teams(
  id INTEGER PRIMARY KEY, name TEXT UNIQUE);
CREATE TABLE IF NOT EXISTS memberships(
  user_id INT, team_id INT, UNIQUE(user_id, team_id));
CREATE TABLE IF NOT EXISTS meetings(
  id INTEGER PRIMARY KEY, platform TEXT, join_url TEXT,
  team_id INT, started_at REAL, active INT DEFAULT 1);
CREATE TABLE IF NOT EXISTS transcript_segments(
  id INTEGER PRIMARY KEY, meeting_id INT, ts REAL, speaker TEXT, text TEXT);
CREATE VIRTUAL TABLE IF NOT EXISTS transcript_fts USING fts5(
  text, content='transcript_segments', content_rowid='id');
CREATE TABLE IF NOT EXISTS kb_entries(
  id INTEGER PRIMARY KEY, text TEXT, team_id INT, channel_id TEXT,
  source_meeting INT, created_at REAL);
CREATE VIRTUAL TABLE IF NOT EXISTS kb_fts USING fts5(
  text, content='kb_entries', content_rowid='id');
CREATE TABLE IF NOT EXISTS summaries(
  meeting_id INT PRIMARY KEY, text TEXT, updated_at REAL);
CREATE TABLE IF NOT EXISTS reminders(
  id INTEGER PRIMARY KEY, who TEXT, what TEXT, due_utc REAL,
  requester_tz TEXT, dedup_key TEXT UNIQUE, fired_at REAL, created_at REAL);
CREATE TABLE IF NOT EXISTS qa_log(
  id INTEGER PRIMARY KEY, meeting_id INT, requester TEXT, question TEXT,
  answered_at REAL);
CREATE INDEX IF NOT EXISTS idx_segments_meeting
  ON transcript_segments(meeting_id, ts);
CREATE TRIGGER IF NOT EXISTS seg_ai AFTER INSERT ON transcript_segments BEGIN
  INSERT INTO transcript_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS kb_ai AFTER INSERT ON kb_entries BEGIN
  INSERT INTO kb_fts(rowid, text) VALUES (new.id, new.text);
END;
"""

_FTS_QUERY_RE = re.compile(r'[^\w\s]')


def fts_escape(text):
    """Make free text safe for an FTS5 MATCH query (plain AND of tokens)."""
    tokens = _FTS_QUERY_RE.sub(" ", text).split()
    return " ".join('"%s"' % t for t in tokens if t)


class KnowledgeBase:
    def __init__(self, db_path):
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self.db_path = db_path
        self.lock = threading.RLock()
        with self._conn() as c:
            c.executescript(_SCHEMA)

    def _conn(self):
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    # ---- users / teams / ACL -------------------------------------------------
    def ensure_user(self, name):
        with self.lock, self._conn() as c:
            row = c.execute("SELECT id FROM users WHERE name=?", (name,)).fetchone()
            if row:
                return row["id"]
            cur = c.execute("INSERT INTO users(name) VALUES(?)", (name,))
            return cur.lastrowid

    def ensure_team(self, name):
        with self.lock, self._conn() as c:
            row = c.execute("SELECT id FROM teams WHERE name=?", (name,)).fetchone()
            if row:
                return row["id"]
            cur = c.execute("INSERT INTO teams(name) VALUES(?)", (name,))
            return cur.lastrowid

    def add_membership(self, user_name, team_name):
        uid = self.ensure_user(user_name)
        tid = self.ensure_team(team_name)
        with self.lock, self._conn() as c:
            c.execute(
                "INSERT OR IGNORE INTO memberships(user_id, team_id) VALUES(?,?)",
                (uid, tid),
            )
        return uid, tid

    def allowed_team_ids(self, user_name):
        """Set of team ids the user may read. Empty set => no access."""
        with self.lock, self._conn() as c:
            rows = c.execute(
                "SELECT m.team_id FROM memberships m JOIN users u ON u.id=m.user_id"
                " WHERE u.name=?",
                (user_name,),
            ).fetchall()
        return {r["team_id"] for r in rows}

    def user_exists(self, name):
        with self.lock, self._conn() as c:
            return c.execute("SELECT 1 FROM users WHERE name=?", (name,)).fetchone() is not None

    def ensure_member_of_team(self, user_name, team_id):
        """Auto-provision an in-meeting participant into the meeting's team."""
        uid = self.ensure_user(user_name)
        with self.lock, self._conn() as c:
            c.execute(
                "INSERT OR IGNORE INTO memberships(user_id, team_id) VALUES(?,?)",
                (uid, team_id),
            )
        return uid

    def team_name(self, team_id):
        with self.lock, self._conn() as c:
            row = c.execute("SELECT name FROM teams WHERE id=?", (team_id,)).fetchone()
            return row["name"] if row else None

    # ---- meetings ------------------------------------------------------------
    def start_meeting(self, platform, join_url, team_name):
        tid = self.ensure_team(team_name)
        with self.lock, self._conn() as c:
            c.execute("UPDATE meetings SET active=0 WHERE active=1")
            cur = c.execute(
                "INSERT INTO meetings(platform, join_url, team_id, started_at, active)"
                " VALUES(?,?,?,?,1)",
                (platform, join_url, tid, time.time()),
            )
            return cur.lastrowid, tid

    def active_meeting(self):
        with self.lock, self._conn() as c:
            return c.execute(
                "SELECT * FROM meetings WHERE active=1 ORDER BY id DESC LIMIT 1"
            ).fetchone()

    def end_meeting(self, meeting_id):
        with self.lock, self._conn() as c:
            c.execute("UPDATE meetings SET active=0 WHERE id=?", (meeting_id,))

    # ---- transcript / memory ---------------------------------------------------
    def append_transcript(self, meeting_id, ts, speaker, text):
        with self.lock, self._conn() as c:
            cur = c.execute(
                "INSERT INTO transcript_segments(meeting_id, ts, speaker, text)"
                " VALUES(?,?,?,?)",
                (meeting_id, ts, speaker, text),
            )
            return cur.lastrowid

    def recent_transcript(self, meeting_id, limit=40):
        with self.lock, self._conn() as c:
            rows = c.execute(
                "SELECT ts, speaker, text FROM transcript_segments"
                " WHERE meeting_id=? ORDER BY id DESC LIMIT ?",
                (meeting_id, limit),
            ).fetchall()
        return [dict(r) for r in reversed(rows)]

    def save_summary(self, meeting_id, text):
        with self.lock, self._conn() as c:
            c.execute(
                "INSERT INTO summaries(meeting_id, text, updated_at) VALUES(?,?,?)"
                " ON CONFLICT(meeting_id) DO UPDATE SET text=excluded.text,"
                " updated_at=excluded.updated_at",
                (meeting_id, text, time.time()),
            )

    def meeting_has_segments_since(self, meeting_id, since_seg_id):
        with self.lock, self._conn() as c:
            row = c.execute(
                "SELECT COUNT(*) AS n FROM transcript_segments"
                " WHERE meeting_id=? AND id>?",
                (meeting_id, since_seg_id),
            ).fetchone()
            return row["n"] > 0

    def last_segment_id(self, meeting_id):
        with self.lock, self._conn() as c:
            row = c.execute(
                "SELECT MAX(id) AS m FROM transcript_segments WHERE meeting_id=?",
                (meeting_id,),
            ).fetchone()
            return row["m"] or 0

    # ---- knowledge base (ACL enforced) ----------------------------------------
    def kb_put(self, text, team_id, channel_id=None, source_meeting=None):
        if not team_id:
            raise ValueError("kb_put requires a team_id — refusing to store unowned data")
        with self.lock, self._conn() as c:
            cur = c.execute(
                "INSERT INTO kb_entries(text, team_id, channel_id, source_meeting,"
                " created_at) VALUES(?,?,?,?,?)",
                (text, team_id, channel_id, source_meeting, time.time()),
            )
            return cur.lastrowid

    def _team_filter(self, allowed_team_ids):
        if not allowed_team_ids:
            return " AND 0", []  # fail closed
        placeholders = ",".join("?" for _ in allowed_team_ids)
        return " AND team_id IN (%s)" % placeholders, list(allowed_team_ids)

    @staticmethod
    def _fts_queries(query):
        """AND-of-tokens first (precise), then OR-of-tokens (recall)."""
        toks = ['"%s"' % t for t in
                _FTS_QUERY_RE.sub(" ", query).split() if t]
        if not toks:
            return []
        return [" ".join(toks), " OR ".join(toks)]

    def kb_search(self, query, allowed_team_ids, limit=6):
        filt, params = self._team_filter(allowed_team_ids)
        rows = []
        with self.lock, self._conn() as c:
            for match in self._fts_queries(query):
                try:
                    rows = c.execute(
                        "SELECT e.id, e.text, e.created_at, e.team_id"
                        " FROM kb_fts f JOIN kb_entries e ON e.id=f.rowid"
                        " WHERE kb_fts MATCH ?"
                        + filt
                        + " ORDER BY rank LIMIT ?",
                        [match] + params + [limit],
                    ).fetchall()
                except sqlite3.OperationalError:
                    rows = []
                if rows:
                    break
        return [dict(r) for r in rows]

    def segments_search(self, query, meeting_id, allowed_team_ids, limit=6):
        """Search past transcript segments, ACL-checked via the meeting's team."""
        filt, params = self._team_filter(allowed_team_ids)
        rows = []
        with self.lock, self._conn() as c:
            for match in self._fts_queries(query):
              try:
                rows = c.execute(
                    "SELECT s.id, s.ts, s.speaker, s.text, s.meeting_id"
                    " FROM transcript_fts f JOIN transcript_segments s ON s.id=f.rowid"
                    " JOIN meetings m ON m.id=s.meeting_id"
                    " WHERE transcript_fts MATCH ? AND s.meeting_id != ?"
                    + filt.replace("team_id", "m.team_id")
                    + " ORDER BY rank LIMIT ?",
                    [match, meeting_id or -1] + params + [limit],
                  ).fetchall()
              except sqlite3.OperationalError:
                rows = []
        return [dict(r) for r in rows]

    # ---- reminders -------------------------------------------------------------
    def create_reminder(self, who, what, due_utc, requester_tz, dedup_key):
        with self.lock, self._conn() as c:
            cur = c.execute(
                "INSERT INTO reminders(who, what, due_utc, requester_tz, dedup_key,"
                " created_at) VALUES(?,?,?,?,?,?)"
                " ON CONFLICT(dedup_key) DO NOTHING",
                (who, what, due_utc, requester_tz, dedup_key, time.time()),
            )
            return cur.lastrowid if cur.rowcount else None

    def due_reminders(self, now=None):
        now = now or time.time()
        with self.lock, self._conn() as c:
            rows = c.execute(
                "SELECT * FROM reminders WHERE fired_at IS NULL AND due_utc <= ?",
                (now,),
            ).fetchall()
        return [dict(r) for r in rows]

    def mark_fired(self, reminder_id):
        """Mark fired BEFORE dispatch so a reminder is announced at most once."""
        with self.lock, self._conn() as c:
            c.execute(
                "UPDATE reminders SET fired_at=? WHERE id=? AND fired_at IS NULL",
                (time.time(), reminder_id),
            )

    def list_reminders(self, only_pending=True):
        with self.lock, self._conn() as c:
            sql = "SELECT * FROM reminders"
            if only_pending:
                sql += " WHERE fired_at IS NULL"
            rows = c.execute(sql + " ORDER BY due_utc").fetchall()
        return [dict(r) for r in rows]

    # ---- misc ------------------------------------------------------------------
    def log_qa(self, meeting_id, requester, question):
        with self.lock, self._conn() as c:
            c.execute(
                "INSERT INTO qa_log(meeting_id, requester, question, answered_at)"
                " VALUES(?,?,?,?)",
                (meeting_id, requester, question, time.time()),
            )

    def stats(self):
        with self.lock, self._conn() as c:
            def count(table):
                return c.execute("SELECT COUNT(*) AS n FROM %s" % table).fetchone()["n"]
            return {
                "users": count("users"),
                "teams": count("teams"),
                "meetings": count("meetings"),
                "transcript_segments": count("transcript_segments"),
                "kb_entries": count("kb_entries"),
                "reminders_pending": c.execute(
                    "SELECT COUNT(*) AS n FROM reminders WHERE fired_at IS NULL"
                ).fetchone()["n"],
            }