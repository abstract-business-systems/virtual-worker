"""Agent configuration from environment variables."""
import os


def _int(name, default):
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def _float(name, default):
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


CONFIG = {
    # identity / behavior
    "wake_name": os.environ.get("VW_NAME", "Alex"),
    "operator_name": os.environ.get("VW_OPERATOR_NAME", "Operator"),
    "browser_profile": os.environ.get("VW_BROWSER_PROFILE",
                                      os.path.expanduser("~/.browser-profile")),
    # sockets / data
    "socket_path": os.environ.get("VW_SOCKET", "/run/virtual-worker/agent.sock"),
    "display": os.environ.get("DISPLAY", ":99"),
    # audio buses (created by ensure_pulse_setup)
    "sink": os.environ.get("VW_SINK", "vw_speaker"),        # meeting audio plays here
    "mic_source": os.environ.get("VW_MIC", "vw_mic"),        # what Chrome uses as mic
    "tts_sink": os.environ.get("VW_TTS_SINK", "vw_tts"),     # TTS plays here -> vw_mic
    # loops
    "screen_interval": _float("VW_SCREEN_INTERVAL", 5.0),    # req 6: ~5s polling
    "screen_diff_threshold": _float("VW_SCREEN_DIFF", 0.02),
    "stt_sample_rate": _int("VW_STT_RATE", 16000),
    "stt_window_max": _float("VW_STT_WINDOW_MAX", 12.0),
    "stt_silence": _float("VW_STT_SILENCE", 0.8),
    "stt_model": os.environ.get("VW_STT_MODEL", "base.en"),
    # meeting
    "google_email": os.environ.get("GOOGLE_EMAIL", ""),
    "google_password": os.environ.get("GOOGLE_PASSWORD", ""),
    "join_timeout": _int("VW_JOIN_TIMEOUT", 90),
    # timezone for reminders created in-meeting
    "default_tz": os.environ.get("VW_TZ", "UTC"),
    # demo
    "demo_mode": os.environ.get("VW_DEMO", "0") == "1",
}