from __future__ import annotations

import dataclasses
import json
import re

from .session import MoodleSession

_AJAX_METHOD = "core_course_get_enrolled_courses_by_timeline_classification"
_CODE_RE = re.compile(r"\b[A-Z]{2,4}\d{4}\b")


@dataclasses.dataclass
class Course:
    id: int
    fullname: str
    shortname: str
    startdate: int
    starred: bool

    @property
    def unit_code(self) -> str:
        m = _CODE_RE.search(self.fullname) or _CODE_RE.search(self.shortname)
        if m:
            return m.group(0)
        safe = re.sub(r"[^A-Za-z0-9_-]+", "_", self.shortname or self.fullname)
        return safe[:30] or f"course{self.id}"


def get_sesskey(sess: MoodleSession, base_url: str) -> str:
    html = sess.get_html(f"{base_url}/my/")
    m = re.search(r'"sesskey":"([^"]+)"', html)
    if not m:
        raise RuntimeError("Could not find sesskey - are you logged in?")
    return m.group(1)


def fetch_courses(sess: MoodleSession, base_url: str,
                  classification: str = "all") -> list[Course]:
    """Enrolled courses via the same AJAX the Moodle dashboard uses.

    classification: "all", "inprogress", "past", "favourites" (= starred), ...
    """
    sesskey = get_sesskey(sess, base_url)
    payload = [{
        "index": 0,
        "methodname": _AJAX_METHOD,
        "args": {"offset": 0, "limit": 0,
                 "classification": classification, "sort": "fullname"},
    }]
    resp = sess.ctx.request.post(
        f"{base_url}/lib/ajax/service.php?sesskey={sesskey}&info={_AJAX_METHOD}",
        data=json.dumps(payload),
        headers={"Content-Type": "application/json"},
    )
    if not resp.ok:
        raise RuntimeError(f"Course list request failed: HTTP {resp.status}")
    body = resp.json()[0]
    if body.get("error"):
        raise RuntimeError(f"Course list request failed: {body}")
    return [
        Course(
            id=int(c["id"]),
            fullname=c.get("fullname", "").strip(),
            shortname=c.get("shortname", "").strip(),
            startdate=int(c.get("startdate", 0)),
            starred=bool(c.get("isfavourite", False)),
        )
        for c in body["data"]["courses"]
    ]

# How many courses to offer on a first-ever setup, before there is anything
# configured to go on.
FIRST_RUN_LIMIT = 8


def preselect(courses: list, configured: set[str]) -> list[bool]:
    """Which courses the setup screen should show as already chosen.

    The saved configuration wins whenever there is one. Deciding from Moodle's
    stars every time meant reopening this screen re-proposed every starred
    course - including ones deliberately taken out - and pressing Finish put
    them straight back. That is how a unit from an earlier semester kept
    reappearing weeks after it was removed.
    """
    if configured:
        return [c.unit_code.upper() in configured for c in courses]
    return [bool(c.starred) and i < FIRST_RUN_LIMIT
            for i, c in enumerate(courses)]
