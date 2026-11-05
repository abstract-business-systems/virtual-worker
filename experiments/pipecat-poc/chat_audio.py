#!/usr/bin/env python3
"""pipecat-poc modes audio / separate — proven hand-built conversation layer
(the engine from ../audio-model-poc, moved in so pipecat-poc runs all three
mode types from one run.sh):

  mic (sounddevice, 100 ms blocks)
    -> Silero VAD (local ONNX) detects end-of-utterance
    -> audio   : audio frontend -> required shared-reasoner handoff -> audio speech
    -> separate: dedicated STT -> same shared reasoner -> dedicated TTS

The common model owns task reasoning, conversation memory and the bash tool.

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
from poc_report import SessionReport, load_environment
from poc_pricing import usage_cost, tts_cost
from poc_fixtures import fixture_plan, feed_plan
from poc_tools import run_tool as shared_run_tool, repair_tool_args as _repair_tool_args
load_environment(HERE)
from poc_reasoning import SharedReasoner, reasoning_model, reasoning_prompt, BASH_TOOL, REASON_TOOL, VOICE_INSTRUCTIONS
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
THINK_MODEL = reasoning_model()
STT_CFG = _PRESETS.get(PRESET, "stt", fallback="").strip() or None
GLOSSARY = os.environ.get("CHAT_GLOSSARY",
                          _PRESETS.get(PRESET, "glossary", fallback="")).strip()
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

SYSTEM_PROMPT = VOICE_INSTRUCTIONS if PRESET == 'audio' else reasoning_prompt(WORKDIR)
TOOLS = [BASH_TOOL]


def spoken_question_hint() -> str:
    """Text hint sent alongside input_audio. The glossary part biases the
    audio model's recognition toward domain terms (M9 audio-quality
    experiment): audio chat models weigh the text prompt when decoding
    speech, so naming the terms they mishear fixes recognition at zero
    extra cost (~40 text tokens/turn, cacheable)."""
    base = "Answer my spoken question."
    if GLOSSARY:
        base += (" Glossary of names/terms you may hear — if the audio is "
                 "unclear, map what you heard to these and use these exact "
                 "spellings: " + GLOSSARY + ".")
        # Glossary biases spelling only; never bypass tools or invent context.
    return base


def run_tool(name, arguments):
    return shared_run_tool(name, arguments, WORKDIR)

OPENAI_KEY = os.environ.get("OPENAI_API_KEY")
# Validate credentials at run time, not import time.

turn_seq = 0  # bumped on barge-in; in-flight turn audio with an older token is dropped
# Echo gate: while Friday speaks, the mic (RDP: server audio -> client
# speakers -> client mic) picks up the playback. Frames during playback are
# never buffered as user speech; barge-in requires louder-than-bleed speech.
BARGE_RMS = int(os.environ.get("CHAT_BARGE_RMS", "2500"))
# Teams/transport echo of Friday's own voice arrives AFTER playback ends
# (network delay > the playback gate). Ignore mic input for this long after
# playback and discard the preroll, or Friday converses with itself.
ECHO_COOLDOWN = float(os.environ.get("CHAT_ECHO_COOLDOWN", "1.5"))

# ---- local Whisper STT (separate mode, stt = whisper:<size>) ----
_whisper = None


def openai_transcribe(pcm24: bytes, model: str = "gpt-4o-mini-transcribe") -> str:
    """Online STT via an /audio/transcriptions endpoint. Base URL and key are
    environment-overridable so OpenRouter-hosted models (e.g. ElevenLabs
    Scribe) use the identical request shape. pcm24: 24 kHz int16 PCM."""
    import io
    api_base = os.getenv("CHAT_STT_API_BASE", "https://api.openai.com/v1")
    api_key = os.getenv("CHAT_STT_API_KEY") or OPENAI_KEY
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
        api_base.rstrip('/') + "/audio/transcriptions", data=body,
        headers={"Authorization": f"Bearer {api_key}",
                 "Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        body = json.load(resp)
    # gpt-4o-mini-transcribe returns usage: {type: tokens, input_tokens,
    # output_tokens, input_token_details{audio_tokens, text_tokens}} —
    # captured so STT cost is in the token report (was silently dropped)
    return body["text"].strip(), body.get("usage")


def openai_tts(text: str, voice: str = "coral", on_pcm=None, model: str | None = None) -> float:
    """Stream speech before the complete utterance has downloaded.

    gpt-4o-mini-tts at OpenAI returns raw 24 kHz PCM. OpenRouter-hosted
    models (e.g. elevenlabs/eleven-v4-turbo) return encoded audio; ffmpeg
    decodes it to 24 kHz mono PCM16 so the playback path is unchanged.
    Returns seconds of 24 kHz PCM produced."""
    import shutil
    import subprocess as _sp
    import urllib.error
    import urllib.request
    model = model or os.getenv("CHAT_TTS_MODEL", "gpt-4o-mini-tts")
    api_base = os.getenv("CHAT_TTS_API_BASE", "https://api.openai.com/v1")
    api_key = os.getenv("CHAT_TTS_API_KEY") or OPENAI_KEY
    voice = os.getenv("CHAT_TTS_VOICE", voice)
    body = {"model": model, "input": text}
    if voice:
        body["voice"] = voice
    fmt = os.getenv("CHAT_TTS_FORMAT") or "pcm"
    body["response_format"] = fmt
    req = urllib.request.Request(
        api_base.rstrip('/') + "/audio/speech", data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"})

    def emit(pcm: bytes):
        if on_pcm and pcm:
            on_pcm(pcm)
        return len(pcm)

    def stream_pcm(response) -> int:
        count = 0
        with response:
            while True:
                chunk = response.read(4800)
                if not chunk:
                    break
                count += emit(chunk)
        return count

    try:
        count = stream_pcm(urllib.request.urlopen(req, timeout=60))
        return count/2/24000
    except urllib.error.HTTPError as exc:
        if fmt != "pcm" or exc.code not in (400, 404, 415, 422):
            raise
        # Endpoint rejects raw PCM: fall back to encoded audio + ffmpeg decode.
        body["response_format"] = "mp3"
        req = urllib.request.Request(
            api_base.rstrip('/') + "/audio/speech", data=json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as response:
            audio = response.read()
    if not audio:
        raise RuntimeError('TTS endpoint returned no audio')
    ffmpeg = shutil.which('ffmpeg')
    if ffmpeg:
        proc = _sp.run([ffmpeg, '-loglevel', 'error', '-i', 'pipe:0',
                        '-f', 's16le', '-ar', '24000', '-ac', '1', 'pipe:1'],
                       input=audio, capture_output=True)
        if proc.returncode == 0 and proc.stdout:
            pcm = proc.stdout
        else:
            raise RuntimeError('TTS decode failed: ' + proc.stderr.decode(errors='replace')[:200])
    else:
        import io as _io
        buf = _io.BytesIO(audio)
        with wave.open(buf, 'rb') as w:
            rate, frames = w.getframerate(), w.readframes(w.getnframes())
        samples = np.frombuffer(frames, dtype=np.int16)
        if rate != 24000:
            count = max(1, int(len(samples) * 24000 / rate))
            samples = np.interp(np.linspace(0, len(samples) - 1, count),
                                np.arange(len(samples)), samples.astype(np.float64))
        pcm = samples.astype(np.int16).tobytes()
    total = 0
    for offset in range(0, len(pcm), 4800):
        total += emit(pcm[offset:offset + 4800])
    return total/2/24000


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
        self.session = None
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
        if data and self.session is not None:
            self.session.playback(data, self._rate,
                delay_s=float(t.outputBufferDacTime - t.currentTime))

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


async def chat_stream(messages, on_audio, on_text, *, think=False, tts=False, stt=False, allow_tools=True, delegate=False):
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
        "stream_options": {"include_usage": True},
    }
    if delegate:
        if not allow_tools:
            raise ValueError('Delegation requires the reason tool')
        tools = [REASON_TOOL]
        payload['tool_choice'] = {'type': 'function', 'function': {'name': 'reason'}}
    if not allow_tools:
        tools = None
    if not think and not stt:
        payload["audio"] = {"voice": VOICE, "format": "pcm16"}
    if tools:
        payload["tools"] = tools
    import urllib.request

    tool_calls: list[dict] = []
    usage = None

    event_loop = asyncio.get_running_loop()

    def worker():
        nonlocal usage
        import urllib.request
        req = urllib.request.Request(API, data=json.dumps(payload).encode(),
            headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                parts_args = {}
                for raw in resp:
                    line = raw.decode(errors="replace").strip()
                    if not line.startswith("data:"): continue
                    data = line[5:].strip()
                    if data == "[DONE]": break
                    ev = json.loads(data)
                    if ev.get("error"):
                        raise RuntimeError("Provider stream error: " + str(ev["error"])[:300])
                    if ev.get("usage") is not None: usage = ev["usage"]
                    delta = ((ev.get("choices") or [{}])[0].get("delta") or {})
                    audio = delta.get("audio") or {}
                    if audio.get("data") and on_audio:
                        event_loop.call_soon_threadsafe(on_audio, audio["data"])
                    text = audio.get("transcript") or delta.get("content")
                    if text and on_text: event_loop.call_soon_threadsafe(on_text, text)
                    for tc in delta.get("tool_calls") or []:
                        slot = parts_args.setdefault(tc.get("index", 0),
                            {"id": None, "name": "", "args": []})
                        fn = tc.get("function") or {}
                        if tc.get("id"): slot["id"] = tc["id"]
                        if fn.get("name"): slot["name"] += fn["name"]
                        if fn.get("arguments"): slot["args"].append(fn["arguments"])
                tool_calls.extend({"id": v["id"], "name": v["name"],
                    "arguments": "".join(v["args"])} for _, v in sorted(parts_args.items()))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode(errors="replace")[:300]
            raise RuntimeError(f"Provider HTTP {exc.code}: {body}") from exc

    await asyncio.to_thread(worker)
    await asyncio.sleep(0)
    return tool_calls, usage


async def run():
    global loop, mic_q, turn_seq
    if not KEY:
        raise SystemExit("OPENROUTER_API_KEY is required")
    session = SessionReport(PRESET, "chat_audio",
        {"voice": MODEL, "think": THINK_MODEL, "stt": STT_CFG})
    session.settings = {'task_reasoning': 'shared_external_model',
                        'native_voice_processing': 'still_bundled_not_free'}
    reasoner = SharedReasoner(WORKDIR, model=THINK_MODEL)
    speaker.session = session
    plan = fixture_plan()
    active_turn = None
    loop = asyncio.get_running_loop()
    mic_q = asyncio.Queue(maxsize=400)
    speaker.start()
    # clean shutdown on Ctrl+C: break the main loop so the finally block
    # (session report) runs — asyncio.run does NOT gracefully cancel the
    # task on a bare KeyboardInterrupt
    stopping = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        loop.add_signal_handler(sig, stopping.set)

    dev = (sd.query_devices(int(os.environ["CHAT_INPUT_DEVICE"])
                            if os.getenv("CHAT_INPUT_DEVICE") else sd.default.device[0])
           if not plan else None)
    rate = int(dev["default_samplerate"]) if dev else 16000
    ch = 1
    if not plan:
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

    print(banner(f"Friday ({PRESET}) — speak, then pause. Ctrl+C to quit."))
    print(dim(f"workdir: {WORKDIR} · think: {THINK_MODEL} · tool: bash · "
              f"key {'set' if KEY else 'MISSING'}"), flush=True)

    feeder_task = None
    session.ready()
    if plan:
        feeder_task = asyncio.create_task(feed_plan(
            plan, session, mic_q.put_nowait, rate, lambda: speaker.playing, stopping.set))

    state = "quiet"
    lvl_buf = bytearray()
    utter24 = bytearray()      # 24 kHz utterance being recorded
    ever_sound = False
    utt_start = None
    quiet_since = None
    last_level_print = time.monotonic()
    preroll = bytearray()

    # Replayed/echoed speech (own TTS looping back, mic re-capturing speakers)
    # made Friday re-answer old questions. Suppress near-identical repeats of
    # recently ANSWERED requests instead of paying to answer them again.
    import re as _re
    from difflib import SequenceMatcher as _Seq
    repeat_window = float(os.getenv("CHAT_REPEAT_SUPPRESS_SECONDS", "600"))
    repeat_ratio = float(os.getenv("CHAT_REPEAT_SUPPRESS_RATIO", "0.8"))
    answered_norm: list[tuple[str, float]] = []

    def _norm(text):
        return _re.sub(r"[^a-z0-9]", "", (text or "").lower())

    def is_repeat(text):
        norm = _norm(text)
        if len(norm) < 8:
            return False
        now = time.monotonic()
        answered_norm[:] = [(t, ts) for t, ts in answered_norm if now - ts <= repeat_window]
        return any(_Seq(None, norm, prev).ratio() >= repeat_ratio for prev, _ in answered_norm)

    def remember(text):
        norm = _norm(text)
        if len(norm) >= 8:
            answered_norm.append((norm, time.monotonic()))

    async def do_turn(utt: bytes):
        """Run one bounded turn; failed/empty responses remain visible failures."""
        nonlocal turns
        session.endpoint()
        row = session.current
        turns += 1
        my_turn = turn_seq
        audio_tasks = []
        transcript = ""

        def on_audio(b64_chunk):
            if my_turn != turn_seq:
                return
            pcm = base64.b64decode(b64_chunk)
            session.audio_received(pcm, 24000)
            audio_tasks.append(asyncio.create_task(speaker.feed(pcm)))

        async def request(messages, *, component, model, **options):
            started = session.now()
            try:
                calls, usage = await chat_stream(messages, on_audio if component in ('tts', 'voice_output') else None,
                                                 text_parts.append, **options)
            except Exception:
                session.stage(component, model, started)
                raise
            tokens.add(usage)
            cost, basis = usage_cost(usage, model)
            session.stage(component, model, started, usage=usage,
                          cost_usd=cost, pricing=basis)
            return calls

        try:
            b64 = pcm_to_wav_b64(utt)
            if PRESET == 'audio':
                # Native audio frontend transcribes into a REQUIRED reason call.
                # Never play a direct answer that bypasses the common model.
                user_message = {'role': 'user', 'content': [
                    {'type': 'text', 'text': 'Transcribe my complete request into the reason tool. '+spoken_question_hint()},
                    {'type': 'input_audio', 'input_audio': {'data': b64, 'format': 'wav'}},
                ]}
                messages = history + [user_message]
                text_parts = []
                calls = await request(messages, component='voice_frontend', model=MODEL, delegate=True)
                if len(calls) != 1 or calls[0].get('name') != 'reason':
                    raise RuntimeError('Audio frontend must delegate exactly once to shared reasoning')
                question = _repair_tool_args(calls[0].get('arguments')).get('question')
                if is_repeat(question):
                    print(_warn_c("[repeat] suppressed replayed/duplicate request"), flush=True)
                    remember(question)
                    session.finish_turn('')
                    return
                remember(question)
                answer = await reasoner.ask(question, session, tokens)
                # Audio providers reject role:tool here. Synthesize only the
                # authorized answer in a fresh, tools-disabled text/audio request.
                text_parts = []
                await request([
                    {'role': 'system', 'content': 'You are a speech synthesizer. Read the supplied text verbatim. Do not answer it or add anything.'},
                    {'role': 'user', 'content': answer},
                ], component='voice_output', model=MODEL, tts=True, allow_tools=False)
                transcript = ''.join(text_parts).strip()
                if not transcript or not row['output_audio_s']:
                    raise RuntimeError('Audio frontend returned no complete spoken answer')
                history.extend([{'role': 'user', 'content': question},
                                {'role': 'assistant', 'content': answer}])
            else:
                # Speech recognition, text inference/tool loop, speech synthesis.
                started = session.now()
                if not STT_CFG or ("/" in STT_CFG and ":" not in STT_CFG):
                    text_parts = []
                    await request([{"role": "system", "content": "Transcribe the audio exactly. No answer."},
                                   {"role": "user", "content": [{"type": "input_audio",
                                    "input_audio": {"data": b64, "format": "wav"}}]}],
                                  component="stt", model=MODEL, stt=True)
                    user_text = "".join(text_parts).strip()
                else:
                    user_text, usage = await asyncio.to_thread(transcribe_utterance, utt)
                    stt_model = ("gpt-4o-mini-transcribe" if STT_CFG == "openai"
                                 else STT_CFG.split(":", 1)[-1])
                    tokens.add(usage)
                    cost, basis = ((0.0, "local_no_provider_charge") if STT_CFG.startswith("whisper:")
                                   else usage_cost(usage, stt_model))
                    session.stage("stt", stt_model, started, usage=usage,
                                  cost_usd=cost, pricing=basis)
                row["input_text"] = user_text
                print(_you_c(f"You: {user_text}"), flush=True)
                if not user_text:
                    raise RuntimeError("STT returned no text")
                if is_repeat(user_text):
                    print(_warn_c("[repeat] suppressed replayed/duplicate request"), flush=True)
                    remember(user_text)
                    session.finish_turn('')
                    return
                remember(user_text)
                transcript = await reasoner.ask(user_text, session, tokens)
                started = session.now()
                if MODEL.startswith("openai-tts:"):
                    tts_model = MODEL.split(":", 1)[1] or "gpt-4o-mini-tts"
                    seconds = await asyncio.to_thread(
                        openai_tts, transcript, os.getenv("CHAT_TTS_VOICE", "coral"),
                        lambda pcm: loop.call_soon_threadsafe(
                            on_audio, base64.b64encode(pcm).decode()),
                        model=tts_model)
                    cost, basis = tts_cost(seconds, tts_model)
                    session.stage("tts", tts_model, started,
                                  cost_usd=cost, pricing=basis)
                else:
                    text_parts = []
                    await request([{"role": "user", "content": "Speak exactly this text: " + transcript}],
                                  component="tts", model=MODEL, tts=True)
            await asyncio.sleep(0)  # drain final streaming callbacks
            if audio_tasks:
                await asyncio.gather(*audio_tasks)
            if my_turn != turn_seq:
                session.finish_turn(transcript, interrupted=True)
            else:
                session.finish_turn(transcript)
        except asyncio.CancelledError:
            session.finish_turn(transcript, interrupted=True)
            raise
        except Exception as exc:
            session.error(f"{type(exc).__name__}: {exc}")
            session.finish_turn(transcript)
            print(err(f"[turn failed] {exc}"), flush=True)
        finally:
            tokens.end_turn()
            known_cost = sum(s['cost_usd'] for s in row['stages'] if s['cost_usd'] is not None)
            print(friday(f"Friday: {transcript or '(no spoken answer — failed)'}"), flush=True)
            print(dim(f"── turn {turns} · {row['status']} · "
                      f"known stage cost ~${known_cost:.4f}; see report for pricing basis"), flush=True)

    async def begin_input():
        nonlocal active_turn
        global turn_seq
        turn_seq += 1
        if active_turn and not active_turn.done():
            active_turn.cancel()
            await asyncio.gather(active_turn, return_exceptions=True)
        if not plan:
            session.next_input()  # fixture feeder already assigned the next input

    was_playing = False
    echo_until = 0.0
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
                        active_turn = asyncio.create_task(do_turn(utt))
                continue
            if not speaker.playing:
                session.observe_input(block, rate)
            pcm = np.frombuffer(block, dtype=np.int16)
            raw16 = await vad_resampler.resample(block, rate, IN_RATE)
            raw24 = await resampler.resample(block, rate, 24000)
            previous_audio = bytes(preroll)
            preroll += raw24
            del preroll[:max(0, len(preroll)-int(24000*.3)*2)]

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
                    elif not ever_sound and not plan:
                        print(_warn_c("[warn] mic capture is silent — the machine is not receiving your voice. On RDP: enable microphone redirection and reconnect."), flush=True)

            if speaker.playing:
                # Friday is playing: the mic may be hearing the playback
                # itself (RDP echo). Buffer nothing and don't feed the VAD
                # (Friday must not barge in on itself); barge in only on
                # clearly louder real speech.
                a = np.frombuffer(raw24, dtype=np.int16).astype(np.float64)
                rms = float(np.sqrt(np.mean(a ** 2))) if len(a) else 0.0
                if rms >= BARGE_RMS:
                    await begin_input()
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
                # playback ended without barge-in: its echo is still in
                # flight, so drop anything buffered during it AND ignore the
                # mic for the cooldown window, then start listening fresh
                was_playing = False
                echo_until = time.monotonic() + ECHO_COOLDOWN
                state = "quiet"
                utter24.clear()
                preroll.clear()
                quiet_since = None
                lvl_buf.clear()
                continue
            if time.monotonic() < echo_until:
                continue  # echo of our own playback; never treat it as user speech

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
                    if not plan or plan.get('allow_interruptions'):
                        await begin_input()
                    state = "recording"
                    utter24.clear()
                    utter24 += previous_audio
                    utt_start = time.monotonic()
                    if DEBUG:
                        print(dim("[vad] speech started"), flush=True)
                utter24 += raw24
                quiet_since = None
            elif st == VADState.STOPPING and state == "recording":
                # Retain trailing phonemes while Silero waits for silence.
                utter24 += raw24
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
                    active_turn = asyncio.create_task(do_turn(utt))
                elif dur < 0.4 and time.monotonic() - quiet_since > 1.0:
                    state = "quiet"
                    utter24.clear()
    except asyncio.CancelledError:
        pass
    finally:
        if active_turn and not active_turn.done():
            active_turn.cancel()
            await asyncio.gather(active_turn, return_exceptions=True)
        if feeder_task and not feeder_task.done():
            feeder_task.cancel()
            await asyncio.gather(feeder_task, return_exceptions=True)
        await reasoner.close(session)
        speaker.stop()
        if not plan:
            mic.stop()
            mic.close()
        report_path = session.write(HERE / "results")
        print(dim(f"session: {turns} turns → {report_path.name}"), flush=True)


def main():
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()