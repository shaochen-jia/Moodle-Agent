"""A small record of what each sync did.

Background syncs have no console, so without this the user has no way to see
what happened. The GUI reads it back on start-up; the file is also readable
on its own.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

MAX_RUNS = 40


def _path() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA", Path.home() / ".local"))
    d = base / "moodle-downloader"
    d.mkdir(parents=True, exist_ok=True)
    return d / "history.json"


def record(status: str, new_files: int = 0, detail: str = "") -> None:
    """status: 'ok' | 'login-needed' | 'error'"""
    try:
        runs = load()
        runs.append({
            "at": time.time(),
            "status": status,
            "new_files": new_files,
            "detail": detail[:300],
        })
        _path().write_text(json.dumps(runs[-MAX_RUNS:]), encoding="utf-8")
    except Exception:
        pass  # history is a convenience, never a blocker


def load() -> list[dict]:
    try:
        return json.loads(_path().read_text(encoding="utf-8"))
    except Exception:
        return []


def last() -> dict | None:
    runs = load()
    return runs[-1] if runs else None


def describe(run: dict) -> str:
    when = time.strftime("%a %H:%M", time.localtime(run.get("at", 0)))
    status = run.get("status")
    if status == "ok":
        n = run.get("new_files", 0)
        what = f"{n} new file{'s' if n != 1 else ''}" if n else "no new files"
        return f"{when} — {what}"
    if status == "login-needed":
        return f"{when} — waiting for you to sign in"
    if status == "cancelled":
        return f"{when} — stopped by you"
    from .problems import diagnose
    return f"{when} — {diagnose(str(run.get('detail', ''))).title}"
