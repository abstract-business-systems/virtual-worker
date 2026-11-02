"""Tiny ANSI coloring for the POC CLIs. Auto-disables when stdout is not a
terminal (piped runs, logs) unless CHAT_FORCE_COLOR=1.

One color per actor so the transcript is easy to scan:
  [you]    cyan      — what the user spoke
  Friday   green     — the agent's spoken reply
  [tool]   magenta   — tool calls
  [stage]  blue      — pipeline stage timings
  [vad]/…  gray      — mic/VAD chatter
  [warn]   yellow    — warnings
  [error]  red       — errors
"""

import os
import sys

_USE = sys.stdout.isatty() or bool(os.getenv("CHAT_FORCE_COLOR"))


def _wrap(code: str, text: str) -> str:
    if not _USE or not text:
        return text
    return f"\033[{code}m{text}\033[0m"


def you(text: str) -> str:
    return _wrap("36", text)


def friday(text: str) -> str:
    return _wrap("32", text)


def tool(text: str) -> str:
    return _wrap("35", text)


def stage(text: str) -> str:
    return _wrap("34", text)


def dim(text: str) -> str:
    return _wrap("90", text)


def warn(text: str) -> str:
    return _wrap("33", text)


def err(text: str) -> str:
    return _wrap("31", text)


def hdr(text: str) -> str:
    return _wrap("1;36", text)


def banner(text: str) -> str:
    return _wrap("1;37", text)