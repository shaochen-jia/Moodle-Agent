from __future__ import annotations

import dataclasses
import os
import shutil
from pathlib import Path

import yaml


def settings_dir() -> Path:
    """One place for settings, whatever started the app.

    Keeping config.yaml beside the executable meant the packaged app and a
    source checkout each had their own copy, which silently drifted apart -
    and moving the exe lost every setting. Both now read the same file.
    """
    base = Path(os.environ.get("LOCALAPPDATA", Path.home() / ".local"))
    d = base / "moodle-downloader"
    d.mkdir(parents=True, exist_ok=True)
    return d


def default_config_path(app_dir: Path | None = None) -> Path:
    """The settings file, migrating an older beside-the-app one if found."""
    path = settings_dir() / "config.yaml"
    if not path.exists() and app_dir:
        legacy = app_dir / "config.yaml"
        if legacy.exists():
            shutil.copy2(legacy, path)
    return path


@dataclasses.dataclass
class Unit:
    code: str
    course_id: int


@dataclasses.dataclass
class Config:
    base_url: str
    root_dir: Path
    week_start: int
    week_end: int
    units: list[Unit]
    section_patterns: list[str]
    unmatched_folder: str
    skip_extensions: list[str]
    config_dir: Path
    course_selection: str = "manual"  # "manual" | "starred"
    assignments_folder: str = "Assignments"  # "" disables assignment capture
    sync_interval_hours: float = 3.0  # auto-sync repeat interval
    weekly_notes: bool = True  # write a Week NN Summary note per week
    # Word first, plain text second: Markdown is unfamiliar to most readers.
    note_formats: tuple[str, ...] = ("docx", "txt")
    transcripts: bool = True  # save captions of lecture recordings
    download_videos: bool = False  # keep the video files themselves
    transcribe_media: bool = True  # let the AI read recordings with no captions
    max_transcribe_mb: int = 300  # skip recordings larger than this
    # YouTube rate-limits an address that asks for many transcripts at once,
    # so a first sync spreads them over several runs instead of bursting.
    max_youtube_per_sync: int = 8
    # Optional AI summaries - everything works without these
    ai_provider: str = ""     # gemini | anthropic | openai | deepseek | ...
    ai_api_key: str = ""
    ai_model: str = ""        # blank uses the provider's default
    ai_base_url: str = ""     # blank uses the provider's default

    @property
    def weeks(self) -> range:
        return range(self.week_start, self.week_end + 1)

    @property
    def session_dir(self) -> Path:
        # Keep the browser profile (cookies!) out of the project folder so it
        # never ends up in git or cloud-synced directories like OneDrive.
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / ".local"))
        return base / "moodle-downloader" / "session"

    @property
    def manifest_path(self) -> Path:
        return self.root_dir / ".manifest.json"


def load_config(path: str | Path) -> Config:
    path = Path(path).resolve()
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    units = [Unit(code=str(u["code"]), course_id=int(u["course_id"]))
             for u in raw.get("units", [])]

    root_dir = Path(str(raw.get("root_dir", "./MoodleFiles")))
    if not root_dir.is_absolute():
        root_dir = (path.parent / root_dir).resolve()

    return Config(
        base_url=str(raw.get("base_url", "https://learning.monash.edu")).rstrip("/"),
        root_dir=root_dir,
        week_start=int(raw.get("week_start", 0)),
        week_end=int(raw.get("week_end", 12)),
        units=units,
        section_patterns=list(raw.get("section_patterns",
                                      [r"week\s*0*{week}\b", r"topic\s*0*{week}\b"])),
        unmatched_folder=str(raw.get("unmatched_folder", "_Other")),
        skip_extensions=[e.lower() for e in raw.get("skip_extensions", []) or []],
        config_dir=path.parent,
        course_selection=str(raw.get("course_selection", "manual")),
        assignments_folder=str(raw.get("assignments_folder", "Assignments")),
        sync_interval_hours=float(raw.get("sync_interval_hours", 3)),
        weekly_notes=bool(raw.get("weekly_notes", True)),
        note_formats=tuple(
            f for f in (raw.get("note_formats") or ["docx", "txt"])
            if f in ("docx", "txt", "md")) or ("docx", "txt"),
        transcripts=bool(raw.get("transcripts", True)),
        download_videos=bool(raw.get("download_videos", False)),
        transcribe_media=bool(raw.get("transcribe_media", True)),
        max_transcribe_mb=int(raw.get("max_transcribe_mb", 300)),
        max_youtube_per_sync=int(raw.get("max_youtube_per_sync", 8)),
        ai_provider=str(raw.get("ai_provider", "") or ""),
        ai_api_key=str(raw.get("ai_api_key", "") or ""),
        ai_model=str(raw.get("ai_model", "") or ""),
        ai_base_url=str(raw.get("ai_base_url", "") or ""),
    )

