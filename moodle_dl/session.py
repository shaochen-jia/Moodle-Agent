from __future__ import annotations

import json
import sys
import time

from playwright.sync_api import BrowserContext, Page, sync_playwright

from . import lock
from .config import Config
from .notify import notify

LOGIN_TIMEOUT_S = 10 * 60  # give the user 10 minutes to complete SSO + MFA

# Session cookies older than this are assumed dead server-side; restoring them
# would only overwrite a fresher session held in the browser profile.
SESSION_COOKIE_MAX_AGE_H = 12


class LoginRequired(RuntimeError):
    """Raised when a sync could not proceed because nobody logged in."""


class BrowserBusy(RuntimeError):
    """Raised when another part of the app already holds the browser profile.

    Chromium allows one process into a profile directory at a time, so an
    auto-sync running in the background makes the setup screen's course fetch
    fail - and it failed with a hundred lines of Chromium launch flags, which
    told the user nothing. Claiming the profile explicitly turns that into a
    sentence: something else is using it, wait.
    """


class MoodleSession:
    """A logged-in Moodle browser session backed by a persistent profile.

    The first run opens a visible browser so the user can log in through
    Okta/SSO + MFA. Cookies live in the persistent profile directory, so
    later runs reuse them headlessly until they expire.
    """

    def __init__(self, cfg: Config, headful: bool = False):
        self.cfg = cfg
        self.headful = headful
        self._pw = None
        self.ctx: BrowserContext | None = None
        self._holds_lock = False

    def __enter__(self) -> "MoodleSession":
        if not lock.acquire("browser"):
            raise BrowserBusy(
                "Another part of the app is using the browser right now.")
        self._holds_lock = True
        try:
            self._pw = sync_playwright().start()
            self._launch(headless=not self.headful)
            if not self._ensure_logged_in():
                raise LoginRequired("Sign-in is needed before files can sync.")
        except BaseException:
            # __exit__ never runs when __enter__ raises, so the profile would
            # stay claimed until the process died.
            self._unlock()
            raise
        return self

    def __exit__(self, *exc) -> None:
        try:
            if self.ctx:
                self._save_cookies()
                self.ctx.close()
            if self._pw:
                self._pw.stop()
        finally:
            self._unlock()

    def _unlock(self) -> None:
        if getattr(self, "_holds_lock", False):
            lock.release("browser")
            self._holds_lock = False

    # -- internals ---------------------------------------------------------

    @property
    def _cookie_file(self):
        return self.cfg.session_dir / "cookies.json"

    def _save_cookies(self) -> None:
        """Persist all cookies, including session cookies that the browser
        would otherwise drop on restart (Moodle and Okta both use them)."""
        try:
            cookies = self.ctx.cookies()
            self._cookie_file.write_text(json.dumps(cookies), encoding="utf-8")
        except Exception as e:
            print(f"Could not save the login session: {e}", file=sys.stderr)

    def _restore_cookies(self) -> None:
        """Re-inject saved cookies, but never overwrite a live session with a
        stale one.

        Session cookies (no expiry) are what actually keep Moodle and the SSO
        provider logged in, and they die server-side within a day or so.
        Restoring a days-old snapshot on top of the browser profile would
        replace whatever fresh session the profile still holds - which stops
        the session ever rolling forward. So old session cookies are dropped,
        and expired ones are never restored at all.
        """
        if not self._cookie_file.exists():
            return
        try:
            age_h = (time.time() - self._cookie_file.stat().st_mtime) / 3600
            cookies = json.loads(self._cookie_file.read_text(encoding="utf-8"))
            now = time.time()
            keep = []
            for c in cookies:
                expires = c.get("expires", -1)
                if expires and expires > 0:
                    if expires > now:
                        keep.append(c)      # long-lived: device trust, prefs
                elif age_h <= SESSION_COOKIE_MAX_AGE_H:
                    keep.append(c)          # session cookie, still plausible
            if keep:
                self.ctx.add_cookies(keep)
        except Exception as e:
            print(f"Could not restore the saved session: {e}", file=sys.stderr)

    def _launch(self, headless: bool, offscreen: bool = False) -> None:
        if self.ctx:
            self.ctx.close()
        self.cfg.session_dir.mkdir(parents=True, exist_ok=True)
        args = []
        if offscreen:
            # A real (headful) window, but parked far off-screen: Okta blocks
            # true headless, yet usually renews the session here without any
            # user action - so the user never sees a window unless needed.
            args.append("--window-position=-32000,-32000")
        # Try the bundled Chromium first, then fall back to a system browser
        # (Edge ships with Windows; Chrome covers most other setups).
        last_err: Exception | None = None
        for channel in (None, "msedge", "chrome"):
            try:
                self.ctx = self._pw.chromium.launch_persistent_context(
                    str(self.cfg.session_dir),
                    channel=channel,
                    headless=headless,
                    args=args,
                    viewport={"width": 1280, "height": 900},
                    # Headless mode advertises "HeadlessChrome" in the UA,
                    # which SSO providers may treat as a bot - use a normal UA.
                    user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                "AppleWebKit/537.36 (KHTML, like Gecko) "
                                "Chrome/150.0.0.0 Safari/537.36"),
                )
                self._restore_cookies()
                return
            except Exception as e:
                last_err = e
        raise RuntimeError(f"Could not launch any browser: {last_err}")

    def _show_window(self, page: Page) -> None:
        """Move an off-screen browser window into view for the user."""
        try:
            cdp = self.ctx.new_cdp_session(page)
            win = cdp.send("Browser.getWindowForTarget")
            cdp.send("Browser.setWindowBounds", {
                "windowId": win["windowId"],
                "bounds": {"left": 120, "top": 60,
                           "width": 1150, "height": 850,
                           "windowState": "normal"},
            })
            page.bring_to_front()
        except Exception:
            pass

    def _hide_window(self, page: Page) -> None:
        """Park the browser window off-screen once no human input is needed."""
        try:
            cdp = self.ctx.new_cdp_session(page)
            win = cdp.send("Browser.getWindowForTarget")
            cdp.send("Browser.setWindowBounds", {
                "windowId": win["windowId"],
                "bounds": {"left": -32000, "top": -32000,
                           "width": 1150, "height": 850,
                           "windowState": "normal"},
            })
        except Exception:
            pass

    def _needs_human(self, page: Page) -> bool:
        """True if the page is showing a login form (username/password)."""
        try:
            sel = ('input[name="identifier"], input[type="password"], '
                   'input[name="username"], input[type="email"]')
            return page.locator(sel).count() > 0
        except Exception:
            return False

    def _is_logged_in(self, page: Page) -> bool:
        if not page.url.startswith(self.cfg.base_url):
            return False  # bounced to Okta / SSO
        if "/login" in page.url:
            return False
        # Moodle tags the <body> with "notloggedin" for guests
        return page.locator("body.notloggedin").count() == 0

    def _wait_logged_in(self, page: Page, timeout_s: float) -> bool:
        """Poll until the page reaches a logged-in Moodle state.

        SSO providers (Okta) redirect via JavaScript, so the login state can
        flip well after the initial page load - polling is the reliable way.
        """
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            try:
                if self._is_logged_in(page):
                    return True
                page.wait_for_timeout(2000)
            except Exception:
                if page.is_closed():
                    print("Browser window was closed before login finished.",
                          file=sys.stderr)
                    return False
                time.sleep(2)
        return False

    def _ensure_logged_in(self) -> bool:
        page = self.ctx.new_page()
        dashboard = f"{self.cfg.base_url}/my/"
        page.goto(dashboard, wait_until="domcontentloaded")

        # Give silent SSO redirects a chance before involving the user.
        if self._wait_logged_in(page, 20):
            self._save_cookies()
            page.close()
            return True

        if not self.headful:
            # Session expired: retry with an invisible off-screen window
            # (headless is blocked by Okta, but off-screen usually renews
            # the session silently - the user sees nothing).
            print("Renewing the Moodle session in the background...",
                  file=sys.stderr)
            self._launch(headless=False, offscreen=True)
            page = self.ctx.new_page()
            page.goto(dashboard, wait_until="domcontentloaded")
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                try:
                    if self._is_logged_in(page):
                        self._save_cookies()
                        page.close()
                        return True
                    if self._needs_human(page):
                        break  # a real login form - the user must act
                    page.wait_for_timeout(2000)
                except Exception:
                    if page.is_closed():
                        return False
                    time.sleep(2)
            self._show_window(page)
            notify("Moodle Downloader needs you to sign in",
                   "Your university session expired. Sign in in the browser "
                   "window that just opened - syncing continues by itself.")

        print("Please log in to Moodle in the browser window "
              "(waiting up to 10 minutes)...", file=sys.stderr)
        print("TIP: tick 'Keep me signed in' on the Okta page so future runs "
              "skip MFA.", file=sys.stderr)
        print("(Don't close the window - it closes by itself when the "
              "download finishes.)", file=sys.stderr)
        ok = self._wait_logged_in(page, LOGIN_TIMEOUT_S)
        if ok:
            self._save_cookies()
            if not page.is_closed():
                # Get the window out of the way again: the rest of the sync
                # needs no supervision, and leaving an empty browser on screen
                # makes it look like something is stuck.
                self._hide_window(page)
                page.close()
            notify("Signed in - syncing now",
                   "You can ignore the browser window; it closes by itself.")
        return ok

    # -- public API --------------------------------------------------------

    def refresh_saved_session(self) -> None:
        """Snapshot the (now freshly used) session for the next run."""
        self._save_cookies()

    def get_html(self, url: str) -> str:
        resp = self.ctx.request.get(url)
        if not resp.ok:
            raise RuntimeError(f"GET {url} -> HTTP {resp.status}")
        return resp.text()

    def get_raw(self, url: str):
        """Return the APIResponse for a URL (follows redirects)."""
        return self.ctx.request.get(url, max_redirects=10)
