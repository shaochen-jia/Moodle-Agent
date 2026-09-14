from __future__ import annotations

import json
import os
import re
from pathlib import Path
from urllib.parse import unquote, urlparse

from .config import Config

_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f-\x9f]')

# Text that was sent as UTF-8 and decoded as Latin-1 keeps every original byte,
# one byte per character, so the damage is exactly reversible. A lead byte in
# C2-F4 followed by a continuation byte in 80-BF is the signature; no real
# filename puts those two next to each other.
_MOJIBAKE = re.compile("[\u00c2-\u00f4][\u0080-\u00bf]")


def demojibake(s: str) -> str:
    """Undo UTF-8 text that arrived decoded as Latin-1.

    HTTP header values are Latin-1 by specification, so a Content-Disposition
    filename that Moodle sent as UTF-8 comes back one character per byte: an
    en dash turns into 'a' followed by two C1 control characters. Explorer
    draws those controls as nothing, so the name looks merely odd - but
    OneDrive and SharePoint reject it outright, which is how this surfaced:
    the same few files failing to sync every day, looking almost fine.
    """
    if not _MOJIBAKE.search(s):
        return s
    for _ in range(2):  # a doubly-encoded name needs a second pass
        try:
            s = s.encode("latin-1").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            break  # mixed with real text; the illegal-character pass takes it
        if not _MOJIBAKE.search(s):
            break
    return s


def clean_name(name: str) -> str:
    """Repair the encoding and drop characters a filesystem cannot hold.

    Kept apart from `sanitize` because this half is safe to run over files
    already on disk: it never shortens a name, so it only ever changes a name
    that is genuinely broken.
    """
    # Windows rejects a trailing dot or space; a *leading* dot is legitimate,
    # and stripping it renamed .manifest.json out from under the sync.
    return _ILLEGAL.sub("_", demojibake(name)).rstrip(" .").lstrip(" ")


def _long(p: Path) -> Path:
    """Windows extended-length form so paths beyond 260 chars still work."""
    s = str(p)
    if os.name == "nt" and not s.startswith("\\\\?\\") and len(s) > 240:
        return Path("\\\\?\\" + s)
    return p


def sanitize(name: str, limit: int = 150) -> str:
    """Make a filename safe and short enough, without losing its extension.

    Trimming blindly to a length cut the suffix off a long name, which leaves
    Windows with a file it will not open - and hides the extension from the
    skip-list and video checks that read it back.
    """
    name = clean_name(name)
    if len(name) <= limit:
        return name or "file"
    stem, dot, suffix = name.rpartition(".")
    if dot and stem and 0 < len(suffix) <= 10:
        keep = max(1, limit - len(suffix) - 1)
        name = f"{stem[:keep].strip(' .')}.{suffix}"
    else:
        name = name[:limit]
    return name.strip(" ") or "file"


def filename_from_response(resp, fallback_url: str) -> str:
    cd = resp.headers.get("content-disposition", "")
    m = re.search(r"filename\*=UTF-8''([^;]+)", cd)
    if m:
        return sanitize(unquote(m.group(1)))
    m = re.search(r'filename="?([^";]+)"?', cd)
    if m:
        return sanitize(m.group(1))
    path = urlparse(resp.url or fallback_url).path
    return sanitize(unquote(path.rsplit("/", 1)[-1]))


class Manifest:
    """Tracks what has already been downloaded so re-runs only fetch new files."""

    def __init__(self, path: Path):
        self.path = path
        self.data: dict[str, dict] = {}
        if path.exists():
            try:
                self.data = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                self.data = {}

    def key(self, url: str) -> str:
        return url.split("?")[0] if "pluginfile.php" in url else url

    def has(self, url: str) -> bool:
        entry = self.data.get(self.key(url))
        if not isinstance(entry, dict) or not entry.get("path"):
            return False  # missing or hand-edited entry: fetch it again
        # If the file was deleted locally, download it again.
        return _long(Path(entry["path"])).exists()

    def add(self, url: str, path: Path, size: int) -> None:
        self.data[self.key(url)] = {"path": str(path), "size": size}
        self.save()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=2, ensure_ascii=False),
                             encoding="utf-8")


def repair_names(root: Path, manifest: Manifest) -> list[tuple[Path, Path]]:
    """Rename anything already saved under a broken name, and move the
    manifest's record with it.

    Repairing the download path only helps files fetched from here on. The
    names already on disk keep failing the user's cloud sync every day, and
    renaming one by hand does not help either: the manifest still points at the
    old path, finds nothing there, and downloads the file again under the same
    broken name. So the rename has to happen here, where the record can follow.

    Only the encoding and illegal characters are repaired - never the length -
    so a name that is merely long is left exactly as it is.
    """
    if not root.exists():
        return []

    # Deepest first, so a folder is renamed only once its contents are done.
    renames: list[tuple[Path, Path]] = []
    for p in sorted(root.rglob("*"), key=lambda q: len(q.parts), reverse=True):
        if p == manifest.path:
            continue  # the sync's own record is not one of the user's files
        fixed = clean_name(p.name)
        if not fixed or fixed == p.name:
            continue
        target = p.with_name(fixed)
        if _long(target).exists():
            continue  # something already sits there; leave both alone
        try:
            _long(p).rename(_long(target))
        except OSError as e:
            print(f"Could not rename {p.name}: {e}")
            continue
        renames.append((p, target))

    if not renames:
        return []

    changed = False
    for entry in manifest.data.values():
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            continue
        path = entry["path"]
        for old, new in renames:  # applied in the same order, so nesting works
            o, n = str(old), str(new)
            if path == o:
                path = n
            elif path.startswith(o + os.sep):
                path = n + path[len(o):]
        if path != entry["path"]:
            entry["path"] = path
            changed = True
    if changed:
        manifest.save()
    return renames


def unique_path(directory: Path, filename: str) -> Path:
    p = directory / filename
    stem, suffix = p.stem, p.suffix
    n = 1
    while _long(p).exists():
        p = directory / f"{stem} ({n}){suffix}"
        n += 1
    return p


def save_response(resp, directory: Path, cfg: Config,
                  manifest: Manifest, source_url: str) -> Path | None:
    """Write a binary response to disk; returns the path or None if skipped."""
    filename = filename_from_response(resp, source_url)
    ext = Path(filename).suffix.lower()
    if cfg.skip_extensions and ext in cfg.skip_extensions:
        return None
    # Recordings are wanted as text, not as gigabytes of video.
    from .captions import VIDEO_EXTS
    if not cfg.download_videos and ext in VIDEO_EXTS:
        return None
    _long(directory).mkdir(parents=True, exist_ok=True)
    body = resp.body()

    # If this exact file is already on disk, adopt it instead of writing a
    # second copy. Without this, a lost or damaged manifest would refill the
    # folders with "name (1).pdf" duplicates.
    target = directory / filename
    if _long(target).exists():
        try:
            if _long(target).stat().st_size == len(body) \
                    and _long(target).read_bytes() == body:
                manifest.add(source_url, target, len(body))
                return None
        except OSError:
            pass

    path = unique_path(directory, filename)
    _long(path).write_bytes(body)
    manifest.add(source_url, path, len(body))
    return path
