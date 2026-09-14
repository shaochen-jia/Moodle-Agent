from __future__ import annotations

import re
from pathlib import Path

from . import ai, captions, history, lock, structure
from .courses import get_sesskey
from .config import Config, Unit
from .notify import notify
from .downloader import Manifest, repair_names, sanitize, save_response
from .folders import init_folders, unit_dir, week_dir
from . import notes
from .scraper import (ASSESS_MODS, LINK_MODS, Activity, SectionInfo,
                      extract_media_urls, extract_pluginfile_links,
                      find_section_links, match_week, parse_section_page)
from .session import LoginRequired, MoodleSession

# Only files the lecturer attached to the assignment brief - never the
# student's own submissions or marker feedback.
_ASSIGN_FILE_RE = re.compile(r"/mod_assign/(intro|introattachment)/")
_DUE_RE = re.compile(r"<strong>\s*Due:\s*</strong>\s*([^<]+)")

MAX_SECTIONS = 120     # safety cap on crawled section numbers
SEED_SECTIONS = 8      # always try section 0..7 even if not linked anywhere
MAX_FOLLOW_DEPTH = 2   # how far to chase a video through wrapper pages


class YouTubeBudget:
    """How many YouTube caption fetches are left in this sync.

    YouTube blocks by address, so the cap has to span the whole run: counting
    per unit means four units at eight each is the same thirty-two request
    burst the cap exists to prevent.
    """

    def __init__(self, limit: int):
        self.left = limit

    def take(self) -> bool:
        """Claim one fetch, or report that the run is out."""
        if self.left <= 0:
            return False
        self.left -= 1
        return True


def crawl_sections(sess: MoodleSession, cfg: Config, unit: Unit,
                   pending: dict[int, list[str]] | None = None
                   ) -> dict[int, SectionInfo]:
    """Fetch the course page and every section page, following subsection
    links, and merge what we learn about each section."""
    course_url = f"{cfg.base_url}/course/view.php?id={unit.course_id}"
    infos: dict[int, SectionInfo] = {}

    def merge(page_infos: list[SectionInfo], own_number: int | None) -> None:
        for pi in page_infos:
            info = infos.setdefault(pi.number, SectionInfo(number=pi.number))
            info.titles |= pi.titles
            if pi.parent is not None:
                info.parent = pi.parent
            if pi.activities and (pi.number == own_number or not info.activities):
                info.activities = pi.activities

    html = sess.get_html(course_url)
    merge(parse_section_page(html, cfg.base_url), own_number=None)
    queue = set(range(SEED_SECTIONS)) | find_section_links(html, unit.course_id)
    visited: set[int] = set()

    while queue:
        n = queue.pop()
        visited.add(n)
        try:
            page = sess.get_html(f"{course_url}&section={n}")
        except Exception:
            continue  # hidden / orphaned section numbers 404 - that's fine
        queue |= {k for k in find_section_links(page, unit.course_id)
                  if k not in visited and k <= MAX_SECTIONS}
        merge(parse_section_page(page, cfg.base_url), own_number=n)

    _add_missing_from_course_map(sess, cfg, unit, infos, pending)
    return infos


# Activity types worth opening or downloading, by Moodle plugin name
_WANTED_MODS = {"resource": "resource", "folder": "folder", "page": "page",
                "url": "url", "book": "page", "lesson": "page"}


def _add_missing_from_course_map(sess: MoodleSession, cfg: Config, unit: Unit,
                                 infos: dict[int, SectionInfo],
                                 pending: dict[int, list[str]] | None = None
                                 ) -> None:
    """Fill in activities the rendered page never showed.

    An activity that is not open yet is drawn as plain text with no link, so
    parsing the markup cannot see it. Moodle's own structure call still lists
    it, which is how we learn that it exists at all.
    """
    try:
        sesskey = get_sesskey(sess, cfg.base_url)
        cmap = structure.fetch_course_map(sess, cfg.base_url, sesskey,
                                          unit.course_id)
    except Exception:
        cmap = None
    if not cmap:
        return

    seen: set[str] = set()
    for info in infos.values():
        for act in info.activities:
            m = re.search(r"view\.php\?id=(\d+)", act.url)
            if m:
                seen.add(m.group(1))

    added = 0
    for sec in cmap.sections.values():
        if sec.number < 0 or sec.number not in infos:
            continue
        for mod in cmap.modules_in(sec):
            kind = _WANTED_MODS.get(mod.modname)
            if not kind or not mod.url or mod.id in seen:
                continue
            if not mod.user_visible:
                # It exists but is not open yet - a release date, usually.
                # Worth naming so the week note can say it is coming.
                if pending is not None:
                    pending.setdefault(sec.number, []).append(mod.name)
                continue
            infos[sec.number].activities.append(
                Activity(name=mod.name, url=mod.url, mod=kind))
            added += 1
    if added:
        print(f"  [structure] {added} item(s) found via Moodle's course map")


def week_of(infos: dict[int, SectionInfo], number: int, cfg: Config) -> int | None:
    """Week of a section: its own title, or the nearest ancestor's."""
    seen: set[int] = set()
    n: int | None = number
    while n is not None and n not in seen:
        seen.add(n)
        info = infos.get(n)
        if info is None:
            return None
        week = match_week(info.title, cfg.section_patterns, cfg.weeks)
        if week is not None:
            return week
        n = info.parent
    return None


def _download_activity(sess: MoodleSession, cfg: Config, manifest: Manifest,
                       act: Activity, dest: Path,
                       videos: list[tuple[str, str]] | None = None) -> list[str]:
    """Download one activity into dest. Returns list of new file paths.

    Recordings are not downloaded; their addresses are collected instead so
    the transcript step can turn them into text.
    """
    new: list[str] = []

    def is_recording(url: str, name: str = "") -> bool:
        if cfg.download_videos:
            return False
        return captions.is_video_file(url) or captions.is_video_file(name)

    if act.mod == "folder":
        html = sess.get_html(act.url)
        for name, url in extract_pluginfile_links(html, cfg.base_url):
            if is_recording(url, name):
                if videos is not None:
                    videos.append((name or act.name, url))
                continue
            if manifest.has(url):
                continue
            resp = sess.get_raw(url)
            if resp.ok:
                p = save_response(resp, dest, cfg, manifest, url)
                if p:
                    new.append(str(p))
        return new

    if is_recording(act.url, act.name):
        if videos is not None:
            videos.append((act.name, act.url))
        return new

    # resource / pluginfile
    if manifest.has(act.url):
        return new
    resp = sess.get_raw(act.url)
    if not resp.ok:
        print(f"    ! HTTP {resp.status} for {act.name}")
        return new
    ctype = resp.headers.get("content-type", "")
    if "text/html" in ctype:
        # resource rendered as an embed page - find the real file link inside
        links = extract_pluginfile_links(resp.text(), cfg.base_url)
        if not links:
            return new
        _, file_url = links[0]
        if is_recording(file_url, act.name):
            if videos is not None:
                videos.append((act.name, file_url))
            return new
        if manifest.has(file_url):
            existing = manifest.data[manifest.key(file_url)]["path"]
            manifest.add(act.url, Path(existing), 0)  # remember the view url too
            return new
        resp = sess.get_raw(file_url)
        if not resp.ok:
            return new
        p = save_response(resp, dest, cfg, manifest, file_url)
        if p:
            manifest.add(act.url, p, 0)
            new.append(str(p))
        return new

    p = save_response(resp, dest, cfg, manifest, act.url)
    if p:
        new.append(str(p))
    return new


def _sync_assessments(sess: MoodleSession, cfg: Config, manifest: Manifest,
                      unit: Unit,
                      assessments: list[tuple[Activity, str]],
                      collected: list[notes.Assessment] | None = None) -> int:
    """Download assignment briefs and write an index of all assessments."""
    if not cfg.assignments_folder or not assessments:
        return 0

    # De-duplicate (the same assignment is often listed in several sections);
    # keep external (lti) tools only when they sit in an assessment section.
    seen: set[str] = set()
    kept: list[Activity] = []
    for act, section_title in assessments:
        key = act.url.split("#")[0]
        if key in seen:
            continue
        if act.mod == "lti" and "assess" not in section_title.lower():
            continue
        seen.add(key)
        kept.append(act)
    if not kept:
        return 0

    assign_root = unit_dir(cfg, unit.code) / cfg.assignments_folder
    total_new = 0
    index_lines = ["Assessments found on Moodle for " + unit.code,
                   "(auto-generated by Moodle Downloader on every sync - "
                   "do not edit)", ""]

    for act in kept:
        due = ""
        if act.mod == "assign":
            try:
                html = sess.get_html(act.url)
            except Exception as e:
                print(f"    ! {act.name}: {e}")
                html = ""
            m = _DUE_RE.search(html)
            due = m.group(1).strip() if m else ""
            dest = assign_root / sanitize(act.name)
            for _, url in extract_pluginfile_links(html, cfg.base_url):
                if not _ASSIGN_FILE_RE.search(url) or manifest.has(url):
                    continue
                resp = sess.get_raw(url)
                if resp.ok:
                    p = save_response(resp, dest, cfg, manifest, url)
                    if p:
                        print(f"    + {_rel(cfg, p)}")
                        total_new += 1
        index_lines.append(f"[{act.mod}] {act.name}")
        if due:
            index_lines.append(f"    Due: {due}")
        index_lines.append(f"    {act.url}")
        index_lines.append("")
        if collected is not None:
            collected.append(notes.Assessment(name=act.name, due=due,
                                              url=act.url, mod=act.mod))

    assign_root.mkdir(parents=True, exist_ok=True)
    (assign_root / "Assessments.txt").write_text(
        "\n".join(index_lines), encoding="utf-8")
    print(f"  [assessments] {len(kept)} item(s) indexed in "
          f"{cfg.assignments_folder}/Assessments.txt")
    return total_new


def _rel(cfg: Config, path) -> str:
    """Show paths relative to the download root - keeps usernames and
    personal folder layouts out of shareable console output."""
    try:
        return str(Path(path).relative_to(cfg.root_dir))
    except ValueError:
        return str(path)




def sync_unit(sess: MoodleSession, cfg: Config, manifest: Manifest,
              unit: Unit, cancel=None,
              yt_budget: YouTubeBudget | None = None) -> int:
    print(f"\n=== {unit.code} "
          f"({cfg.base_url}/course/view.php?id={unit.course_id}) ===")
    pending: dict[int, list[str]] = {}
    infos = crawl_sections(sess, cfg, unit, pending)
    if not infos:
        print("  ! No sections found - the page layout may be unusual.")
        return 0

    total_new = 0
    assessments: list[tuple[Activity, str]] = []
    week_links: dict[int, list[tuple[str, str]]] = {}
    week_videos: dict[int, list[tuple[str, str]]] = {}
    for n in sorted(infos):
        info = infos[n]
        files = [a for a in info.activities
                 if a.mod not in ASSESS_MODS + LINK_MODS + ("media",)]
        assessments += [(a, info.title) for a in info.activities
                        if a.mod in ASSESS_MODS]
        links = [a for a in info.activities if a.mod in LINK_MODS + ("media",)]
        if links:
            wk = week_of(infos, n, cfg)
            if wk is not None:
                collected = []
                for a in links:
                    collected.append((a.name, a.url))
                    # A page or link activity is often just a wrapper around
                    # an embedded recording - open it and take what is inside.
                    if a.mod in ("page", "url"):
                        collected += _open_page(sess, a)
                week_links.setdefault(wk, []).extend(collected)
        if not files:
            continue
        week = week_of(infos, n, cfg)
        if week is not None:
            dest = week_dir(cfg, unit.code, week)
            label = f"Week {week}"
        elif cfg.unmatched_folder:
            dest = unit_dir(cfg, unit.code) / cfg.unmatched_folder
            label = "other"
        else:
            continue
        print(f"  [{label}] {info.title} - {len(files)} item(s)")
        for act in files:
            _check(cancel)
            found_videos: list[tuple[str, str]] = []
            try:
                new = _download_activity(sess, cfg, manifest, act, dest,
                                         found_videos)
            except Exception as e:
                print(f"    ! {act.name}: {e}")
                continue
            if found_videos and week is not None:
                week_videos.setdefault(week, []).extend(found_videos)
            for p in new:
                print(f"    + {_rel(cfg, p)}")
            total_new += len(new)

    found: list[notes.Assessment] = []
    try:
        total_new += _sync_assessments(sess, cfg, manifest, unit, assessments,
                                       found)
    except Exception as e:
        print(f"  ! assessments: {e}")

    no_captions: dict[int, list[tuple[str, str]]] = {}
    if cfg.transcripts:
        try:
            total_new += _sync_transcripts(sess, cfg, manifest, unit,
                                           week_links, no_captions,
                                           week_videos, yt_budget)
        except Exception as e:
            print(f"  ! transcripts: {e}")

    _report_skipped(no_captions)

    # Pending items are collected per section; the notes are per week.
    pending_by_week: dict[int, list[str]] = {}
    for section_number, names in pending.items():
        wk = week_of(infos, section_number, cfg)
        if wk is not None:
            pending_by_week.setdefault(wk, []).extend(names)

    if cfg.weekly_notes:
        try:
            written = _write_week_notes(cfg, unit, week_links, found,
                                        no_captions, pending_by_week)
            for p in written:
                print(f"    ~ {_rel(cfg, p)}")
        except Exception as e:
            print(f"  ! weekly notes: {e}")
    return total_new


def _open_page(sess: MoodleSession, act: Activity, depth: int = 0,
               seen: set[str] | None = None) -> list[tuple[str, str]]:
    """Recordings reached through a Moodle activity.

    A page or link activity is often just a wrapper: the page holds an
    embedded player, or the link bounces to another Moodle page that does.
    Following one level is not enough, so this recurses - with a small depth
    limit and a visited set, because course pages do link back to each other.
    """
    seen = seen if seen is not None else set()
    if depth > MAX_FOLLOW_DEPTH or act.url in seen:
        return []
    seen.add(act.url)
    try:
        html = sess.get_html(act.url)
    except Exception:
        return []

    found = extract_media_urls(html)
    out = [(act.name if len(found) == 1 else f"{act.name} — {label}", url)
           for label, url in found]

    # Nothing playable here: follow any Moodle activity this one points to.
    if not found and depth < MAX_FOLLOW_DEPTH:
        for m in re.finditer(r'href="([^"]*/mod/(?:page|url|resource|book|'
                             r'lesson)/view\.php\?id=\d+)"', html):
            nxt = m.group(1).replace("&amp;", "&")
            if nxt not in seen:
                out += _open_page(sess, Activity(name=act.name, url=nxt,
                                                 mod="page"), depth + 1, seen)
    return out


def _sync_transcripts(sess: MoodleSession, cfg: Config, manifest: Manifest,
                      unit: Unit,
                      week_links: dict[int, list[tuple[str, str]]],
                      no_captions: dict[int, list[tuple[str, str]]],
                      week_videos: dict[int, list[tuple[str, str]]] | None = None,
                      yt_budget: YouTubeBudget | None = None) -> int:
    """Save captions for the week's recordings as readable transcripts.

    Recordings that turn out to have no captions are reported back so the
    week note can still name them.
    """
    panopto: captions.PanoptoClient | None = None
    summariser = ai.Summariser(cfg)
    new = 0
    if yt_budget is None:  # a one-unit run still gets the full allowance
        yt_budget = YouTubeBudget(cfg.max_youtube_per_sync)
    for week, links in sorted(week_links.items()):
        dest = week_dir(cfg, unit.code, week)
        for label, url in links:
            ids = captions.panopto_ids(url)
            vid = captions.youtube_id(url) if not ids else None
            if not ids and not vid:
                continue
            key = f"transcript:{ids[1] if ids else vid}"
            if manifest.has(key):
                continue
            if ids:
                if panopto is None:
                    panopto = captions.PanoptoClient(sess)
                got, why = panopto.transcript(*ids)
                if not got:
                    no_captions.setdefault(week, []).append(
                        (label, url, why))
                    continue
                title, text = got
                source = "Panopto"
            else:
                if not yt_budget.take():
                    # Asking YouTube for many transcripts in one burst is what
                    # gets an address blocked; the rest wait for the next run.
                    no_captions.setdefault(week, []).append(
                        (label, url, captions.DEFERRED))
                    continue
                title = captions.youtube_title(vid) or f"{label} ({vid})"
                text, reason = captions.youtube_transcript(vid)
                source = "YouTube"
                if not text and reason != captions.NONE \
                        and summariser.can_transcribe:
                    # Rate-limited or unreachable: let the AI read the video
                    # from its own side instead of ours.
                    try:
                        text = summariser.transcribe_youtube(url)
                        source = "YouTube (read by AI)"
                    except ai.AIError as e:
                        print(f"    ! {title}: {e}")
                if not text:
                    no_captions.setdefault(week, []).append(
                        (label, url, reason))
                    continue
            summary = ""
            if summariser and summariser.enabled:
                try:
                    summary = summariser.summarise(title, text)
                except ai.AIError as e:
                    print(f"    ! summary for {title}: {e}")
            path = captions.save_transcript(dest, title, source, url, text,
                                            summary, cfg.note_formats)
            manifest.add(key, path, path.stat().st_size)
            print(f"    + {_rel(cfg, path)}  ({len(text):,} chars)")
            new += 1

    new += _transcribe_moodle_videos(sess, cfg, manifest, unit, summariser,
                                     no_captions, week_videos or {})

    # Transcripts saved before the AI was configured, or while the allowance
    # was used up, still deserve a summary.
    waiting = 0
    if summariser.enabled:
        for week in cfg.weeks:
            for path in captions.transcripts_without_summary(
                    week_dir(cfg, unit.code, week)):
                if not summariser.available:
                    waiting += 1
                    continue
                try:
                    body = path.read_text(encoding="utf-8")
                    summary = summariser.summarise(path.stem, body)
                except ai.QuotaExhausted as e:
                    print(f"    ! {e}")
                    waiting += 1
                    continue
                except (OSError, ai.AIError) as e:
                    print(f"    ! summary for {path.stem}: {e}")
                    continue
                if summary:
                    captions.add_summary(path, summary, cfg.note_formats)
                    print(f"    ~ summary added to {_rel(cfg, path)}")
    if waiting:
        print(f"  {waiting} transcript(s) still need a summary - the AI "
              f"allowance is used up for now. They are filled in "
              f"automatically on later syncs; the full text is already saved.")
    return new


def _report_skipped(no_captions: dict[int, list]) -> None:
    """Say plainly which recordings produced no text, and why.

    Without this the only trace is a line in a week note, and a student has
    no way to tell a permanent gap from one that fixes itself.
    """
    counts: dict[str, int] = {}
    for items in no_captions.values():
        for item in items:
            reason = item[2] if len(item) > 2 else captions.NONE
            counts[reason] = counts.get(reason, 0) + 1
    if not counts:
        return
    total = sum(counts.values())
    print(f"  {total} recording(s) produced no transcript:")
    for reason, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"    - {n} x {captions.reason_text(reason)}")
    if any(r in captions.RETRYABLE for r in counts):
        print("    These are picked up automatically on a later sync; "
              "press Sync now to try again sooner.")


def _transcribe_moodle_videos(sess: MoodleSession, cfg: Config,
                              manifest: Manifest, unit: Unit,
                              summariser: "ai.Summariser",
                              no_captions: dict,
                              week_videos: dict[int, list[tuple[str, str]]]
                              ) -> int:
    """Handle recordings Moodle hosts itself.

    These carry no caption API. If staff attached a subtitle file we use it;
    otherwise the recording is fetched to a temporary file, read by the AI,
    and deleted - the video itself is never kept.
    """
    new = 0
    for week, items in sorted(week_videos.items()):
        dest = week_dir(cfg, unit.code, week)
        for name, url in items:
            key = f"transcript:moodle:{url.split('?')[0]}"
            if manifest.has(key):
                continue
            title = Path(name).stem or "Recording"
            text = ""

            # 1. a subtitle file published beside the recording
            try:
                page_html = sess.get_html(url) if "/mod/" in url else ""
            except Exception:
                page_html = ""
            for track in captions.caption_track_urls(page_html, cfg.base_url):
                r = sess.get_raw(track)
                if r.ok and len(r.text()) > 50:
                    text = captions.vtt_to_text(r.text())
                    break

            # 2. otherwise let the AI listen to it
            why = captions.NONE
            if not text and cfg.transcribe_media:
                if not summariser.can_transcribe:
                    # Reading a video is a capability, not a setting: only
                    # Gemini has it. Reporting "no captions" here would read
                    # as final when switching provider would fetch it.
                    why = captions.NO_READER
                else:
                    try:
                        text = _ai_read_video(sess, cfg, summariser, url)
                    except ai.QuotaExhausted as e:
                        # The allowance ran out, not the captions. Saying "no
                        # captions" here would retire a recording that a later
                        # sync can still read.
                        why = captions.BLOCKED
                        print(f"    ! {title}: {e}")
                    except (ai.AIError, OSError) as e:
                        print(f"    ! {title}: {e}")
            if not text:
                no_captions.setdefault(week, []).append(
                    (name, url, why))
                continue

            summary = ""
            if summariser.available:
                try:
                    summary = summariser.summarise(title, text)
                except ai.AIError as e:
                    print(f"    ! summary for {title}: {e}")
            path = captions.save_transcript(dest, title, "Moodle recording",
                                            url, text, summary,
                                            cfg.note_formats)
            manifest.add(key, path, path.stat().st_size)
            print(f"    + {_rel(cfg, path)}  ({len(text):,} chars)")
            new += 1
    return new


def _ai_read_video(sess: MoodleSession, cfg: Config,
                   summariser: "ai.Summariser", url: str) -> str:
    """Fetch a recording to a temporary file just long enough to read it."""
    resp = sess.get_raw(url)
    if not resp.ok:
        return ""
    blob = resp.body()
    size_mb = len(blob) / (1024 * 1024)
    if size_mb > cfg.max_transcribe_mb:
        print(f"    ! recording is {size_mb:.0f} MB - over the "
              f"{cfg.max_transcribe_mb} MB limit, skipped")
        return ""
    suffix = Path(urlparse(url).path).suffix or ".mp4"
    tmp = Path(tempfile.gettempdir()) / f"moodle-dl-media{suffix}"
    try:
        tmp.write_bytes(blob)
        return summariser.transcribe_file(tmp, _mime_for(suffix))
    finally:
        tmp.unlink(missing_ok=True)


def _mime_for(suffix: str) -> str:
    return {".mp4": "video/mp4", ".mov": "video/quicktime",
            ".webm": "video/webm", ".mkv": "video/x-matroska",
            ".mp3": "audio/mpeg", ".m4a": "audio/mp4",
            ".wav": "audio/wav"}.get(suffix.lower(), "video/mp4")


def _write_week_notes(cfg: Config, unit: Unit,
                      week_links: dict[int, list[tuple[str, str]]],
                      assessments: list[notes.Assessment],
                      no_captions: dict[int, list[tuple[str, str]]] | None = None,
                      pending: dict[int, list[str]] | None = None
                      ) -> list[Path]:
    """Refresh the per-week summary note for every week that has content."""
    written = []
    pending_by_week = pending or {}
    for week in cfg.weeks:
        note = notes.WeekNote(
            unit=unit.code, week=week,
            folder=week_dir(cfg, unit.code, week),
            links=week_links.get(week, []),
            assessments=assessments,
            no_captions=(no_captions or {}).get(week, []),
            pending=pending_by_week.get(week, []),
        )
        path = notes.write_note(cfg, note)
        if path:
            written.append(path)
    return written


class Cancelled(RuntimeError):
    """Raised when the user asks a running sync to stop."""


def _check(cancel) -> None:
    """Stop between items, so cancelling is quick but never leaves a
    half-written file behind."""
    if cancel is not None and cancel.is_set():
        raise Cancelled("Sync cancelled.")


def sync(cfg: Config, headful: bool = False,
         only_units: list[str] | None = None,
         background: bool = False, cancel=None) -> None:
    """Run one sync. `background` adds a desktop notification on new files,
    since an unattended run has no console for the user to read."""
    if not lock.acquire("sync"):
        print("Another sync is already running - skipping this one.")
        return
    try:
        new_files = _sync_locked(cfg, headful, only_units, cancel)
        history.record("ok", new_files)
        if background and new_files:
            notify(f"{new_files} new file{'s' if new_files != 1 else ''} "
                   "downloaded", f"Saved under {cfg.root_dir.name}.")
    except Cancelled:
        print("\nStopped at your request. Anything already downloaded is kept.")
        history.record("cancelled")
        return
    except LoginRequired as e:
        history.record("login-needed", detail=str(e))
        raise
    except Exception as e:
        history.record("error", detail=f"{type(e).__name__}: {e}")
        raise
    finally:
        lock.release("sync")


def _sync_locked(cfg: Config, headful: bool,
                 only_units: list[str] | None, cancel=None) -> int:
    manifest = Manifest(cfg.manifest_path)

    # Before anything is fetched: repair names that an earlier version saved
    # with a mangled separator. Doing it here rather than on download means the
    # files already sitting in the folder stop failing the user's cloud sync.
    for old, new in repair_names(cfg.root_dir, manifest):
        print(f"Renamed a file with a broken name: {old.name} -> {new.name}")

    with MoodleSession(cfg, headful=headful) as sess:
        if cfg.course_selection == "starred":
            from .courses import _CODE_RE, fetch_courses
            starred = fetch_courses(sess, cfg.base_url, "favourites")
            units = [Unit(code=c.unit_code, course_id=c.id)
                     for c in starred if _CODE_RE.search(c.unit_code)]
            print("Starred units: " + (", ".join(u.code for u in units)
                                       or "(none - star some on Moodle!)"))
        else:
            units = cfg.units
        if only_units:
            wanted = {u.upper() for u in only_units}
            units = [u for u in units if u.code.upper() in wanted]
        if not units:
            print("No units to sync - check your config.yaml.")
            return 0
        init_folders(cfg, units)
        print(f"Saving files to: {cfg.root_dir}")

        total = 0
        yt_budget = YouTubeBudget(cfg.max_youtube_per_sync)
        for unit in units:
            _check(cancel)
            total += sync_unit(sess, cfg, manifest, unit, cancel, yt_budget)
        # Refresh the stored session after real work, so the next run starts
        # from a session that is minutes old rather than days old.
        sess.refresh_saved_session()
    print(f"\nDone. {total} new file(s) downloaded.")
    return total

