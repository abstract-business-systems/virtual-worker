"""Text-to-speech output through the virtual mic bus, and browser control
(Playwright) for joining Google Meet."""
import os
import subprocess
import tempfile
import time

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


def tts_duration(text):
    words = max(1, len(text.split()))
    return 0.4 + words * 0.45


class Browser:
    """Playwright-driven Chromium on the virtual display."""

    def __init__(self):
        self.pw = None
        self.ctx = None
        self.page = None
        self.joined_url = None

    def _ensure(self):
        if self.pw is None:
            from playwright.sync_api import sync_playwright
            os.makedirs(CONFIG["browser_profile"], exist_ok=True)
            self.pw = sync_playwright().start()
            self.ctx = self.pw.chromium.launch_persistent_context(
                CONFIG["browser_profile"],
                headless=False,                      # runs on Xvfb
                args=["--use-fake-ui-for-media-stream",
                      "--use-fake-device-for-media-stream",
                      "--autoplay-policy=no-user-gesture-required",
                      "--no-first-run", "--no-default-browser-check"],
            )
            self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()

    def close(self):
        try:
            if self.ctx:
                self.ctx.close()
        finally:
            if self.pw:
                self.pw.stop()
            self.pw = self.ctx = self.page = None
            self.joined_url = None

    # ---- joining (requirement 1) --------------------------------------------------
    def join(self, url):
        """Open a Google Meet link and enter the meeting.

        Uses the dedicated Google account if credentials are configured,
        otherwise attempts guest join. Best-effort UI automation: Meet's DOM
        changes, so every selector failure is tolerated and logged.
        """
        self._ensure()
        page = self.page
        if CONFIG["google_email"] and CONFIG["google_password"]:
            try:
                self._sign_in(page)
            except Exception as exc:
                print("[browser] google sign-in failed (%s); trying guest join"
                      % exc, flush=True)
        page.goto(url, wait_until="domcontentloaded",
                  timeout=CONFIG["join_timeout"] * 1000)
        time.sleep(3)
        self._click_any(page, [
            'button[aria-label*="Join now"]',
            'button:has-text("Join now")',
            'button:has-text("Ask to join")',
            'span:has-text("Ask to join")',
            'span:has-text("Join now")',
        ])
        # camera off / mic muted on entry (design: appears muted while listening)
        self._click_any(page, [
            'button[aria-label*="camera"][aria-label*="off"]',
            'button[aria-label="Turn off camera"]',
        ], required=False)
        self._click_any(page, [
            'button[aria-label*="microphone"][aria-label*="mute"]',
            'button[aria-label="Mute microphone"]',
        ], required=False)
        time.sleep(4)
        self.joined_url = url
        return {"joined": url, "page_title": page.title()}

    def _sign_in(self, page):
        page.goto("https://accounts.google.com/ServiceLogin",
                  timeout=45000)
        page.fill('input[type="email"]', CONFIG["google_email"])
        page.keyboard.press("Enter")
        time.sleep(3)
        page.fill('input[type="password"]', CONFIG["google_password"])
        page.keyboard.press("Enter")
        time.sleep(4)

    def _click_any(self, page, selectors, required=True):
        for sel in selectors:
            try:
                loc = page.locator(sel).first
                loc.click(timeout=3000)
                print("[browser] clicked %s" % sel, flush=True)
                return True
            except Exception:
                continue
        if required:
            print("[browser] no join button matched; page title=%s"
                  % (page.title() if page else "?"), flush=True)
        return False

    def set_mic(self, unmute):
        """Toggle the Meet mic so the worker speaks only when asked."""
        labels = (['button[aria-label*="microphone"]'] if unmute else
                  ['button[aria-label*="microphone"]'])
        if self.page is None:
            return False
        for sel in labels:
            try:
                loc = self.page.locator(sel).first
                label = loc.get_attribute("aria-label", timeout=2000) or ""
                want = "unmute" if unmute else "mute"
                if want in label.lower():
                    loc.click(timeout=2000)
                    return True
            except Exception:
                continue
        return False