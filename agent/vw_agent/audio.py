"""PulseAudio plumbing: virtual buses for meeting audio, STT and TTS.

Buses (all virtual, no hardware needed):
  vw_speaker  (null sink)     : meeting audio plays here (Chrome output)
  vw_speaker.monitor          : the agent records this for STT
  vw_tts     (null sink)      : TTS plays here
  vw_mic     (virtual source) : fed by vw_tts.monitor; Chrome uses it as its
                               microphone -> silence when the worker is idle

Everything is idempotent: existing modules are detected and skipped.
"""
import subprocess
import time

from .config import CONFIG


def _run(cmd, check=True):
    return subprocess.run(cmd, capture_output=True, text=True, check=check)


def pactl(*args, check=True):
    return _run(["pactl"] + list(args), check=check).stdout


def _module_loaded(name):
    out = pactl("list", "short", "modules", check=False)
    return name in out


def ensure_pulse_setup():
    """Create the virtual buses if missing. Safe to call repeatedly."""
    sink, tts_sink, mic = CONFIG["sink"], CONFIG["tts_sink"], CONFIG["mic_source"]

    if sink not in pactl("list", "short", "sinks"):
        pactl("load-module", "module-null-sink",
              "sink_name=%s" % sink,
              "sink_properties=device.description=VirtualWorker_MeetingAudio")
    if tts_sink not in pactl("list", "short", "sinks"):
        pactl("load-module", "module-null-sink",
              "sink_name=%s" % tts_sink,
              "sink_properties=device.description=VirtualWorker_TTS")
    if mic not in pactl("list", "short", "sources"):
        pactl("load-module", "module-virtual-source",
              "source_name=%s" % mic, "master=%s.monitor" % tts_sink,
              "source_properties=device.description=VirtualWorker_Mic")
    # make the virtual mic and speaker the defaults for the browser
    pactl("set-default-source", mic, check=False)
    pactl("set-default-sink", sink, check=False)
    time.sleep(0.3)
    return {
        "sink": sink,
        "sink_monitor": "%s.monitor" % sink,
        "tts_sink": tts_sink,
        "mic_source": mic,
    }


def start_recording(source_monitor, rate=16000):
    """Start parec recording 16kHz mono s16le; returns (Popen, proc-wide stdin)."""
    proc = subprocess.Popen(
        ["parec", "--format=s16le", "--rate=%d" % rate, "--channels=1",
         "-d", source_monitor],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    return proc


def play_wav(path, sink=None):
    sink = sink or CONFIG["tts_sink"]
    _run(["paplay", "--device=%s" % sink, path])


def set_sink_volume(sink, percent):
    pactl("set-sink-volume", sink, "%d%%" % percent, check=False)