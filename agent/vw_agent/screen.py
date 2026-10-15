"""Screen change detection (requirement 6): screenshot every ~5s, perceptual
hash diff, and OCR of the changed frame. Only extracted TEXT is forwarded,
never the image itself."""
import subprocess
import threading
import time

from .config import CONFIG


class ScreenWatcher:
    def __init__(self, on_change, interval=None, diff_threshold=None):
        self.on_change = on_change          # fn(new_text) -> None
        self.interval = interval or CONFIG["screen_interval"]
        self.threshold = diff_threshold or CONFIG["screen_diff_threshold"]
        self._stop = threading.Event()
        self._last_hash = None
        self._last_ocr = None

    def stop(self):
        self._stop.set()

    def run(self):
        from PIL import Image
        import mss
        while not self._stop.is_set():
            try:
                self.tick(mss)
            except Exception as exc:
                print("[screen] tick failed: %s" % exc, flush=True)
                time.sleep(3)
            self._stop.wait(self.interval)

    def tick(self, mss=None):
        img = self.screenshot(mss)
        h = self.ahash(img)
        if self._last_hash is not None and self._similar(h, self._last_hash):
            return None
        changed = self._last_hash is not None
        self._last_hash = h
        text = self.ocr(img)        if changed and text and text != self._last_ocr:
            self._last_ocr = text
            self.on_change(text)
            return text
        self._last_ocr = text or self._last_ocr
        return None

    # ---- internals -------------------------------------------------------------
    def screenshot(self, mss=None):
        """Full-resolution grayscale frame (hashing downscales internally)."""
        from PIL import Image
        if mss is None:
            import mss as _mss
            mss = _mss
        with mss.mss() as sct:
            raw = sct.grab(sct.monitors[0])
        img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
        return img.convert("L")

    def ahash(self, img):
        small = img.resize((256, 144))
        pixels = list(small.getdata())
        avg = sum(pixels) / len(pixels)
        bits = 0
        for p in pixels:
            bits = (bits << 1) | (1 if p > avg else 0)
        return bits

    def _similar(self, a, b):
        diff = bin(a ^ b).count("1")
        return (diff / 36864.0) < self.threshold

    def ocr(self, img_full=None):
        """Extract text from the frame. RapidOCR (pip-only) first, pytesseract
        as fallback — no root install required on the host."""
        from PIL import Image
        if img_full is None:
            img_full = self.screenshot()
        if img_full.size[0] < 400:
            return None
        try:
            from rapidocr_onnxruntime import RapidOCR
            ocr = RapidOCR()
            rgb = img_full.convert("RGB")
            result, _ = ocr(__import__("numpy").array(rgb))
            if not result:
                return None
            return " ".join(str(item[1]) for item in result).strip() or None
        except ImportError:
            pass
        try:
            import pytesseract
            return pytesseract.image_to_string(img_full).strip() or None
        except Exception:
            return None


def ensure_xvfb(display=None):
    """Start Xvfb on the configured display if it is not running yet."""
    display = display or CONFIG["display"]
    lock = "/tmp/.X%s-lock" % display.lstrip(":").split(".")[0]
    if not __import__("os").path.exists(lock):
        subprocess.Popen(["Xvfb", display, "-screen", "0", "1920x1080x24",
                          "-nolisten", "tcp"])
        for _ in range(30):
            if __import__("os").path.exists(lock):
                break
            time.sleep(0.2)
    return display