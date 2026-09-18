from __future__ import annotations

import os
from pathlib import Path

from .config import Config


def week_folder_name(week: int) -> str:
    return f"Week {week:02d}"


def unit_dir(cfg: Config, unit_code: str) -> Path:
    return cfg.root_dir / unit_code


def week_dir(cfg: Config, unit_code: str, week: int) -> Path:
    return unit_dir(cfg, unit_code) / week_folder_name(week)


def init_folders(cfg: Config, units=None) -> list[Path]:
    """Create root/<UNIT>/Week 00..Week NN for every unit."""
    created: list[Path] = []
    for unit in (units if units is not None else cfg.units):
        for week in cfg.weeks:
            d = week_dir(cfg, unit.code, week)
            if not d.exists():
                d.mkdir(parents=True, exist_ok=True)
                created.append(d)
        if cfg.unmatched_folder:
            d = unit_dir(cfg, unit.code) / cfg.unmatched_folder
            if not d.exists():
                d.mkdir(parents=True, exist_ok=True)
                created.append(d)
    return created

# Folders that belong to the student's own work rather than to the unit. A
# software engineering unit's folder can hold a git checkout and a virtualenv,
# which is thousands of files nobody downloaded.
NOT_COURSEWORK = {".git", ".venv", "venv", "node_modules", "__pycache__",
                  ".idea", ".vscode", "site-packages"}


def count_files(unit_dir: Path) -> int:
    """How many course files are in a unit folder.

    Walked rather than globbed, and counted per directory rather than
    all-or-nothing: one unreadable path used to abandon the whole count and
    report zero. A `.venv/lib64` symlink Windows refuses to follow did exactly
    that, so the unit with the most work in it showed `0 files`.
    """
    total = 0
    try:
        for _root, dirs, files in os.walk(unit_dir):
            dirs[:] = [d for d in dirs
                       if d not in NOT_COURSEWORK and not d.startswith(".")]
            total += sum(1 for name in files if not name.startswith("."))
    except OSError:
        pass  # a partial count is worth more than a zero
    return total
