"""Action layer: the worker acts like a normal Linux user on the desktop.

No browser controller: actions are performed with xdotool (keyboard/mouse as
a regular user on the visible session) plus OCR screenshots to locate UI
elements. Covers meeting leave/rejoin and audio routing.
"""
import os
import re
import subprocess
import time

from .config import CONFIG


def _env():
    env = dict(os.environ)
    env["DISPLAY"] = CONFIG.get("action_display") or env.get("DISPLAY", ":0")
    xauth = CONFIG.get("xauthority")
    if xauth:
        env["XAUTHORITY"] = xauth
    return env


def _run(cmd, timeout=30):
    return subprocess.run(cmd, capture_output=True, text=True,
                          timeout=timeout, env=_env())


def xdotool(*args, timeout=30):
    r = _run(["xdotool", *args], timeout=timeout)
    return r.stdout.strip()


def key(keys, delay=0.4):
    xdotool("key", keys)
    time.sleep(delay)


def type_text(text, delay=0.3):
    xdotool("type", "--delay", "60", text)
    time.sleep(delay)


def click(x, y, delay=0.6):
    xdotool("mousemove", str(int(x)), str(int(y)), "click", "1")
    time.sleep(delay)


def activate_firefox():
    """Bring a Firefox window into focus; launch one if none exists."""
    out = xdotool("search", "--name", "Mozilla Firefox")
    if not out:
        subprocess.Popen(["firefox"],
                         env=_env(),
                         stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL,
                         start_new_session=True)
        time.sleep(8)
        out = xdotool("search", "--name", "Mozilla Firefox")
    wid = out.splitlines()[0].strip() if out else None
    if wid:
        xdotool("windowactivate", "--sync", wid)
        time.sleep(0.6)
    return wid


def screenshot(path="/tmp/vw_action.png"):
    _run(["import", "-window", "root", path])
    return path if os.path.exists(path) else None


def locate_all(target, screen_path=None):
    """OCR the screen; return [(x, y), ...] centers of every line that
    contains the target text (case-insensitive), top to bottom."""
    import numpy as np
    path = screen_path or screenshot()
    if not path or not os.path.exists(path):
        return []
    try:
        from rapidocr_onnxruntime import RapidOCR
        from PIL import Image
        result, _ = RapidOCR()(np.array(Image.open(path).convert("RGB")))
    except Exception as exc:
        print("[actions] ocr failed: %s" % exc, flush=True)
        return []
    if not result:
        return []
    t = target.lower()
    hits = []
    for box, text, _conf in result:
        if t in str(text).lower():
            xs = [p[0] for p in box]
            ys = [p[1] for p in box]
            hits.append((sum(xs) / len(xs), sum(ys) / len(ys)))
    return sorted(hits, key=lambda p: p[1])


def locate_text(target, screen_path=None):
    """First (topmost) match — kept for compatibility."""
    hits = locate_all(target, screen_path)
    return hits[0] if hits else None


def locate_lowest(target, screen_path=None):
    """Lowest match on screen — headings repeat the label of the field
    below them (e.g. 'Your name'), so the field is the bottom match."""
    hits = locate_all(target, screen_path)
    return hits[-1] if hits else None


def route_audio():
    """Make sure Firefox's meeting streams use the worker's virtual buses:
    its playback goes to vw_speaker (STT) and its mic capture comes from
    the TTS-fed virtual mic."""
    sinks = _run(["pactl", "list", "short", "sinks"]).stdout
    if "vw_speaker" not in sinks:
        print("[actions] vw_speaker missing; skip routing", flush=True)
        return
    inputs = _run(["pactl", "list", "sink-inputs"]).stdout
    for block in inputs.split("\nSink Input #")[1:]:
        block = "Sink Input #" + block
        if 'application.name = "Firefox"' not in block:
            continue
        idx = re.search(r"Sink Input #(\d+)", block)
        sink = re.search(r"Sink: (\d+)", block)
        if idx and sink and "vw_speaker" not in block:
            _run(["pactl", "move-sink-input", idx.group(1), "vw_speaker"])
            print("[actions] moved sink-input %s to vw_speaker"
                  % idx.group(1), flush=True)
    sources = _run(["pactl", "list", "sources"]).stdout
    mic_name = None
    for m in re.finditer(r"Name: (\S*vw_mic\S*)", sources):
        mic_name = m.group(1)
    outputs = _run(["pactl", "list", "source-outputs"]).stdout
    for block in outputs.split("\nSource Output #")[1:]:
        block = "Source Output #" + block
        if 'application.name = "Firefox"' not in block:
            continue
        idx = re.search(r"Source Output #(\d+)", block)
        src = re.search(r"Source: (\d+)", block)
        if idx and src and mic_name:
            _run(["pactl", "move-source-output", idx.group(1), mic_name])
            print("[actions] moved source-output %s to %s"
                  % (idx.group(1), mic_name), flush=True)


def leave_meeting():
    """Leave the meeting: close the Meet tab (works from the call screen)."""
    activate_firefox()
    key("ctrl+w")
    time.sleep(2)
    return True


def open_url(url):
    activate_firefox()
    key("ctrl+l", 0.6)
    type_text(url, 0.4)
    key("Return", 0.6)


def rejoin_meeting(url, name="Alex"):
    """Open the meeting link and complete the pre-join screen by looking:
    locate the name field via OCR, type the name, press Enter. Then make
    sure the audio is routed to the worker's buses."""
    open_url(url)
    time.sleep(9)
    # allow mic permission if Firefox asks
    pos = locate_text("Allow")
    if pos:
        click(*pos)
        time.sleep(2)
    pos = locate_lowest("Your name") or locate_lowest("What's your name")
    if pos:
        click(*pos)
    else:
        key("Tab", 0.3)
    type_text(name, 0.4)
    key("Return", 0.6)
    time.sleep(6)
    route_audio()
    return True