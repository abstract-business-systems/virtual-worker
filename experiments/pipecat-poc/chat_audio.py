#!/usr/bin/env python3
"""pipecat-poc modes audio / separate — proven hand-built conversation layer
(the engine from ../audio-model-poc, moved in so pipecat-poc runs all three
mode types from one run.sh):

  mic (sounddevice, 100 ms blocks)
    -> Silero VAD (local ONNX) detects end-of-utterance
    -> audio   : whole utterance -> one OpenRouter audio-model call
                 (streams audio reply, tools, memory in one model)
    -> separate: local Whisper STT -> think text model (native tools)
                 -> voice model TTS  (each stage timed and costed)

No realtime API in these modes; HTTP per turn (survives network drops).
Requires OPENROUTER_API_KEY. Models come from models.ini sections
[audio] / [separate]; ./run.sh <mode> selects the preset.

Run: ./run.sh audio | ./run.sh separate   (Ctrl+C or terminal close exits)
"""

import asyncio
import base64
import configparser
import json
import os
import queue
import signal
import subprocess
import sys
import time
import wave
from pathlib import Path

import numpy as np
import sounddevice as sd

from loguru import logger
logger.remove()  # silence pipecat's banner/log noise before first import
from pipecat.audio.utils import calculate_audio_volume, create_stream_resampler
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams, VADState

HERE = Path(__file__).parent
from cli_ui import banner, dim, err, friday, hdr, stage, tool, warn as _warn_c, you as _you_c
import token_accounting as ta
IN_RATE = 16000          # VAD + whisper native rate
OUT_RATE = 44100         # typical playback device rate

# ---- mode preset (models.ini) — ./run.sh audio|separate picks one ----
_PRESETS = configparser.ConfigParser()
_PRESETS.read(HERE / "models.ini")
PRESET = os.environ.get("CHAT_PRESET", "audio")
if not _PRESETS.has_section(PRESET):
    raise SystemExit(f"[preset] '{PRESET}' not in models.ini — sections: "
                     f"{_PRESETS.sections()}")
VOICE_MODEL = (_PRESETS.get(PRESET, "voice").strip()
               or "openai/gpt-audio-mini")
THINK_MODEL = _PRESETS.get(PRESET, "think", fallback="").strip() or None
STT_CFG = _PRESETS.get(PRESET, "stt", fallback="").strip() or None
# STT_CFG "whisper:<size>" = local faster-whisper; "provider/model" = the
# OpenRouter voice model transcribes; empty = voice model transcribes.

# CHAT_MODEL (--model) overrides the voice model of the preset
MODEL = os.environ.get("CHAT_MODEL") or VOICE_MODEL
VOICE = os.environ.get("VW_POC_VOICE", "alloy")
API = "https://openrouter.ai/api/v1/chat/completions"
KEY = os.environ.get("OPENROUTER_API_KEY")
DEBUG = bool(os.getenv("CHAT_DEBUG"))

# chat.py (realtime) shares the same workdir convention via CHAT_WORKDIR
WORKDIR = os.environ.get("CHAT_WORKDIR") or os.getcwd()

SYSTEM_PROMPT = (
    "You are Friday, a virtual worker. Keep spoken answers brief (2-3 "
    "sentences). Your working directory is " + WORKDIR + " — use the bash "
    "tool to inspect and summarize the files there when asked. Call "
    "get_meeting_context when the question touches meetings, notes, or "
    "the calendar."
)
TOOLS = [{
    "type": "function",
    "function": {
        "name": "get_meeting_context",
        "description": "Look up recent meeting notes and calendar context for Friday.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}, {
    "type": "function",
    "function": {
        "name": "bash",
        "description": "Run a bash command in the working directory and "
                       "return its stdout/stderr. Use it to list, read, or "
                       "summarize files in the working directory.",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string",
                            "description": "The bash command to run"},
            },
            "required": ["command"],
        },
    },
}]


def _tool_get_meeting_context(args: dict):
    return {"recent_meeting": "M7 POC scoping", "next_event": "Friday demo review"}


def _tool_bash(args: dict):
    """Run one bash command in WORKDIR; return structured output."""
    cmd = str(args.get("command", ""))
    if not cmd.strip():
        return {"returncode": -1, "output": "(empty command)"}
    try:
        p = subprocess.run(["bash", "-c", cmd], cwd=WORKDIR,
                           capture_output=True, text=True, timeout=60)
        out = p.stdout or ""
        if p.stderr.strip():
            out += ("\n[stderr] " if out else "[stderr] ") + p.stderr
        return {"returncode": p.returncode,
                "output": (out.strip()[:4000] or "(no output)")}
    except subprocess.TimeoutExpired:
        return {"returncode": -1, "output": "(command timed out after 60s)"}
    except Exception as e:  # noqa: BLE001
        return {"returncode": -1, "output": f"(error: {e!r})"}


TOOL_IMPL = {
    "get_meeting_context": _tool_get_meeting_context,
    "bash": _tool_bash,
}


def run_tool(name: str, arguments: str):
    """Dispatch a tool call; returns the JSON-able result."""
    try:
        args = json.loads(arguments or "{}")
        if not isinstance(args, dict):
            args = {}
    except json.JSONDecodeError:
        args = {}
    impl = TOOL_IMPL.get(name)
    if impl is None:
        return {"error": f"unknown tool {name}"}
    return impl(args)

OPENAI_KEY = os.environ.get("OPENAI_API_KEY")
if not KEY:
    sys.exit("OPENROUTER_API_KEY not set (experiments/pipecat-poc/.env)")

turn_seq = 0  # bumped on barge-in; in-flight turn audio with an older token is dropped
# Echo gate: while Friday speaks, the mic (RDP: server audio -> client
# speakers -> client mic) picks up the playback. Frames during playback are
# never buffered as user speech; barge-in requires louder-than-bleed speech.
BARGE_RMS = int(os.environ.get("CHAT_BARGE_RMS", "2500"))

# ---- local Whisper STT (separate mode, stt = whisper:<size>) ----
_whisper = None


def openai_transcribe(pcm24: bytes, model: str = "gpt-4o-mini-transcribe") -> str:
    """Online STT via OpenAI /v1/audio/transcriptions (online model, as the
    separate mode intends; gpt-audio-mini itself proved an unreliable
    transcriber). pcm24: 24 kHz int16 PCM."""
    import io
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(24000)
        w.writeframes(pcm24)
    data = buf.getvalue()
    boundary = "----vwstt"
    body = (f'--{boundary}\r\nContent-Disposition: form-data; '
            f'name="file"; filename="utt.wav"\r\n'
            f'Content-Type: audio/wav\r\n\r\n').encode() + data + \
           (f'\r\n--{boundary}\r\nContent-Disposition: form-data; '
            f'name="model"\r\n\r\n{model}\r\n'
            f'--{boundary}--\r\n').encode()
    import urllib.request
    req = urllib.request.Request(
        "https://api.openai.com/v1/audio/transcriptions", data=body,
        headers={"Authorization": f"Bearer {OPENAI_KEY}",
                 "Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        body = json.load(resp)
    # gpt-4o-mini-transcribe returns usage: {type: tokens, input_tokens,
    # output_tokens, input_token_details{audio_tokens, text_tokens}} —
    # captured so STT cost is in the token report (was silently dropped)
    return body["text"].strip(), body.get("usage")


def openai_tts(text: str, voice: str) -> tuple:
    """Dedicated OpenAI TTS: gpt-4o-mini-tts via /v1/audio/speech.
    Proper TTS voices (coral/alloy/...) at ~$12/M audio tokens — far
    better voice quality than the gpt-audio-mini chat path, still cheap.
    Returns (pcm16 bytes @24 kHz, seconds of speech)."""
    import io as _io, wave as _wave, urllib.request as _ur
    body = json.dumps({"model": "gpt-4o-mini-tts", "voice": voice,
                       "input": text, "response_format": "wav"}).encode()
    req = _ur.Request(
        "https://api.openai.com/v1/audio/speech", data=body,
        headers={"Authorization": f"Bearer {OPENAI_KEY}",
                 "Content-Type": "application/json"})
    with _ur.urlopen(req, timeout=60) as r:
        wav_bytes = r.read()
    with _wave.open(_io.BytesIO(wav_bytes)) as w:
        sr = w.getframerate()
        pcm = w.readframes(w.getnframes())
    if sr != 24000:
        import audioop
        pcm, _ = audioop.ratecv(pcm, 2, 1, sr, 24000, None)
    return pcm, len(pcm) / 2 / 24000


def transcribe_utterance(pcm24: bytes, quiet: bool = False) -> tuple:
    """Dispatch on STT_CFG:
      whisper:<size>        local faster-whisper (offline fallback)
      openai[:<model>]      OpenAI transcription API (default;
                            gpt-4o-mini-transcribe)
      <provider/model>      the OpenRouter voice model transcribes
                            (audio in, text out)
    Empty STT_CFG = the voice model transcribes."""
    if STT_CFG and STT_CFG.startswith("openai"):
        model = STT_CFG.split(":", 1)[1] if ":" in STT_CFG else \
            "gpt-4o-mini-transcribe"
        return openai_transcribe(pcm24, model)
    # STT_CFG "provider/model" should never reach here (openrouter path);
    # whisper:* or None -> local faster-whisper
    return whisper_transcribe(pcm24, quiet), None


def whisper_transcribe(pcm24: bytes, quiet: bool = False) -> str:
    """24 kHz int16 PCM -> transcript text via local faster-whisper.
    Model is downloaded once (~75 MB for base) and cached.
    quiet=True skips the load/timing prints (display-only use)."""
    global _whisper
    t0 = time.perf_counter()
    if _whisper is None:
        from faster_whisper import WhisperModel
        size = (STT_CFG.split(":", 1)[1]
                if (STT_CFG and ":" in STT_CFG) else "base")
        if not quiet:
            print(stage(f"[stt] loading local whisper '{size}' (first run downloads it)..."), flush=True)
        _whisper = WhisperModel(size, device="cpu", compute_type="int8")
    # resample 24k int16 -> 16k float32 (whisper native rate)
    pcm16 = (np.frombuffer(pcm24, dtype=np.int16).astype(np.float32)
             / 32768.0)
    pcm16 = _resample_f32(pcm16, 24000, 16000)
    segments, _info = _whisper.transcribe(pcm16, language="en",
                                          vad_filter=True)
    text = " ".join(s.text for s in segments).strip()
    if not quiet:
        if DEBUG:
            print(stage(f"[stt] whisper ready {time.perf_counter() - t0:.2f}s (incl. load)"), flush=True)
    return text


def _resample_f32(x: np.ndarray, src: int, dst: int) -> np.ndarray:
    if src == dst:
        return x.astype(np.float32)
    # linear resample is fine for 24k -> 16k speech here
    n = int(len(x) * dst / src)
    return np.interp(
        np.linspace(0, len(x) - 1, n), np.arange(len(x)), x
    ).astype(np.float32)  # np.interp returns float64; whisper needs float32


class Speaker:
    """24 kHz model audio -> playback device rate (same callback pattern)."""

    def __init__(self):
        self.q: queue.Queue = queue.Queue()
        self._last_feed = 0.0   # monotonic ts of the last fed audio chunk
        self.stream = None
        self.resampler = None

    @property
    def playing(self):
        """True while reply audio is queued/recently fed.

        Derived, not stored: a plain flag is only ever cleared on barge-in,
        so after a natural playback end it stays True forever and every
        later turn commit is blocked ('listens but never replies again' —
        the pre-existing bug found in live testing). The 0.3 s grace covers
        the playback-device buffer draining after the last fed chunk."""
        return (not self.q.empty()) or (time.monotonic() - self._last_feed) < 0.3

    def _callback(self, outdata, frames, t, status):
        need = frames * 2
        chunks = []
        while need > 0:
            try:
                d = self.q.get_nowait()
            except queue.Empty:
                break
            d = d[: len(d) // 2 * 2]
            take = min(need, len(d))
            chunks.append(d[:take])
            need -= take
            if len(d) > take:
                self.q.queue.appendleft(d[take:])
        data = b"".join(chunks)
        outdata[:] = data.ljust(frames * 2, b"\x00")

    def start(self):
        self.resampler = create_stream_resampler()
        outdev = sd.query_devices(kind="output")
        rate = int(outdev["default_samplerate"])
        self.stream = sd.RawOutputStream(
            samplerate=rate, blocksize=1024, dtype="int16", channels=1,
            callback=self._callback)
        self.stream.start()
        self._rate = rate

    def stop(self):
        if self.stream:
            self.stream.stop()
            self.stream.close()

    def barge_in(self):
        while True:
            try:
                self.q.get_nowait()
            except queue.Empty:
                break
        self._last_feed = 0.0  # playback cut -> not playing immediately

    async def feed(self, pcm24: bytes):
        """Queue one chunk of 24 kHz model audio for playback."""
        audio = pcm24
        if self._rate != 24000:
            audio = await self.resampler.resample(pcm24, 24000, self._rate)
        if audio:
            self.q.put(audio)
            self._last_feed = time.monotonic()


def pcm_to_wav_b64(pcm24: bytes) -> str:
    """Wrap raw 24 kHz PCM16 in a real WAV container, base64-encoded.
    The provider rejects headerless PCM sent with format=wav (verified:
    'This model does not support the format you provided')."""
    import io
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(24000)
        w.writeframes(pcm24)
    return base64.b64encode(buf.getvalue()).decode()


speaker = Speaker()
loop: asyncio.AbstractEventLoop | None = None
mic_q: asyncio.Queue | None = None


def mic_callback(indata, frames, t, status):
    if loop is not None and mic_q is not None:
        loop.call_soon_threadsafe(mic_q.put_nowait, bytes(indata))


async def chat_stream(messages, on_audio, on_text, *, think=False, tts=False, stt=False):
    """Streaming OpenRouter chat completion on a worker thread (urllib is
    blocking). Returns (tool_calls, usage).

    think=True: the preset's THINK text model, tools enabled, text-only.
    tts=True:   the voice model speaks text (audio out only).
    stt=True:   the voice model transcribes (audio in, text out).
    Default:    the voice model, audio in/out, tools (single-call mode).
    """
    if think:
        model, modalities, tools = THINK_MODEL, ["text"], TOOLS
    elif tts:
        # OpenRouter rejects modalities:["audio"] — text+audio required
        model, modalities, tools = MODEL, ["text", "audio"], None
    elif stt:
        model, modalities, tools = MODEL, ["text"], None
    else:
        model, modalities, tools = MODEL, ["text", "audio"], TOOLS
    payload = {
        "model": model,
        "modalities": modalities,
        "stream": True,
        "messages": messages,
    }
    if not think and not stt:
        payload["audio"] = {"voice": VOICE, "format": "pcm16"}
    if tools:
        payload["tools"] = tools
    import urllib.request

    tool_calls: list[dict] = []
    usage = None

    def worker():
        nonlocal usage
        try:
            req = urllib.request.Request(
                API, data=json.dumps(payload).encode(),
                headers={"Authorization": f"Bearer {KEY}",
                         "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=180) as resp:
                parts_args: dict[str, list] = {}
                for raw in resp:
                    line = raw.decode(errors="replace").strip()
                    if not line.startswith("data:"):
                        continue
                    sdata = line[5:].strip()
                    if sdata == "[DONE]":
                        break
                    try:
                        ev = json.loads(sdata)
                    except json.JSONDecodeError:
                        continue
                    if ev.get("error"):
                        print(err(f"[api error] {json.dumps(ev['error'])[:300]}"), flush=True)
                    if ev.get("usage"):
                        usage = ev["usage"]
                    ch = (ev.get("choices") or [{}])[0]
                    delta = ch.get("delta") or {}
                    ad = (delta.get("audio") or {}).get("data")
                    if ad and on_audio:
                        loop.call_soon_threadsafe(on_audio, ad)
                    tr = (delta.get("audio") or {}).get("transcript")
                    if tr and on_text:
                        loop.call_soon_threadsafe(on_text, tr)
                    # text-only responses (stt/think/tts paths) come via content
                    c = delta.get("content")
                    if c and on_text:
                        loop.call_soon_threadsafe(on_text, c)
                    for i, tc in enumerate(delta.get("tool_calls") or []):
                        fn = tc.get("function") or {}
                        slot = parts_args.setdefault(
                            i, {"id": tc.get("id"), "name": None, "args": []})
                        if tc.get("id"):
                            slot["id"] = tc.get("id")
                        if fn.get("name"):
                            slot["name"] = fn["name"]
                        if fn.get("arguments"):
                            slot["args"].append(fn["arguments"])
                tool_calls.extend(
                    {"name": v["name"], "arguments": "".join(v["args"])}
                    for _, v in sorted(parts_args.items()))
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")[:300]
            print(err(f"[api error] HTTP {e.code}: {body}"), flush=True)
        except Exception as e:  # noqa: BLE001
            print(err(f"[error] stream failed: {e!r}"), flush=True)

    await asyncio.to_thread(worker)
    return tool_calls, usage


async def run():
    global loop, mic_q, turn_seq
    started_utc = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    loop = asyncio.get_running_loop()
    mic_q = asyncio.Queue(maxsize=400)
    speaker.start()
    # clean shutdown on Ctrl+C: break the main loop so the finally block
    # (session report) runs — asyncio.run does NOT gracefully cancel the
    # task on a bare KeyboardInterrupt
    stopping = asyncio.Event()
    loop.add_signal_handler(signal.SIGINT, stopping.set)

    dev = sd.query_devices(int(os.environ["CHAT_INPUT_DEVICE"])
                           if os.getenv("CHAT_INPUT_DEVICE")
                           else sd.default.device[0])
    rate = int(dev["default_samplerate"])
    ch = 1
    test_wav = os.getenv("CHAT_TEST_WAV")
    if not test_wav:
        mic = sd.RawInputStream(samplerate=rate, blocksize=int(rate / 10),
                                dtype="int16", channels=ch, device=dev["index"],
                                callback=mic_callback)
        mic.start()

    resampler = create_stream_resampler()
    vad_resampler = create_stream_resampler()
    vad = SileroVADAnalyzer(
        sample_rate=IN_RATE,
        params=VADParams(min_volume=float(os.getenv("CHAT_MIN_VOLUME", "0.15")),
                         start_secs=0.2, stop_secs=0.4))
    vad.set_sample_rate(IN_RATE)

    history: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]
    tokens = ta.TokenAccount()
    turns = 0
    mode_desc = (f"{MODEL} single-call" if not THINK_MODEL else
                 f"{STT_CFG or MODEL} -> {THINK_MODEL} -> {MODEL}")

    print(banner(f"Friday ({PRESET}) — speak, then pause. Ctrl+C to quit."))
    print(dim(f"workdir: {WORKDIR} · tools: bash, get_meeting_context · "
              f"key {'set' if KEY else 'MISSING'}"), flush=True)

    if test_wav:
        async def feeder():
            await asyncio.sleep(1.0)
            with wave.open(test_wav) as w:
                pcm = w.readframes(w.getnframes()); wsr = w.getframerate()
            r = create_stream_resampler()
            up = await r.resample(pcm, wsr, rate)
            chunk = int(rate / 10) * 2
            silence = b"\x00\x00" * int(rate * 1.5)
            for b in [up[i:i+chunk] for i in range(0, len(up), chunk)] + [silence]:
                await mic_q.put(b)
                await asyncio.sleep(0.1)
            print(dim("[test] utterance fed"), flush=True)
        asyncio.create_task(feeder())

    state = "quiet"
    lvl_buf = bytearray()
    utter24 = bytearray()      # 24 kHz utterance being recorded
    ever_sound = False
    utt_start = None
    quiet_since = None
    last_level_print = time.monotonic()

    async def do_turn(utt: bytes):
        """One spoken turn. audio mode = single call; separate mode =
        STT -> think -> TTS, each stage timed and costed."""
        nonlocal turns
        t_end = time.perf_counter()
        b64 = pcm_to_wav_b64(utt)
        turns += 1
        my_turn = turn_seq
        first_audio: dict = {}

        def on_audio(b64_chunk):
            if my_turn != turn_seq:
                return  # stale audio from an interrupted turn
            first_audio.setdefault("t", time.perf_counter() - t_end)
            asyncio.ensure_future(speaker.feed(base64.b64decode(b64_chunk)))

        if not THINK_MODEL:
            # ---- audio mode: voice model hears, thinks, speaks ----
            if DEBUG:
                print(dim(f"[vad] speech stopped ({len(utt)/2/24000:.1f}s) — asking {MODEL}..."), flush=True)
            # what the agent is getting: local whisper runs while the
            # request is in flight; printed with the turn report below
            disp = asyncio.create_task(
                asyncio.to_thread(transcribe_utterance, utt, True))  # (txt, usage)
            audio_user_msg = {"role": "user", "content": [
                {"type": "text", "text": "Answer my spoken question."},
                {"type": "input_audio",
                 "input_audio": {"data": b64, "format": "wav"}},
            ]}
            msgs = history + [audio_user_msg]
            text_parts: list[str] = []
            tool_calls, usage = await chat_stream(msgs, on_audio,
                                                  text_parts.append)
            transcript = "".join(text_parts).strip()
            tokens.add(usage)
            if any(tc["name"] for tc in tool_calls):
                tc = next(tc for tc in tool_calls if tc["name"])
                name = tc["name"]
                tool_result = run_tool(name, tc["arguments"])
                result_json = json.dumps(tool_result)
                print(tool(f"[tool] {name}({(tc['arguments'] or '')[:80]}) -> {result_json[:160]}"), flush=True)
                follow = msgs + [{"role": "user", "content": [
                    {"type": "text",
                     "text": f"{name} returned: {result_json}. Answer the "
                             f"original question briefly."}]}]
                text_parts.clear()
                first_audio.clear()
                _, usage2 = await chat_stream(follow, on_audio,
                                              text_parts.append)
                tokens.add(usage2)
                transcript = "".join(text_parts).strip()
            reply = transcript or "(tool result answer)"
            stages = ""
            user_text = "Answer my spoken question."
        else:
            # ---- separate mode: STT -> think -> TTS ----
            if DEBUG:
                print(dim(f"[vad] speech stopped ({len(utt)/2/24000:.1f}s) — separate pipeline"), flush=True)

            # 1) STT: online OpenAI API (default), local whisper fallback,
            #    or the OpenRouter voice model transcribes
            t0 = time.perf_counter()
            openrouter_stt = (not STT_CFG
                              or ("/" in STT_CFG and ":" not in STT_CFG))
            if openrouter_stt:
                stt_parts: list[str] = []
                _, u = await chat_stream(
                    [{"role": "system",
                      "content": "You are a transcription engine. Output "
                                 "ONLY the exact words spoken in the audio "
                                 "— no replies, no commentary, no questions."},
                     {"role": "user", "content": [{"type": "input_audio",
                      "input_audio": {"data": b64, "format": "wav"}}]}],
                    None, stt_parts.append, stt=True)
                user_text = "".join(stt_parts).strip()
            else:
                # online OpenAI API (stt = openai[:model]) or local whisper
                # (stt = whisper:<size>) — no OpenRouter call, zero tokens
                user_text, u = await asyncio.to_thread(
                    transcribe_utterance, utt)
            t_stt = time.perf_counter() - t0
            tokens.add(u)  # local whisper: u is None, zero tokens
            print(stage(f"[stage] stt {t_stt:.2f}s:") + " " + _you_c(f"You: {user_text}"), flush=True)
            if not user_text:
                print(_warn_c("[stage] stt produced no text — turn skipped"), flush=True)
                return

            # 2) THINK: text model answers, with native tools (text models
            #    accept standard tool_calls — no OpenRouter audio fragility)
            think_msgs = (history
                          + [{"role": "user", "content": user_text}])
            reply_parts: list[str] = []
            t0 = time.perf_counter()
            tool_calls, u = await chat_stream(think_msgs, None,
                                              reply_parts.append, think=True)
            if any(tc["name"] for tc in tool_calls):
                tc = next(tc for tc in tool_calls if tc["name"])
                name = tc["name"]
                tool_result = json.dumps(run_tool(name, tc["arguments"]))
                print(tool(f"[tool] {name}({(tc['arguments'] or '')[:80]}) -> {tool_result[:160]}"), flush=True)
                think_msgs += [
                    {"role": "assistant", "content": "".join(reply_parts),
                     "tool_calls": [{"id": tc.get("id") or "call_1",
                                     "type": "function",
                                     "function": {"name": tc["name"],
                                                  "arguments": tc["arguments"]}}],
                     },
                    {"role": "tool",
                     "tool_call_id": tc.get("id") or "call_1",
                     "content": tool_result},
                ]
                reply_parts.clear()
                _, u2 = await chat_stream(think_msgs, None,
                                          reply_parts.append, think=True)
                if u2:
                    u = u2
            reply = "".join(reply_parts).strip()
            t_think = time.perf_counter() - t0
            tokens.add(u)
            print(stage(f"[stage] think {t_think:.2f}s"), flush=True)

            # 3) TTS: voice model speaks the reply (text in, audio out)
            first_audio.clear()
            t0 = time.perf_counter()
            if VOICE_MODEL.startswith("openai-tts:"):
                # dedicated OpenAI TTS — the audio/speech endpoint returns
                # no usage, so tokens/cost are estimated from the speech
                # length at the documented ~15 tokens/s density ($12/M)
                loop = asyncio.get_running_loop()

                def _speak():
                    pcm, secs = openai_tts(
                        reply, VOICE_MODEL.split(":", 1)[1] or "coral")
                    loop.call_soon_threadsafe(
                        on_audio, base64.b64encode(pcm).decode())
                    return secs

                secs = await asyncio.to_thread(_speak)
                out_tok = round(secs * 15)
                u = {"prompt_tokens": 0, "completion_tokens": out_tok,
                     "input_audio_tokens": 0,
                     "output_audio_tokens": out_tok,
                     "cost": round(out_tok * 12e-6, 6)}
            else:
                _, u = await chat_stream(
                    [{"role": "user", "content": reply}],
                    on_audio, None, tts=True)
            t_tts = time.perf_counter() - t0
            tokens.add(u)
            print(stage(f"[stage] tts {t_tts:.2f}s"), flush=True)
            stages = (f"stt {t_stt:.2f}s think {t_think:.2f}s "
                      f"tts {t_tts:.2f}s")
            transcript = reply

        # memory
        if THINK_MODEL:
            history.append({"role": "user", "content": user_text})
            history.append({"role": "assistant", "content": reply})
        else:
            history.append({"role": "user", "content": [
                {"type": "text", "text": "Answer my spoken question."},
                {"type": "input_audio",
                 "input_audio": {"data": b64, "format": "wav"}},
            ]})
            history.append({"role": "assistant",
                            "content": transcript or "(tool result answer)"})

        tok = tokens.end_turn()
        lat = first_audio.get("t") or 0.0
        stages_c = f" · {stages.strip(' |')}" if stages else ""
        if not THINK_MODEL:
            try:
                yt = (await disp)[0].strip()
            except Exception:
                yt = ""
            if yt:
                print(_you_c(f"You: {yt}"), flush=True)
        print(friday(f"Friday: {transcript}"), flush=True)
        total = sum(t["cost"] for t in tokens.turns)
        cache_s = (f" · cached {tok['cached']:,} "
                   f"({tok['cached_text']:,} text/{tok['cached_audio']:,} audio)"
                   if tok["cached"] else "")
        print(dim(f"── turn {turns} · first audio {lat:.2f}s{stages_c} · "
                  f"in {tok['in']:,} ({tok['in_audio']:,} audio) · "
                  f"out {tok['out']:,} ({tok['out_audio']:,} audio){cache_s} · "
                  f"~${tok['cost']:.4f} (total ~${total:.4f})"), flush=True)

    was_playing = False
    try:
        while not stopping.is_set():
            # timeout tick: the commit check must run even when no mic
            # blocks arrive (silence after an utterance still needs
            # evaluating, or the turn is never committed)
            try:
                block = await asyncio.wait_for(mic_q.get(), timeout=0.1)
            except asyncio.TimeoutError:
                block = None
            if block is None:
                if state == "recording":
                    # keep the VAD advancing on silence so the analyzer can
                    # reach QUIET (stop_secs needs samples, not wall time)
                    st = await vad.analyze_audio(
                        b"\x00\x00" * int(IN_RATE / 10 * 2))
                    if st == VADState.QUIET:
                        utter24 += b"\x00\x00" * int(24000 / 10 * 2)
                        if quiet_since is None:
                            quiet_since = time.monotonic()
                    if (quiet_since is not None
                            and time.monotonic() - quiet_since >= 0.5
                            and len(utter24) / 2 / 24000 >= 0.4
                            and not speaker.playing):
                        state = "quiet"
                        utt = bytes(utter24)
                        utter24.clear()
                        asyncio.ensure_future(do_turn(utt))
                continue
            pcm = np.frombuffer(block, dtype=np.int16)
            raw16 = await vad_resampler.resample(block, rate, IN_RATE)
            raw24 = await resampler.resample(block, rate, 24000)

            lvl_buf += raw24
            del lvl_buf[: max(0, len(lvl_buf) - int(24000 * 0.5) * 2)]
            if time.monotonic() - last_level_print > 5:
                last_level_print = time.monotonic()
                try:
                    vol = calculate_audio_volume(bytes(lvl_buf), 24000)
                except Exception:
                    vol = -1
                if DEBUG:
                    print(dim(f"[mic] level={vol:.2f}"), flush=True)
                if lvl_buf:
                    a = np.frombuffer(bytes(lvl_buf), np.int16).astype(np.float64)
                    rms = float(np.sqrt(np.mean(a ** 2)))
                    if rms > 300:
                        ever_sound = True
                    elif not ever_sound:
                        print(_warn_c("[warn] mic capture is silent — the machine is not receiving your voice. On RDP: enable microphone redirection and reconnect."), flush=True)

            if speaker.playing:
                # Friday is playing: the mic may be hearing the playback
                # itself (RDP echo). Buffer nothing and don't feed the VAD
                # (Friday must not barge in on itself); barge in only on
                # clearly louder real speech.
                a = np.frombuffer(raw24, dtype=np.int16).astype(np.float64)
                rms = float(np.sqrt(np.mean(a ** 2))) if len(a) else 0.0
                if rms >= BARGE_RMS:
                    turn_seq += 1
                    speaker.barge_in()
                    print(_warn_c(
                        f"[barge-in] real speech over playback "
                        f"(rms {rms:.0f} >= gate {BARGE_RMS})"), flush=True)
                    state = "recording"
                    utter24.clear()
                    quiet_since = None
                elif DEBUG:
                    print(dim(f"[echo] bleed rms {rms:.0f} < gate {BARGE_RMS}"),
                          flush=True)
                was_playing = True
                continue
            if was_playing:
                # playback ended without barge-in: drop anything buffered
                # during it and start listening fresh
                was_playing = False
                state = "quiet"
                utter24.clear()
                quiet_since = None

            st = await vad.analyze_audio(raw16)

            # barge-in: user speech while Friday plays -> cut playback
            if st == VADState.SPEAKING and speaker.playing:
                turn_seq += 1
                speaker.barge_in()
                print(_warn_c("[barge-in] cutting playback"), flush=True)
                state = "recording"
                utter24.clear()
                utt_start = time.monotonic()
                quiet_since = None
                continue

            if st in (VADState.SPEAKING, VADState.STARTING):
                if state == "quiet":
                    state = "recording"
                    utter24.clear()
                    utt_start = time.monotonic()
                    if DEBUG:
                        print(dim("[vad] speech started"), flush=True)
                utter24 += raw24
                quiet_since = None
            elif st == VADState.QUIET and state == "recording":
                utter24 += raw24
                if quiet_since is None:
                    quiet_since = time.monotonic()
                dur = len(utter24) / 2 / 24000
                if (time.monotonic() - quiet_since >= 0.5
                        and dur >= 0.4):
                    state = "quiet"
                    utt = bytes(utter24)
                    utter24.clear()
                    if speaker.playing:
                        continue  # playback still ongoing; don't double-send
                    asyncio.ensure_future(do_turn(utt))
                elif dur < 0.4 and time.monotonic() - quiet_since > 1.0:
                    state = "quiet"
                    utter24.clear()
    except asyncio.CancelledError:
        pass
    finally:
        speaker.stop()
        if not test_wav:
            mic.stop()
            mic.close()
        # session report: tokens in/out per turn + totals (M8 cost scope)
        try:
            stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
            out = HERE / "results" / f"{PRESET}-{stamp}.json"
            out.parent.mkdir(exist_ok=True)
            out.write_text(json.dumps({
                "mode": PRESET,
                "engine": "chat_audio",
                "started_utc": started_utc,
                "turns": turns,
                "tokens": tokens.report(),
            }, indent=2))
            print(dim(f"session: {turns} turns · {tokens.line()} "
                      f"→ {out.name}"), flush=True)
        except Exception as e:  # noqa: BLE001
            print(err(f"(session report failed: {e!r})"), flush=True)


def main():
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    def _hup(*_):
        os._exit(0)
    signal.signal(signal.SIGHUP, _hup)
    main()