"""Browser control (Playwright) for joining Google Meet."""
import os
import time

from .config import CONFIG


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
            args = ["--autoplay-policy=no-user-gesture-required",
                    "--no-first-run", "--no-default-browser-check",
                    "--disable-blink-features=AutomationControlled",
                    "--hide-crash-restore-bubble",
                    "--disable-features=IsolateOrigins"]
            profile = CONFIG["browser_profile"]
            try:
                # Meet blocks outdated browsers: use the system Google Chrome
                self.ctx = self.pw.chromium.launch_persistent_context(
                    profile, channel="chrome", headless=False,
                    viewport={"width": 1920, "height": 1080},
                    # look like a normal browser: Meet refuses automation-flagged
                    # sessions ("Chrome is being controlled by automated test
                    # software") before the host ever sees a knock
                    ignore_default_args=["--enable-automation"],
                    args=args + ["--window-size=1920,1080"])
                print("[browser] using system Google Chrome", flush=True)
            except Exception:
                self.ctx = self.pw.chromium.launch_persistent_context(
                    profile, headless=False,
                    viewport={"width": 1920, "height": 1080},
                    ignore_default_args=["--enable-automation"],
                    args=args + ["--window-size=1920,1080"])
                print("[browser] using bundled chromium", flush=True)
            # grant mic/camera like a normal user would have accepted, instead
            # of the detectable --use-fake-ui-for-media-stream flag
            for origin in ("https://meet.google.com", "https://accounts.google.com"):
                try:
                    self.ctx.grant_permissions(["microphone", "camera"],
                                               origin=origin)
                except Exception:
                    pass
            self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()

    # ---- primitives for the vision loop (the brain decides, browser acts) ----
    def goto(self, url):
        self._ensure()
        self.page.goto(url, wait_until="domcontentloaded",
                       timeout=CONFIG["join_timeout"] * 1000)

    def shot_png(self):
        """Full-window screenshot as PNG bytes (same size as the viewport)."""
        return self.page.screenshot(type="png")

    def act(self, decision):
        """Execute a decision from the brain: click/fill/key/wait."""
        action = decision.get("action")
        coord = decision.get("coordinate")
        if action == "click":
            self.page.mouse.click(coord[0], coord[1])
        elif action == "fill":
            if coord:
                self.page.mouse.click(coord[0], coord[1])
            self.page.keyboard.type(decision.get("text", ""), delay=40)
        elif action == "key":
            self.page.keyboard.press(decision.get("text", "Enter"))
        elif action == "wait":
            time.sleep(2.5)
        else:
            raise ValueError("unknown action: %s" % action)
        time.sleep(1.5)   # let the UI settle before the next look

    def close(self):
        try:
            if self.ctx:
                self.ctx.close()
        finally:
            if self.pw:
                self.pw.stop()
            self.pw = self.ctx = self.page = None
            self.joined_url = None

    # ---- joining (requirement 1): scripted fallback -------------------------------
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
        time.sleep(4)
        # dismiss popups, enter the participant name so "Ask to join" enables
        self._click_any(page, ['button:has-text("Got it")'], required=False)
        self._click_any(page, ['button[aria-label="Dismiss"]'], required=False)
        try:
            page.locator('input[placeholder*="name" i], '
                         'input[aria-label*="name" i]').first.fill(
                CONFIG["wake_name"], timeout=5000)
            print("[browser] name entered: %s" % CONFIG["wake_name"],
                  flush=True)
        except Exception as exc:
            print("[browser] name input not found: %s" % exc, flush=True)
        time.sleep(1)
        self._click_any(page, [
            'button:has-text("Ask to join")',
            'button:has-text("Join now")',
            'button[aria-label*="Join now"]',
        ])
        # camera off / mic muted on entry (design: appears muted while listening)
        self._click_any(page, [
            'button[aria-label*="camera" i][aria-label*="off" i]',
            'button[aria-label="Turn off camera"]',
        ], required=False)
        self._click_any(page, [
            'button[aria-label*="microphone" i][aria-label*="mute" i]',
            'button[aria-label="Mute microphone"]',
        ], required=False)
        time.sleep(6)
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