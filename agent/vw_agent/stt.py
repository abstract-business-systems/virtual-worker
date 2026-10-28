"""Speech-to-text: faster-whisper with simple energy-based endpointing."""
import threading
import time

from .config import CONFIG

_MODEL = None
_MODEL_LOCK = threading.Lock()


def get_model():
    global _MODEL
    with _MODEL_LOCK:
        if _MODEL is None:
            from faster_whisper import WhisperModel
            print("[stt] loading model %s ..." % CONFIG["stt_model"], flush=True)
            _MODEL = WhisperModel(CONFIG["stt_model"], device="cpu",
                                  compute_type="int8")
            print("[stt] model ready", flush=True)
    return _MODEL


def pcm16_to_float32(pcm_bytes):
    import array
    a = array.array("h", pcm_bytes)
    return [x / 32768.0 for x in a]


def rms(chunk_floats):
    if not chunk_floats:
        return 0.0
    return (sum(x * x for x in chunk_floats) / len(chunk_floats)) ** 0.5


class Segmenter:
    """Accumulates 16k mono s16le audio, emits (text, duration) speech segments.

    Endpointing: start when RMS rises above threshold; finish after
    `silence` seconds of quiet or `window_max` seconds total.
    """

    def __init__(self, rate=None, silence=None, window_max=None):
        self.rate = rate or CONFIG["stt_sample_rate"]
        self.silence = silence or CONFIG["stt_silence"]
        self.window_max = window_max or CONFIG["stt_window_max"]
        self.threshold = 0.008
        self._buf = []          # float samples
        self._speech = False
        self._quiet_since = None
        # Monotonic timestamps make latency measurements immune to wall-clock
        # adjustments; last_timing is consumed by the agent after each segment.
        self._speech_started_at = None
        self.last_timing = None

    def feed_pcm(self, pcm_bytes):
        """Feed raw s16le bytes; returns a transcript text when a segment ends."""
        self._buf.extend(pcm16_to_float32(pcm_bytes))
        result = None
        frame = int(0.05 * self.rate)  # 50 ms frames
        while len(self._buf) >= frame:
            frame_samples = self._buf[:frame]
            del self._buf[:frame]
            level = rms(frame_samples)
            speaking = level > self.threshold
            if not self._speech:
                if speaking:
                    self._speech = True
                    self._speech_samples = list(frame_samples)
                    self._speech_started_at = time.monotonic()
                    self._quiet_since = None
                continue
            self._speech_samples.extend(frame_samples)
            if not speaking:
                if self._quiet_since is None:
                    self._quiet_since = time.monotonic()
                elif time.monotonic() - self._quiet_since >= self.silence:
                    result = self._flush()
            elif len(self._speech_samples) >= self.window_max * self.rate:
                result = self._flush()
        return result

    def _flush(self):
        # Capture the endpoint and STT boundaries for the end-to-end trace.
        endpointed_at = time.monotonic()
        speech_started_at = self._speech_started_at
        self._speech = False
        self._quiet_since = None
        self._speech_started_at = None
        samples = self._speech_samples
        self._speech_samples = []
        dur = len(samples) / self.rate
        if dur < 0.4:
            self.last_timing = None
            return None
        stt_started_at = time.monotonic()
        text = self.transcribe(samples)
        stt_finished_at = time.monotonic()
        self.last_timing = {
            "speech_started_at": speech_started_at,
            "endpointed_at": endpointed_at,
            "stt_started_at": stt_started_at,
            "stt_finished_at": stt_finished_at,
            "speech_duration_s": dur,
        }
        return (text, dur) if text else None

    def transcribe(self, samples):
        model = get_model()
        import numpy as np
        audio = np.array(samples, dtype=np.float32)
        segs, _info = model.transcribe(audio, language="en", beam_size=1,
                                       vad_filter=True)
        text = " ".join(s.text.strip() for s in segs).strip()
        return text or None