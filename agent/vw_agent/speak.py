"""Text-to-speech output through the virtual mic bus (vw_tts -> vw_mic).

Engines, in order of preference:
  openrouter : neural voice via OpenRouter (openai/gpt-audio-mini, streaming
               pcm16 -> WAV -> paplay). High quality, uses OPENROUTER_API_KEY.
  espeak-ng  : offline fallback.

Config: VW_TTS_ENGINE=auto|openrouter|espeak, VW_TTS_MODEL, VW_TTS_VOICE.
"""
import base64
import json
import os
import subprocess
import tempfile
import urllib.request
import wave

from .config import CONFIG


def _tts_wav_openrouter(text, wav_path):
    """Synthesize via OpenRouter streaming audio; write a WAV file."""
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise RuntimeError("OPENROUTER_API_KEY not set")
    body = json.dumps({
        "model": CONFIG["tts_model"],
        "modalities": ["text", "audio"],
        "audio": {"voice": CONFIG["tts_voice"], "format": "pcm16"},
        "stream": True,
        "messages": [
            {"role": "system", "content":
                "You are %s, a virtual worker. Read the user's text out "
                "loud, verbatim, in a natural friendly spoken style. Do not "
                "add anything." % CONFIG["wake_name"]},
            {"role": "user", "content": text},
        ],
    }).encode()
    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions",
        data=body,
        headers={"Authorization": "Bearer %s" % key,
                 "Content-Type": "application/json"},
    )
    chunks = []
    with urllib.request.urlopen(req, timeout=120) as resp:
        for raw_line in resp:
            line = raw_line.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            try:
                msg = json.loads(payload)
            except ValueError:
                continue
            if "error" in msg:
                raise RuntimeError("openrouter tts error: %s"
                                   % str(msg["error"])[:200])
            delta = (msg.get("choices") or [{}])[0].get("delta", {})
            audio = delta.get("audio") or {}
            if audio.get("data"):
                chunks.append(audio["data"])
    if not chunks:
        raise RuntimeError("openrouter tts returned no audio")
    pcm = base64.b64decode("".join(chunks))
    with wave.open(wav_path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(CONFIG["tts_sample_rate"])
        w.writeframes(pcm)


def _tts_wav_espeak(text, wav_path):
    subprocess.run(["espeak-ng", "-v", "en-us", "-s", "155", "-p", "40",
                    "-w", wav_path, text], check=True,
                   capture_output=True, timeout=60)


_ELEVEN_VOICE_IDS = {}

def _eleven_voice_id(name, key):
    """Resolve an ElevenLabs voice NAME to its voice_id (cached)."""
    if not _ELEVEN_VOICE_IDS:
        req = urllib.request.Request(
            "https://api.elevenlabs.io/v1/voices",
            headers={"xi-api-key": key})
        with urllib.request.urlopen(req, timeout=30) as resp:
            lib = json.loads(resp.read()).get("voices", [])
        for v in lib:
            _ELEVEN_VOICE_IDS[v["name"].lower()] = v["voice_id"]
    vid = _ELEVEN_VOICE_IDS.get(name.lower())
    if not vid:
        vid = next(iter(_ELEVEN_VOICE_IDS.values()), None)
    if not vid:
        raise RuntimeError("elevenlabs: no voices on this account")
    return vid


def _tts_wav_elevenlabs(text, wav_path):
    """ElevenLabs neural TTS -> raw pcm_44100 -> WAV (mono s16le)."""
    import wave
    key = CONFIG["elevenlabs_api_key"]
    if not key:
        raise RuntimeError("elevenlabs: VW_ELEVENLABS_API_KEY not set")
    vid = _eleven_voice_id(CONFIG["eleven_voice"], key)
    body = json.dumps({
        "text": text,
        "model_id": CONFIG["eleven_model"],
    }).encode()
    url = ("https://api.elevenlabs.io/v1/text-to-speech/%s"
           "?output_format=pcm_44100" % vid)
    req = urllib.request.Request(url, data=body, headers={
        "xi-api-key": key,
        "Content-Type": "application/json",
        "accept": "audio/*",
    })
    with urllib.request.urlopen(req, timeout=90) as resp:
        pcm = resp.read()
    w = wave.open(wav_path, "wb")
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(44100)
    w.writeframes(pcm)
    w.close()


def tts_synth(text, wav_path):
    """Synthesize `text` to a WAV file (no playback)."""
    engine = CONFIG["tts_engine"]
    error = None
    if engine in ("auto", "elevenlabs") and CONFIG["elevenlabs_api_key"]:
        try:
            _tts_wav_elevenlabs(text, wav_path)
            return
        except Exception as exc:
            error = exc
            if engine == "elevenlabs":
                raise
    if engine in ("auto", "openrouter"):
        try:
            _tts_wav_openrouter(text, wav_path)
            return
        except Exception as exc:
            error = exc
            if engine == "openrouter":
                raise
    if error is not None or engine == "espeak":
        _tts_wav_espeak(text, wav_path)


def play_wav_file(path):
    subprocess.run(["paplay", "--device=%s" % CONFIG["tts_sink"], path],
                   check=True, capture_output=True, timeout=180)


def tts_play(text):
    """Speak `text` into the vw_tts sink (which feeds the virtual mic)."""
    engine = CONFIG["tts_engine"]
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        wav = f.name
    try:
        error = None
        if engine in ("auto", "elevenlabs") and CONFIG["elevenlabs_api_key"]:
            try:
                _tts_wav_elevenlabs(text, wav)
            except Exception as exc:
                error = exc
                if engine == "elevenlabs":
                    raise
        if engine in ("auto", "openrouter"):
            try:
                _tts_wav_openrouter(text, wav)
            except Exception as exc:
                error = exc
                if engine == "openrouter":
                    raise
        if error is not None or engine == "espeak":
            _tts_wav_espeak(text, wav)
        play_wav_file(wav)
    finally:
        try:
            os.unlink(wav)
        except OSError:
            pass