"""Reminder scheduler: polls for due reminders and pushes them to the agent.

A reminder is marked fired BEFORE dispatch (at-most-once) so the same
reminder is never announced twice (requirement 5).
"""
import threading
import time
import traceback


class ReminderScheduler(threading.Thread):
    def __init__(self, kb, push_fn, interval=2.0):
        super().__init__(daemon=True, name="reminder-scheduler")
        self.kb = kb
        self.push_fn = push_fn  # fn(msg_dict) -> bool (False if agent offline)
        self.interval = interval
        self._stop = threading.Event()

    def run(self):
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                traceback.print_exc()
            self._stop.wait(self.interval)

    def stop(self):
        self._stop.set()

    def tick(self):
        for rem in self.kb.due_reminders():
            # mark first => guaranteed once
            self.kb.mark_fired(rem["id"])
            msg = {
                "type": "reminder.due",
                "id": rem["id"],
                "who": rem["who"],
                "what": rem["what"],
                "due_utc": rem["due_utc"],
                "requester_tz": rem["requester_tz"],
            }
            ok = self.push_fn(msg)
            print("[scheduler] reminder #%d due (%s: %s) dispatched=%s"
                  % (rem["id"], rem["who"], rem["what"], ok), flush=True)