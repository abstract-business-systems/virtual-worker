"""Virtual worker agent: joins a meeting, listens, answers, remembers.

Threads:
  - AgentClient reader  : persistent backend link; dispatches pushes
                          (speak / reminder.due / meeting.join / ...)
  - audio loop          : parec -> endpointing -> STT -> transcript + wake flow
  - screen loop         : 5s screenshot diff -> OCR text -> transcript (req 6)

The PoC demo (vw demo) plays scripted utterances through the meeting bus with
espeak so the full STT -> wake -> brain -> TTS loop runs end-to-end without a
real Google account. The real flow uses the same path once joined via Meet.
"""
import os
import queue
import tempfile
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor

from vw_common import send_msg, recv_msg

from . import audio as audio_mod
from . import browser as browser_mod
from . import parse as parse_mod
from . import screen as screen_mod
from . import speak as speak_mod
from . import stt as stt_mod
from .config import CONFIG


class AgentClient:
    """Persistent connection to the backend with push handling."""

    def __init__(self, socket_path, push_handler):
        self.socket_path = socket_path
        self.push_handler = push_handler
        self.sock = None
        self.buf = bytearray()
        self.send_lock = threading.Lock()
        self.pending = {}
        self.pending_lock = threading.Lock()
        self._counter = 0
        self._queue = queue.Queue()
        self._pool = ThreadPoolExecutor(max_workers=3,
                                        thread_name_prefix="push")
        self.connected = threading.Event()

    def start(self):
        threading.Thread(target=self._loop, daemon=True,
                         name="backend-link").start()
        threading.Thread(target=self._handler_loop, daemon=True,
                         name="push-handler").start()
        for _ in range(120):
            if self.connected.is_set():
                return True
            time.sleep(0.5)
        return False

    def _loop(self):
        buf = bytearray()
        while True:
            try:
                if self.sock is None:
                    import socket as _s
                    self.sock = _s.socket(_s.AF_UNIX, _s.SOCK_STREAM)
                    self.sock.connect(self.socket_path)
                    buf.clear()
                    send_msg(self.sock, {"type": "hello", "role": "agent"})
                    self.connected.set()
                    print("[agent] connected to backend", flush=True)
                msg = recv_msg(self.sock, buf)
                if msg is None:
                    raise ConnectionError("backend closed")
                if msg.get("type") == "result":
                    self._dispatch(msg)   # resolve inline, never queue results
                else:
                    self._queue.put(msg)  # pushes run on the handler thread
            except Exception:
                self.connected.clear()
                if self.sock:
                    try:
                        self.sock.close()
                    except OSError:
                        pass
                self.sock = None
                time.sleep(2)

    def _dispatch(self, msg):
        if msg.get("type") == "result":
            with self.pending_lock:
                entry = self.pending.pop(msg.get("req_id"), None)
            if entry:
                ev, box = entry
                box.update(msg)
                ev.set()

    def _handler_loop(self):
        """Push messages run on a small pool (NOT the reader thread) so slow
        handlers (TTS, selftest) never block other pushes or the link."""
        while True:
            msg = self._queue.get()
            self._pool.submit(self._safe_handle, msg)

    def _safe_handle(self, msg):
        try:
            self.push_handler(msg)
        except Exception:
            traceback.print_exc()

    def send_push(self, msg):
        """Send an arbitrary message to the backend (thread-safe)."""
        with self.send_lock:
            send_msg(self.sock, msg)

    def request(self, msg, timeout=120):
        req_id = "a%d" % (self._next_id(),)
        msg = dict(msg)
        msg["req_id"] = req_id
        ev = threading.Event()
        box = {}
        with self.pending_lock:
            self.pending[req_id] = (ev, box)
        with self.send_lock:
            send_msg(self.sock, msg)
        if not ev.wait(timeout):
            with self.pending_lock:
                self.pending.pop(req_id, None)
            raise TimeoutError("backend did not answer %s" % msg["type"])
        if not box.get("ok"):
            raise RuntimeError(box.get("error", "backend error"))
        return box.get("data", {})

    def _next_id(self):
        with self.pending_lock:
            self._counter += 1
            return self._counter


class Worker:
    def __init__(self):
        self.client = AgentClient(CONFIG["socket_path"], self.on_push)
        self.browser = browser_mod.Browser()
        # the worker carries its own brain: it looks at the screen and decides
        from vw_backend.brain import Brain
        self.brain = Brain()
        self.segmenter = None
        self.screen_watcher = None
        self.parec = None
        self._lock = threading.Lock()
        self.transcript_count = 0
        self.spoken = []            # every text the worker said aloud
        self.joined_url = None
        self.meeting_id = None
        self._last_wake_ts = 0.0
        self._armed_until = 0.0

    # ---- lifecycle -------------------------------------------------------------
    def start(self):
        buses = audio_mod.ensure_pulse_setup()
        print("[agent] audio buses: %s" % buses, flush=True)
        screen_mod.ensure_xvfb()
        if not self.client.start():
            raise RuntimeError("cannot reach backend at %s"
                               % CONFIG["socket_path"])
        self.segmenter = stt_mod.Segmenter()
        threading.Thread(target=self._audio_loop, daemon=True,
                         name="audio-loop").start()
        self.screen_watcher = screen_mod.ScreenWatcher(self._on_screen_change)
        threading.Thread(target=self.screen_watcher.run, daemon=True,
                         name="screen-loop").start()
        # warm the STT model so the first question does not stall
        threading.Thread(target=stt_mod.get_model, daemon=True).start()
        print("[agent] running (wake name: %r)" % CONFIG["wake_name"], flush=True)

    # ---- push handlers -----------------------------------------------------------
    def on_push(self, msg):
        t = msg.get("type")
        if t == "speak":
            self.say(msg["text"], once_key=msg.get("once_key"))
        elif t == "reminder.due":
            self.say("Reminder for %s: %s" % (msg["who"], msg["what"]),
                     once_key="reminder-%s" % msg["id"])
        elif t == "meeting.join":
            data = {}
            try:
                data = self.join_meeting(msg["url"])
            except Exception as exc:
                data = {"error": str(exc)}
            self.client.send_push({
                "type": "result", "req_id": msg.get("req_id"),
                "ok": "error" not in data, "data": data})
        elif t == "status":
            self.client.send_push({
                "type": "result", "req_id": msg.get("req_id"), "ok": True,
                "data": self.status()})
        elif t == "selftest":
            data = {}
            try:
                data = self.selftest()
            except Exception as exc:
                data = {"error": str(exc), "report": {}}
            self.client.send_push({
                "type": "result", "req_id": msg.get("req_id"),
                "ok": "error" not in data, "data": data})
        elif t == "demo":
            data = {}
            try:
                data = self.demo()
            except Exception as exc:
                traceback.print_exc()
                data = {"error": str(exc)}
            self.client.send_push({
                "type": "result", "req_id": msg.get("req_id"),
                "ok": "error" not in data, "data": data})

    # ---- meeting join (requirement 1): vision-driven ---------------------------
    def join_meeting(self, url):
        if url.startswith("demo://"):
            # scripted demo meeting: no real browser navigation
            self.joined_url = url
        else:
            print("[agent] joining meeting: %s" % url, flush=True)
            try:
                result = self._vision_join(url)
            except Exception as exc:
                # no vision brain available -> fall back to scripted flow
                print("[agent] vision join unavailable (%s); scripted fallback"
                      % exc, flush=True)
                result = self.browser.join(url)
            self.joined_url = url
        # register the meeting on the backend so memory is tied to a team
        # (always re-register: a previous demo meeting must not own this one)
        try:
            team = CONFIG.get("team") or "Product"
            data = self.client.request({"type": "meeting.start",
                                        "platform": "meet", "url": url,
                                        "team": team})
            self.meeting_id = data.get("meeting_id")
        except Exception as exc:
            print("[agent] meeting.start failed: %s" % exc, flush=True)
        return {"joined": self.joined_url}

    def _vision_join(self, url, max_steps=18):
        """Look at the screen, decide, act, repeat - until in the meeting."""
        b = self.browser
        b.goto(url)
        actions = []
        for step in range(max_steps):
            time.sleep(2.5)                     # let the page settle
            png = b.shot_png()
            decision = self.brain.decide_action(
                png, b.page.url, b.page.title(), actions)
            action = decision.get("action", "wait")
            reason = decision.get("reason", "")
            print("[agent] vision step %d: %s - %s" % (step, action, reason),
                  flush=True)
            actions.append("%s: %s" % (action, reason)[:140])
            if action == "done":
                return {"joined": url, "via": "vision", "reason": reason}
            if action == "give_up":
                return {"error": "brain gave up: %s" % reason}
            if action in ("click", "fill", "key", "wait"):
                try:
                    b.act(decision)
                except Exception as exc:
                    print("[agent] act failed: %s" % exc, flush=True)
        return {"error": "join did not complete within %d vision steps" % max_steps}

    # ---- audio loop (requirements 2, 3) ----------------------------------------------
    def _audio_loop(self):
        buses = audio_mod.ensure_pulse_setup()
        proc = audio_mod.start_recording(buses["sink_monitor"],
                                         CONFIG["stt_sample_rate"])
        print("[agent] recording meeting audio from %s" % buses["sink_monitor"],
              flush=True)
        chunk = int(0.2 * CONFIG["stt_sample_rate"]) * 2  # 0.2s of s16le
        while True:
            pcm = proc.stdout.read(chunk)
            if not pcm:
                time.sleep(0.5)
                proc = audio_mod.start_recording(
                    buses["sink_monitor"], CONFIG["stt_sample_rate"])
                continue
            try:
                seg = self.segmenter.feed_pcm(pcm)
            except Exception:
                traceback.print_exc()
                continue
            if seg:
                text, dur = seg
                self._handle_segment(text, dur)

    def _handle_segment(self, text, dur):
        text = text.strip()
        print("[agent] heard: %r (%.1fs)" % (text, dur), flush=True)
        self._append_transcript(speaker=CONFIG["operator_name"], text=text)
        low = text.lower()
        now = time.time()
        if self._contains_wake(low):
            question = self._strip_wake(text).strip(" ,.?!")
            if sum(c.isalpha() for c in question) >= 3:
                self._armed_until = 0.0
                self._answer_question(question)
            else:
                # bare wake name: arm for the question that follows
                self._armed_until = now + 25
                self._last_wake_ts = now
                print("[agent] wake name alone - waiting for the question",
                      flush=True)
            return
        if now < self._armed_until and sum(c.isalpha() for c in text) >= 6:
            self._armed_until = 0.0
            self._answer_question(text)
            return
        if now - self._last_wake_ts < 60 and (
                "?" in text or low.startswith(("what", "can you", "tell me",
                                               "how", "why", "when", "who",
                                               "do you", "did you", "repeat",
                                               "say again", "say that again"))):
            # follow-up question shortly after addressing Alex
            self._answer_question(text)
            return
        if "remind" in low or (now - self._last_wake_ts < 12 and
                               parse_mod.parse_reminder(text,
                                       default_tz=CONFIG["default_tz"])):
            self._maybe_reminder(text)

    _ALPHA_RE = None

    def _strip_wake(self, text):
        """Remove the wake name (and fuzzy variants) from the start/middle."""
        import re
        wake = CONFIG["wake_name"].lower()
        pat = re.compile(r"\b[\w'\-]{0,3}%s[\w'\-]{0,3}\b" % re.escape(wake),
                         re.I)
        return pat.sub("", text)

    def _contains_wake(self, low):
        """Exact substring or fuzzy token match, so STT mis-hearings of the
        wake name ("a lex", "alice", "awlex", "lx", ...) still trigger."""
        wake = CONFIG["wake_name"].lower()
        if wake in low:
            return True
        import difflib
        import string
        for word in low.split():
            word = word.strip(string.punctuation)
            if not word or abs(len(word) - len(wake)) > 2:
                continue
            if difflib.SequenceMatcher(None, word, wake).ratio() >= 0.65:
                return True
        return False

    def _append_transcript(self, speaker, text):
        try:
            self.client.request({"type": "transcript.append",
                                 "ts": time.time(), "speaker": speaker,
                                 "text": text}, timeout=30)
            self.transcript_count += 1
        except Exception as exc:
            print("[agent] transcript.append failed: %s" % exc, flush=True)

    def _answer_question(self, question):
        """Wake detected -> ask the brain -> speak the answer, sentence by
        sentence as they stream (first words within ~2s of generation)."""
        question = (question or "").strip()
        print("[agent] asking brain: %r" % question, flush=True)
        try:
            context = self.client.request({
                "type": "context.get",
                "question": question,
                "requester": CONFIG["operator_name"],
                "meeting_id": self.meeting_id,
                "in_meeting": self.joined_url is not None,
            }, timeout=30)
        except Exception as exc:
            print("[agent] context.get failed: %s" % exc, flush=True)
            context = {}
        try:
            deltas = self.brain.stream_answer(
                question, context.get("transcript", []),
                context.get("kb_hits", []), context.get("segment_hits", []),
                context.get("summaries", []))
        except Exception as exc:
            print("[agent] streaming failed (%s), fallback" % exc, flush=True)
            try:
                data = self.client.request({
                    "type": "question",
                    "requester": CONFIG["operator_name"],
                    "text": question,
                    "meeting_id": self.meeting_id,
                    "in_meeting": self.joined_url is not None,
                    "recent_transcript": []}, timeout=120)
                answer = data.get("answer", "Sorry, I could not process that.")
            except Exception as exc2:
                answer = "Sorry, I could not process that. (%s)" % exc2
            self.say(answer)
            self._maybe_reminder(question)
            return
        self._speak_stream(deltas, question)
        self._maybe_reminder(question)

    def _speak_stream(self, deltas, question):
        """Consume streamed answer text; synthesize and play sentence by
        sentence, pipelined so playback of sentence k overlaps synthesis of
        sentence k+1."""
        import re
        synth_q = queue.Queue(maxsize=2)
        DONE = object()

        def synth_worker():
            buf = ""
            full = []
            try:
                for delta in deltas:
                    buf += delta
                    full.append(delta)
                    while True:
                        m = re.search(r"[.!?](?:\s|$)", buf)
                        if not m:
                            break
                        sentence = buf[:m.end()].strip()
                        buf = buf[m.end():]
                        if sentence and sum(c.isalpha() for c in sentence) >= 2:
                            wav = tempfile.NamedTemporaryFile(
                                suffix=".wav", delete=False)
                            wav.close()
                            try:
                                speak_mod.tts_synth(sentence, wav.name)
                                synth_q.put(wav.name)
                            except Exception as exc:
                                print("[agent] synth failed: %s" % exc,
                                      flush=True)
                if buf.strip():
                    wav = tempfile.NamedTemporaryFile(suffix=".wav",
                                                      delete=False)
                    wav.close()
                    try:
                        speak_mod.tts_synth(buf.strip(), wav.name)
                        synth_q.put(wav.name)
                    except Exception as exc:
                        print("[agent] synth failed: %s" % exc, flush=True)
            finally:
                synth_q.put(DONE)
                print("[agent] answer: %s" % "".join(full), flush=True)

        threading.Thread(target=synth_worker, daemon=True,
                         name="answer-synth").start()
        with self._lock:
            try:
                self.browser.set_mic(unmute=True)
            except Exception:
                pass
            try:
                spoken_any = False
                while True:
                    item = synth_q.get()
                    if item is DONE:
                        break
                    try:
                        speak_mod.play_wav_file(item)
                        spoken_any = True
                        self.spoken.append(open(item, "rb").read(1) and
                                          "")  # keep bookkeeping cheap
                    finally:
                        try:
                            os.unlink(item)
                        except OSError:
                            pass
                if not spoken_any:
                    speak_mod.tts_play("Sorry, I could not answer that.")
            finally:
                try:
                    self.browser.set_mic(unmute=False)
                except Exception:
                    pass

    def _maybe_reminder(self, text):
        rem = parse_mod.parse_reminder(text, default_tz=CONFIG["default_tz"])
        if not rem:
            return False
        rem["who"] = rem.get("who") or CONFIG["operator_name"]
        rem["dedup_key"] = "r:%d:%s" % (int(rem["due_utc"]),
                                        rem["what"][:60].lower())
        try:
            data = self.client.request({"type": "reminder.create", **rem},
                                       timeout=30)
        except Exception as exc:
            print("[agent] reminder.create failed: %s" % exc, flush=True)
            return False
        if data.get("created"):
            when = time.strftime("%H:%M:%S UTC", time.gmtime(rem["due_utc"]))
            self.say("Okay %s, I will remind you at %s." % (rem["who"], when))
            print("[agent] reminder created: %s" % data, flush=True)
            return True
        return False

    # ---- speaking (requirement 2) ------------------------------------------------
    def say(self, text, once_key=None):
        if not text:
            return
        if once_key:
            if once_key in self.spoken:
                return  # never repeat the same announcement (requirement 5)
            self.spoken.append(once_key)
        with self._lock:
            print("[agent] speaking: %s" % text, flush=True)
            try:
                self.browser.set_mic(unmute=True)
            except Exception:
                pass
            try:
                speak_mod.tts_play(text)
            finally:
                try:
                    self.browser.set_mic(unmute=False)
                except Exception:
                    pass
            self.spoken.append(text)

    # ---- screen loop (requirement 6) ------------------------------------------------
    def _on_screen_change(self, text):
        print("[agent] screen changed: %r" % text[:120], flush=True)
        self._append_transcript(speaker="screen", text=text)

    # ---- status / selftest / demo ------------------------------------------------------
    def status(self):
        return {
            "wake_name": CONFIG["wake_name"],
            "joined_url": self.joined_url,
            "meeting_id": self.meeting_id,
            "transcript_segments": self.transcript_count,
            "spoken_texts": len(self.spoken),
            "audio_loop": self.parec_alive() if self.parec else "lazy",
            "screen_loop": bool(self.screen_watcher and
                                not self.screen_watcher._stop.is_set()),
        }

    def parec_alive(self):
        return self.parec is not None and self.parec.poll() is None

    def selftest(self):
        report = {}
        def check(name, fn):
            try:
                report[name] = "PASS: %s" % (fn(),)
            except Exception as exc:
                report[name] = "FAIL: %s" % exc
        check("xvfb-screenshot", lambda: (screen_mod.ScreenWatcher()
              .screenshot().size[0] > 0) and "screenshot ok")
        check("pulse-buses", lambda: ", ".join(
            sorted(audio_mod.ensure_pulse_setup().values())))
        check("stt-model", lambda: stt_mod.get_model() is not None and
              "model loaded")
        check("tts-play", lambda: (speak_mod.tts_play("selftest") or None) is None
              and "played into %s" % CONFIG["tts_sink"])
        check("browser-render", self._check_browser)
        check("backend-roundtrip", lambda: self.client.request(
            {"type": "ping"}, timeout=30).get("pong") and "pong")
        check("acl-fail-closed", lambda: self.client.request(
            {"type": "kb.search", "requester": "nobody-%d" % time.time(),
             "query": "launch"}, timeout=30)["allowed"] is False and
            "unknown user gets nothing")
        check("reminder-scheduling", self._check_reminder_roundtrip)
        return {"report": report}

    def _check_browser(self):
        b = browser_mod.Browser()
        try:
            b._ensure()
            w = screen_mod.ScreenWatcher()
            before = w.ahash(w.screenshot())
            b.page.goto("data:text/html,<h1>virtual worker</h1>")
            time.sleep(1)
            after = w.ahash(w.screenshot())
            if before == after:
                raise AssertionError("screen did not change")
            return "chromium renders on %s" % CONFIG["display"]
        finally:
            b.close()

    def _check_reminder_roundtrip(self):
        got = threading.Event()
        box = {}
        orig = self.on_push

        def spy(msg):
            if msg.get("type") == "reminder.due":
                box.update(msg)
                got.set()
            else:
                orig(msg)
        self.on_push = spy
        self.client.push_handler = spy
        try:
            data = self.client.request({
                "type": "reminder.create", "who": "selftest", "what": "ping",
                "due_utc": time.time() + 2, "requester_tz": "UTC",
                "dedup_key": "selftest-%f" % time.time()}, timeout=15)
            if not data.get("created"):
                raise AssertionError("reminder not created: %s" % data)
            if not got.wait(20):
                raise AssertionError("reminder.due push never arrived")
            return "scheduler fired reminder #%s" % box.get("id")
        finally:
            self.on_push = orig
            self.client.push_handler = orig

    def demo(self):
        """End-to-end scripted meeting played through the real audio pipeline."""
        if self.meeting_id is None:
            self.join_meeting("demo://local-meeting")   # registers team+meeting
        self._append_transcript(speaker="system", text="demo meeting started")
        script = [
            ("participant", "We agreed to launch the product on the twentieth "
             "of October, and Bob will prepare the report."),
        ]
        for speaker, line in script:
            self._play_participant(line)
        time.sleep(3)
        # 1) wake + question -> brain -> spoken answer
        self._play_participant("Alex, what did we agree about the launch?")
        time.sleep(15)
        # 2) reminder -> scheduler -> spoken announcement (20s)
        self._play_participant("Alex, remind me in 20 seconds to prepare the "
                               "report.")
        deadline = time.time() + 45
        while time.time() < deadline:
            if any("Reminder for" in s for s in self.spoken):
                break
            time.sleep(1)
        return {
            "transcript_segments": self.transcript_count,
            "spoken": list(self.spoken),
            "reminder_fired": any("Reminder for" in s for s in self.spoken),
        }

    def _play_participant(self, line):
        """A 'participant' speaks through the meeting bus (real STT hears it)."""
        print("[demo] participant says: %s" % line, flush=True)
        self._append_transcript(speaker="participant", text=line)
        import subprocess, tempfile, os as _os
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            wav = f.name
        try:
            subprocess.run(["espeak-ng", "-v", "en-us", "-s", "140", "-w", wav,
                            line], check=True, capture_output=True, timeout=60)
            subprocess.run(["paplay", "--device=%s" % CONFIG["sink"], wav],
                           check=True, capture_output=True, timeout=120)
        finally:
            _os.unlink(wav)


def main():
    worker = Worker()
    worker.start()
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()