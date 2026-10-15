"""Text-to-speech output through the virtual mic bus (vw_tts -> vw_mic)."""
import os
import subprocess
import tempfile

from .config import CONFIG


def tts_play(text):
    """Speak `text` into the vw_tts sink (which feeds the virtual mic)."""
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        wav = f.name
    try:
        subprocess.run(["espeak-ng", "-v", "en-us", "-s", "155", "-p", "40",
                        "-w", wav, text], check=True,
                       capture_output=True, timeout=60)
        subprocess.run(["paplay", "--device=%s" % CONFIG["tts_sink"], wav],
                       check=True, capture_output=True, timeout=120)
    finally:
        try:
            os.unlink(wav)
        except OSError:
            pass