"""Turn an exception into something a student can act on.

The app is for people who have never opened a terminal, so a failure has to
answer three questions in their words: what happened, what do I do now, and -
only if they go looking - what did the machine actually say.

Kept away from the GUI on purpose. The mapping is the part worth testing, and
it is testable here without opening a window.
"""
from __future__ import annotations

import dataclasses

ISSUES = "https://github.com/shaochen-jia/Moodle-Downloader/issues"


@dataclasses.dataclass
class Problem:
    title: str            # one plain sentence, no jargon
    steps: list[str]      # what to do, most likely fix first
    details: str = ""     # the machine's own words, for a bug report
    retry: bool = True    # whether trying again could plausibly work


def diagnose(exc: BaseException | str) -> Problem:
    """Best explanation for a failure, with what to do about it."""
    details = f"{type(exc).__name__}: {exc}" if isinstance(exc, BaseException) \
        else str(exc)
    low = details.lower()
    name = type(exc).__name__ if isinstance(exc, BaseException) else ""

    if name == "BrowserBusy" or "another part of the app" in low:
        return Problem(
            "The app is already using the browser for its background sync.",
            ["Wait a minute and try again - a sync usually takes under one.",
             "Or turn off Auto-sync on the main screen, then try again.",
             "Only one thing can drive the browser at a time, so this is the "
             "app queueing rather than anything being broken."],
            details)

    if name == "LoginRequired" or "sign-in" in low or "sign in" in low \
            or "log in" in low:
        return Problem(
            "Nobody signed in, so Moodle could not be reached.",
            ["Try again - a browser window will open.",
             "Sign in there with your Monash account, including the phone "
             "approval, exactly as you normally would.",
             "Tick 'Keep me signed in' so you do not have to do this again.",
             "Leave that window alone; it closes by itself when it is done."],
            details)

    if "executable doesn" in low or "playwright install" in low:
        return Problem(
            "The app could not find a browser to sign you in with.",
            ["Install Google Chrome or Microsoft Edge - the app borrows one "
             "of them rather than shipping its own.",
             "Then try again; nothing else needs setting up."],
            details)

    # The profile a browser is opened against takes one process at a time, so
    # this is nearly always the app's own background sync rather than a broken
    # install - and the message Chromium gives for it says neither.
    if any(s in low for s in ("launch_persistent_context", "processsingleton",
                              "singletonlock", "browser has been closed",
                              "could not launch any browser",
                              "browsertype.launch")):
        return Problem(
            "Another copy of the app is already using the browser.",
            ["If Auto-sync is on, a sync is probably running right now - wait "
             "a minute and try again.",
             "Check the taskbar for a second Moodle Downloader window and "
             "close it.",
             "If neither is true, close the app and open it again; a browser "
             "left running invisibly will hold on to this.",
             "Only if that fails: check that Google Chrome or Microsoft Edge "
             "is installed."],
            details)

    if any(s in low for s in ("err_name_not_resolved", "err_internet_disconnected",
                             "err_connection", "err_network", "getaddrinfo",
                             "failed to resolve", "no such host")):
        return Problem(
            "Your computer could not reach Moodle.",
            ["Check you are online - open learning.monash.edu in your normal "
             "browser and see if it loads.",
             "If it loads there but not here, wait a minute and try again.",
             "On a hotel or public network, sign in to that network first."],
            details)

    if "timeout" in low or "timed out" in low:
        return Problem(
            "Moodle took too long to answer.",
            ["Try again - this is usually Moodle being slow, not your setup.",
             "If it keeps happening, check whether Moodle is up in your normal "
             "browser."],
            details)

    if "err_cert" in low or "ssl" in low or "certificate" in low:
        return Problem(
            "The connection to Moodle could not be trusted.",
            ["If you are on a shared or public network, that network may be "
             "intercepting traffic - try again on a different one.",
             "Check that your computer's clock is right; a wrong date breaks "
             "every secure connection."],
            details)

    if "404" in low or "not a moodle" in low or "sesskey" in low:
        return Problem(
            "That address does not look like a Moodle site.",
            ["At Monash the address is https://learning.monash.edu",
             "Check for a typo, and leave off anything after the .edu.",
             "If the address is right, you may have been signed out - try "
             "again and complete the login."],
            details)

    if name in ("PermissionError", "OSError") and (
            "permission" in low or "winerror 5" in low or "access is denied" in low):
        return Problem(
            "Windows would not let the app use that folder.",
            ["Pick a folder inside your own user folder - Desktop or Documents "
             "is fine.",
             "Avoid the Program Files folder; Windows protects it.",
             "If the folder is on OneDrive, wait for OneDrive to finish "
             "syncing and try again."],
            details)

    if "no space" in low or "enospc" in low or "disk full" in low:
        return Problem(
            "The drive is full.",
            ["Free up some space and try again.",
             "Course files for a semester need roughly one gigabyte."],
            details)

    return Problem(
        "Something went wrong, and the app has no specific advice for this one.",
        ["Try again - a good share of these do not come back.",
         "If it keeps happening, close the app and open it again.",
         f"Still stuck? Copy the details below into a new issue at {ISSUES} - "
         "that text is what makes it fixable."],
        details)
