#!/usr/bin/env python3
"""Diagnose the microphone: does Silero VAD hear you?

Records 10 s from every input-capable device while you talk, then reports
per device: level (BS.1770 loudness), clipping ratio, and Silero's max
confidence — i.e. whether VAD would fire on your voice.

Run: ./mic-check.sh [device_index]
"""

import asyncio
import queue
import sys
import wave

import numpy as np
import sounddevice as sd

from pipecat.audio.utils import calculate_audio_volume, create_stream_resampler
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams

RECORD_SECS = 10


async def capture(device_index: int | None, name: str):
    q: queue.Queue[bytes] = queue.Queue()
    try:
        dev = sd.query_devices(device_index if device_index is not None else sd.default.device[0])
        sr = int(dev["default_samplerate"])
        stream = sd.RawInputStream(
            samplerate=sr,
            blocksize=max(1, int(sr / 100)),
            dtype="int16",
            channels=1,
            device=device_index,
            callback=lambda i, f, t, s: q.put(bytes(i)),
        )
        stream.start()
    except Exception as e:
        print(f"  {name}: FAILED to open ({type(e).__name__}: {e})")
        return None

    print(f"  {name} @ {sr} Hz — SPEAK NOW...", flush=True)
    blocks: list[bytes] = []
    import time as _t

    end = _t.monotonic() + RECORD_SECS
    while _t.monotonic() < end:
        try:
            blocks.append(q.get(timeout=0.2))
        except queue.Empty:
            pass
    stream.stop()
    stream.close()
    return b"".join(blocks), sr


async def analyze(name: str, raw: bytes, sr: int):
    pcm = np.frombuffer(raw, dtype=np.int16)
    if len(pcm) == 0:
        print(f"  {name}: no audio captured")
        return
    rms = float(np.sqrt(np.mean(pcm.astype(np.float64) ** 2)))
    clipped = float(np.mean(np.abs(pcm.astype(np.int32)) >= 32000))
    try:
        loud = calculate_audio_volume(raw[-int(sr * 0.5) * 2 :], sr)
    except Exception:
        loud = -1
    print(
        f"  {name}: rms={rms:.0f} loudness={loud:.2f} clipped={clipped * 100:.1f}% "
        f"peak={np.abs(pcm).max()}"
    )
    if clipped > 0.02:
        print(f"    !! >2% samples clipped — mic gain too high, speech is distorted")

    # resample to 16 kHz and run Silero chunk analysis
    r = create_stream_resampler()
    vad = SileroVADAnalyzer(sample_rate=16000, params=VADParams(min_volume=0.15))
    vad.set_sample_rate(16000)
    chunk = sr * 2 // 10  # 100 ms
    out_all = b""
    states = []
    for i in range(0, len(raw), chunk):
        out = await r.resample(raw[i : i + chunk], sr, 16000)
        if out:
            out_all += out
            states.append((await vad.analyze_audio(out)).name)
    from collections import Counter

    print(f"    silero states: {dict(Counter(states))}")

    # raw confidence percentiles from the model directly
    arr = np.frombuffer(out_all, np.int16).astype(np.float32) / 32768.0
    vals = []
    for i in range(0, len(arr) - 511, 512):
        c = vad._model(arr[i : i + 512], 16000)
        vals.append(float(np.asarray(c).reshape(-1)[0]))
    if vals:
        v = np.array(vals)
        print(
            f"    silero confidence: max={v.max():.3f} mean={v.mean():.3f} "
            f"p95={np.percentile(v, 95):.3f} (fires at >=0.7)"
        )
        with wave.open(f"/tmp/mic-check-{name.replace(' ', '_').replace('/', '_')}.wav", "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes((arr * 32768).astype(np.int16).tobytes())


async def main():
    idx = int(sys.argv[1]) if len(sys.argv) > 1 else None
    print(f"Recording {RECORD_SECS}s — please speak continuously (count 1..20 out loud).")
    if idx is None:
        devices = [d for d in sd.query_devices() if d["max_input_channels"] > 0]
        print(f"Testing {len(devices)} input devices:")
        for d in devices:
            res = await capture(d["index"], d["name"])
            if res:
                await analyze(d["name"], res[0], res[1])
    else:
        dev = sd.query_devices(idx)
        res = await capture(idx, dev["name"])
        if res:
            await analyze(dev["name"], res[0], res[1])


if __name__ == "__main__":
    asyncio.run(main())