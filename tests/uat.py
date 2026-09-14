"""Acceptance checks for the behaviours that matter and the ways they break.

Run it from anywhere:

    python tests/uat.py

Exits non-zero if anything fails, so a hook or a CI step can gate on it.

Two rules keep this suite worth running. It touches **nothing real**:
LOCALAPPDATA is redirected at import time, so the lock file, the settings
directory and the sync history all land in a temp folder rather than in the
copy of the app you actually use. And it needs **no network**: the one check
that used to call a provider for real now stubs the transport, because a suite
that only passes online is a suite nobody runs.

Most of what is here was written after something broke. The comments say which
failure each check is standing guard over — that is the part worth keeping.
"""
from __future__ import annotations

import io
import json
import os
import re
import shutil
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

# Redirect the per-user data directory before anything imports it. moodle_dl
# resolves LOCALAPPDATA at call time, so setting it here is enough to keep the
# suite out of the real app's lock file, settings and history. Without it, the
# history checks below write three imaginary syncs that the GUI then displays
# as though they had happened.
TMP = Path(tempfile.mkdtemp(prefix="uat_"))
os.environ["LOCALAPPDATA"] = str(TMP / "appdata")

from moodle_dl import ai, captions, history, lock, notes  # noqa: E402
from moodle_dl.config import Config, load_config  # noqa: E402
from moodle_dl.downloader import (Manifest, demojibake,  # noqa: E402
                                  repair_names, sanitize, save_response)
from moodle_dl.main import YouTubeBudget  # noqa: E402
from moodle_dl.scraper import extract_media_urls, match_week  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: object, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    mark = "PASS" if cond else "FAIL"
    print(f"[{mark}] {name}" + (f"  - {detail}" if detail else ""))


def cfg_for(root: Path, **kw) -> Config:
    base = dict(base_url="https://x", root_dir=root, week_start=0, week_end=12,
                units=[], section_patterns=[r"week\s*0*{week}\b", r"topic\s*0*{week}\b"],
                unmatched_folder="_Other", skip_extensions=[], config_dir=root)
    base.update(kw)
    return Config(**base)


class FakeResp:
    def __init__(self, body: bytes, name: str = "file.pdf"):
        self._b = body
        self.ok = True
        self.status = 200
        self.url = f"https://x/{name}"
        self.headers = {"content-disposition": f'filename="{name}"'}

    def body(self) -> bytes:
        return self._b


print("\n=== A. Download safety ===")
root = TMP / "dl"
cfg = cfg_for(root)
mf = Manifest(root / ".manifest.json")
d = root / "Week 01"
p1 = save_response(FakeResp(b"hello"), d, cfg, mf, "https://x/a.pdf")
check("A1 first download writes the file", p1 and p1.exists())

p2 = save_response(FakeResp(b"hello"), d, cfg, mf, "https://x/a.pdf")
check("A2 identical file is adopted, no duplicate",
      p2 is None and len(list(d.glob("*.pdf"))) == 1,
      f"files={[f.name for f in d.glob('*')]}")

p3 = save_response(FakeResp(b"different"), d, cfg, mf, "https://x/other.pdf")
check("A3 different content with same name gets a numbered copy",
      p3 and p3.name != "file.pdf" and len(list(d.glob("*.pdf"))) == 2,
      f"files={[f.name for f in d.glob('*')]}")

(root / ".manifest.json").write_text("{ this is not json", encoding="utf-8")
check("A4 corrupt manifest does not crash", Manifest(root / ".manifest.json").data == {})

mf3 = Manifest(root / ".manifest.json")
mf3.add("https://x/gone.pdf", d / "missing.pdf", 10)
check("A5 deleted local file is re-downloaded", not mf3.has("https://x/gone.pdf"))

check("A6 illegal filename characters are stripped",
      sanitize('a/b:c*d?"e<f>g|h') == "a_b_c_d__e_f_g_h",
      sanitize('a/b:c*d?"e<f>g|h'))

deep = root / ("x" * 120) / ("y" * 120)
check("A7 path longer than 260 chars still writes",
      save_response(FakeResp(b"deep"), deep, cfg, mf, "https://x/deep.pdf") is not None)

# Trimming a long name to the length cap used to take the extension with it,
# leaving a file Windows will not open and a suffix the skip-list and video
# checks read back as empty.
long_pdf = sanitize("A" * 300 + ".pdf")
check("A8 a long filename keeps its extension", long_pdf.endswith(".pdf"), long_pdf[-12:])
check("A9 a long filename still respects the limit", len(long_pdf) <= 150, str(len(long_pdf)))
check("A10 a name that is all extension is still handled",
      sanitize("." * 200) == "file", sanitize("." * 200))

# A manifest entry hand-edited, or written by an older build, used to raise
# KeyError and take the whole sync down with it.
mf_path = TMP / "broken_manifest.json"
mf_path.write_text(json.dumps({"http://x/a.pdf": {"size": 1}, "http://x/b.pdf": "junk"}),
                   encoding="utf-8")
broken = Manifest(mf_path)
try:
    got = (broken.has("http://x/a.pdf"), broken.has("http://x/b.pdf"))
    check("A11 a malformed manifest entry does not crash", got == (False, False), str(got))
except Exception as e:  # noqa: BLE001
    check("A11 a malformed manifest entry does not crash", False, type(e).__name__)

print("\n=== B. Week matching ===")
pat = [r"week\s*0*{week}\b", r"topic\s*0*{week}\b"]
check("B1 'Week 1' does not match week 10", match_week("Week 1: Intro", pat, range(13)) == 1)
check("B2 'Week 10' matches 10", match_week("Week 10", pat, range(13)) == 10)
check("B3 'Topic 03' matches 3", match_week("Topic 03 - Trees", pat, range(13)) == 3)
check("B4 unrelated title matches nothing", match_week("Assessments", pat, range(13)) is None)

print("\n=== C. Recording discovery ===")
# Staff publish recordings four different ways and all four are in use, so a
# change that handles only the obvious one silently loses the rest.
html = """<div>
 <a href="https://monash.au.panopto.com/Panopto/Pages/Viewer.aspx?id=abc-123">Lecture</a>
 <iframe src="https://monash.au.panopto.com/Panopto/Pages/Embed.aspx?id=def-456"></iframe>
 <p>Supplementary: https://www.youtube.com/watch?v=dQw4w9WgXcQ and
    https://youtu.be/aircAruvnKk</p>
 <a href="https://learning.monash.edu/mod/resource/view.php?id=9">Slides</a>
</div>"""
urls = [u for _, u in extract_media_urls(html)]
check("C1 anchor link found", any("Viewer.aspx" in u for u in urls))
check("C2 iframe embed found", any("Embed.aspx" in u for u in urls))
check("C3 bare youtube URL found", any("dQw4w9WgXcQ" in u for u in urls))
check("C4 short youtu.be URL found", any("aircAruvnKk" in u for u in urls))
check("C5 non-media link ignored", not any("mod/resource" in u for u in urls))
check("C6 panopto id parsed from Embed URL",
      captions.panopto_ids("https://monash.au.panopto.com/Panopto/Pages/Embed.aspx?id=def-456")
      == ("monash.au.panopto.com", "def-456"))
check("C7 youtube id parsed from both forms",
      captions.youtube_id("https://youtu.be/aircAruvnKk") == "aircAruvnKk"
      and captions.youtube_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ") == "dQw4w9WgXcQ")

srt = ("1\n00:00:00,240 --> 00:00:06,090\n"
       "[Auto-generated transcript. Edits may have been applied for clarity.]\n"
       "Hello there. This is a test.\n\n2\n00:00:06,810 --> 00:00:10,860\nSecond cue.\n")
txt = captions.srt_to_text(srt)
check("C8 timestamps and cue numbers stripped",
      "-->" not in txt and "00:00" not in txt and "Auto-generated" not in txt, txt[:60])

print("\n=== D. Weekly notes ===")
nroot = TMP / "notes"
ncfg = cfg_for(nroot)
check("D1 empty week writes no note",
      notes.write_note(ncfg, notes.WeekNote(unit="FIT1", week=5,
                                            folder=nroot / "FIT1" / "Week 05")) is None)

# Written as a fixed date first, which quietly started failing the day it went
# past - a test that only passes until a certain morning is worse than no test.
_SOON = (datetime.now() + timedelta(days=30)).strftime("%A, %d %B %Y, %I:%M %p")

wk = nroot / "FIT1" / "Week 01"
wk.mkdir(parents=True)
(wk / "slides.pdf").write_bytes(b"x" * 2048)
note = notes.WeekNote(
    unit="FIT1", week=1, folder=wk,
    links=[("Rec", "https://p/1")],
    no_captions=[("Silent lecture", "https://p/2", captions.NONE),
                 ("Throttled talk", "https://p/3", captions.BLOCKED)],
    assessments=[
        notes.Assessment("A1", _SOON, "u", "assign"),
        notes.Assessment("Old", "Monday, 1 January 2020, 8:00 PM", "u", "assign"),
        notes.Assessment("NoDate", "", "u", "quiz")])
p = notes.write_note(ncfg, note)
check("D2 Word is the primary output", p.suffix == ".docx", p.suffix)
body = p.with_suffix(".txt").read_text(encoding="utf-8")
check("D3 note lists files with size", "slides.pdf" in body and "KB" in body)
check("D4 uncaptioned recordings are named with a reason",
      "no transcript" in body and "Silent lecture" in body
      and "no captions published" in body and "rate-limiting" in body,
      "retryable and final reasons are distinguished")
check("D5 Markdown is not left behind when Word is chosen", not p.with_suffix(".md").exists())
check("D6 future assessment listed", "A1" in body)
check("D7 past assessment hidden", "Old" not in body)
check("D8 undated assessment hidden", "NoDate" not in body)
check("D9 unchanged note is not rewritten", notes.write_note(ncfg, note) is None)
check("D10 plain text copy produced", p.with_suffix(".txt").exists())
(wk / "extra.pdf").write_bytes(b"y" * 1024)
check("D11 new file triggers a rewrite", notes.write_note(ncfg, note) is not None)

print("\n=== E. AI configuration and failure modes ===")
off = ai.Summariser(cfg_for(TMP, ai_provider="", ai_api_key=""))
check("E1 disabled with no provider", not off.enabled)
check("E2 disabled call returns empty string", off.summarise("t", "text") == "")
check("E3 unknown provider is not enabled",
      not ai.Summariser(cfg_for(TMP, ai_provider="notreal", ai_api_key="k")).enabled)
check("E4 ollama needs no key",
      ai.Summariser(cfg_for(TMP, ai_provider="ollama", ai_api_key="")).enabled)


def _stub_urlopen(status: int, payload: bytes):
    """Answer every request with one HTTP status, so the suite stays offline."""
    def _raise(req, timeout=None):  # noqa: ANN001
        raise urllib.error.HTTPError("http://x", status, "stub", {}, io.BytesIO(payload))
    return _raise


_real_urlopen = urllib.request.urlopen
_real_backoff, _real_gap = ai.RATE_LIMIT_BACKOFF_S, ai.MIN_CALL_GAP_S
ai.RATE_LIMIT_BACKOFF_S = ai.MIN_CALL_GAP_S = 0.0
try:
    urllib.request.urlopen = _stub_urlopen(
        400, b'{"error":{"message":"API key not valid"}}')
    try:
        ai.Summariser(cfg_for(TMP, ai_provider="gemini", ai_api_key="INVALID")).summarise("t", "x")
        check("E5 a rejected key raises AIError", False, "no exception")
    except ai.QuotaExhausted as e:
        check("E5 a rejected key raises AIError", False, f"wrongly treated as quota: {e}")
    except ai.AIError as e:
        check("E5 a rejected key raises AIError", True, str(e)[:60])

    # A 403 that survives every retry is throttling, not a bad key, and it will
    # answer the same way for the rest of the run. Without tripping the circuit
    # breaker, every remaining item pays the full backoff again - the hour-long
    # stall the maintainer notes warn about.
    urllib.request.urlopen = _stub_urlopen(403, b'{"error":{"message":"throttled"}}')
    try:
        ai._post("http://x", {}, {})
        check("E6 a persistent 403 trips the quota circuit breaker", False, "no exception")
    except ai.QuotaExhausted as e:
        check("E6 a persistent 403 trips the quota circuit breaker", True, str(e)[:60])
    except Exception as e:  # noqa: BLE001
        check("E6 a persistent 403 trips the quota circuit breaker", False,
              f"{type(e).__name__}: {e}")
finally:
    urllib.request.urlopen = _real_urlopen
    ai.RATE_LIMIT_BACKOFF_S, ai.MIN_CALL_GAP_S = _real_backoff, _real_gap

print("\n=== F. Transcripts and summaries ===")
tdir = TMP / "tr"
tp = captions.save_transcript(tdir, "Lecture 1", "Panopto", "https://p/1", "Body text here.")
readable = tp.with_suffix(".txt")
check("F1 transcript saves in Word and text",
      tp.suffix == ".docx" and readable.exists()
      and "Summary" not in readable.read_text(encoding="utf-8"))
pending = captions.transcripts_without_summary(tdir)
check("F2 missing summary is detected for retry", len(pending) == 1,
      str([q.name for q in pending]))
captions.add_summary(pending[0], "Key points.")
after = readable.read_text(encoding="utf-8")
check("F3 summary sits above the transcript",
      "Summary" in after and after.index("Summary") < after.index("Body text here."))
check("F4 a file with a summary is not retried", captions.transcripts_without_summary(tdir) == [])
check("F5 Word copy is rebuilt with the summary", tp.exists() and tp.stat().st_size > 0)

print("\n=== G. Failure reasons ===")
# Every skipped recording has to say whether waiting will help. Silent skipping
# is what kept a whole class of breakage invisible for weeks.
check("G1 a Panopto sign-in failure is retryable, not 'no captions'",
      captions.SIGNIN in captions.RETRYABLE)
check("G2 'no captions' stays final", captions.NONE not in captions.RETRYABLE)

# Reading a recording that has no captions is a capability only Gemini has.
# Reporting that as "no captions" reads as final, when changing provider fetches it.
check("G3 'no video reader' is distinct from 'no captions'",
      captions.NO_READER != captions.NONE, captions.NO_READER)
check("G4 it does not falsely promise a retry", captions.NO_READER not in captions.RETRYABLE)
_txt = captions.reason_text(captions.NO_READER)
check("G5 it names what would fix it", "Gemini" in _txt and "retry" not in _txt, _txt)
check("G6 every reason has readable text",
      all(captions.reason_text(r) != r for r in
          (captions.NONE, captions.BLOCKED, captions.ERROR, captions.SIGNIN,
           captions.DEFERRED, captions.NO_READER)))


# A sign-in that never completes must not be reported as "genuinely has no
# captions". The two are indistinguishable from the outside, which is exactly
# why this one is pinned.
class _FakePage:
    url = "https://x.panopto.com/Panopto/Pages/Auth/Login.aspx"

    def goto(self, url, **kw): pass
    def wait_for_timeout(self, ms): pass
    def evaluate(self, js): return []
    def query_selector(self, sel): return None
    def select_option(self, *a, **kw): pass
    def close(self): pass


class _FakeSess:
    ctx = type("Ctx", (), {"new_page": lambda self: _FakePage()})()


result, why = captions.PanoptoClient(_FakeSess()).transcript("x.panopto.com", "guid-1")
check("G7 an unfinished Panopto sign-in reports needs-signin",
      result is None and why == captions.SIGNIN, why)

print("\n=== H. Concurrency, history and config ===")
check("H1 lock is acquired", lock.acquire("uat-test"))
check("H2 same process re-entry allowed", lock.acquire("uat-test"))
lock.release("uat-test")

history.record("ok", 3)
history.record("login-needed")
history.record("error", detail="Timeout")
runs = history.load()[-3:]
check("H3 history records outcomes",
      "3 new files" in history.describe(runs[0])
      and "sign in" in history.describe(runs[1])
      and "failed" in history.describe(runs[2]),
      " | ".join(history.describe(r) for r in runs))
check("H4 the suite wrote history to a temp dir, not the real one",
      str(TMP) in str(history._path()), str(history._path().parent))

bad = TMP / "bad.yaml"
bad.write_text("units: [oops\n", encoding="utf-8")
try:
    load_config(bad)
    check("H5 malformed YAML raises a clear error", False, "no exception")
except Exception as e:  # noqa: BLE001
    check("H5 malformed YAML raises a clear error", True, type(e).__name__)

empty_cfg = TMP / "empty.yaml"
empty_cfg.write_text("", encoding="utf-8")
c = load_config(empty_cfg)
check("H6 empty config falls back to defaults",
      c.week_end == 12 and c.course_selection == "manual" and c.ai_provider == "")

print("\n=== I. YouTube request budget ===")
b = YouTubeBudget(3)
took = [b.take() for _ in range(5)]
check("I1 budget hands out exactly its limit",
      took == [True, True, True, False, False], f"{sum(took)} of 5 allowed")

# The regression this pins: the cap is named per-sync, so it must not reset per
# unit. Four units at eight each is the thirty-two request burst that gets an
# address blocked - the exact thing the limit exists to prevent.
shared = YouTubeBudget(8)
per_unit = []
for _unit in range(4):
    n = 0
    while shared.take() and n <= 50:
        n += 1
    per_unit.append(n)
check("I2 one budget spans every unit in the sync",
      sum(per_unit) == 8, f"units got {per_unit}, total {sum(per_unit)}")
check("I3 a spent budget refuses further fetches", not shared.take())

print("\n=== J. Source text encoding ===")
# An em dash in main.py had been saved double-encoded, so every note built from
# it carried a garbled separator into Word, plain text and Markdown alike. The
# bytes look like an ordinary dash in an editor, which is why it survived - so
# it is guarded at the byte level instead.
MOJIBAKE = {
    "em dash": b"\xc3\xa2\xe2\x82\xac\xe2\x80\x9d",
    "en dash": b"\xc3\xa2\xe2\x82\xac\xe2\x80\x9c",
    "curly quote": b"\xc3\xa2\xe2\x82\xac\xe2\x84\xa2",
}
offenders = [f"{py.name}:{what}"
             for py in sorted((REPO / "moodle_dl").glob("*.py"))
             for what, seq in MOJIBAKE.items()
             if seq in py.read_bytes()]
check("J1 no double-encoded punctuation in the source",
      not offenders, ", ".join(offenders) or "clean")

print("\n=== K. Stale unpack-folder sweep ===")
# A one-file build strands ~150 MB in %TEMP% every time it is killed rather
# than closed - which includes every Windows restart while auto-sync is running.
run_py = (REPO / "run.py").read_text(encoding="utf-8")
check("K1 the sweep runs before anything else in cli()",
      re.search(r"def cli\(\) -> int:\s*\n\s*_sweep_stale_unpack_dirs\(\)", run_py) is not None)
check("K2 it only ever touches _MEI folders",
      r"_MEI\d+" in run_py and 'glob("_MEI*")' in run_py)
check("K3 it never deletes the folder this process is running from",
      "_MEIPASS" in run_py and "d.resolve() == mine" in run_py)
check("K4 it does nothing when running from source",
      'if not getattr(sys, "frozen", False):' in run_py)

print("\n=== L. Mangled filenames ===")
# Moodle sends the filename in a Content-Disposition header, and HTTP header
# values are Latin-1 by specification - so a UTF-8 en dash arrived as three
# characters, two of them C1 controls. Explorer shows almost nothing wrong;
# OneDrive refuses the whole name and reports it, every day, forever.
MANGLED = "Empathy \u00e2\u0080\u0093 Advanced.pdf"
FIXED = "Empathy – Advanced.pdf"

check("L1 a Latin-1 mangled name is repaired",
      demojibake(MANGLED) == FIXED, ascii(demojibake(MANGLED)))
check("L2 sanitize repairs it on the way in", sanitize(MANGLED) == FIXED)
check("L3 healthy names are left alone",
      all(sanitize(n) == n for n in ["Week 08 – Cryptanalysis.pdf",
                                     "Café notes.pdf", "中文.pdf"]))
check("L4 a C1 control that cannot be decoded is still removed",
      "\u0093" not in sanitize("broken\u0093name.pdf"),
      ascii(sanitize("broken\u0093name.pdf")))
check("L5 a leading dot survives, so .manifest.json keeps its name",
      sanitize(".manifest.json") == ".manifest.json", sanitize(".manifest.json"))

# Repairing new downloads does nothing for the files already on disk, and
# renaming one by hand makes it worse: the manifest points at a path that no
# longer exists, so the next sync fetches it again under the same broken name.
rroot = TMP / "repair"
rdir = rroot / "FIT5234" / "Assign \u00e2\u0080\u0093 One"
rdir.mkdir(parents=True)
(rdir / MANGLED).write_bytes(b"x")
rman_path = rroot / ".manifest.json"
rman_path.write_text(json.dumps({"https://m/1": {"path": str(rdir / MANGLED), "size": 1}}),
                     encoding="utf-8")
rdone = repair_names(rroot, Manifest(rman_path))
check("L6 the file and its folder are both renamed", len(rdone) == 2, str(len(rdone)))
check("L7 the repaired file is on disk",
      (rroot / "FIT5234" / "Assign – One" / FIXED).exists())
check("L8 the manifest followed it, so the file is not downloaded again",
      Manifest(rman_path).has("https://m/1"))
check("L9 a second run finds nothing left to do",
      repair_names(rroot, Manifest(rman_path)) == [])
check("L10 the sync's own manifest file is never renamed", rman_path.exists())
check("L11 the repair runs before anything is fetched",
      "repair_names(cfg.root_dir, manifest)"
      in (REPO / "moodle_dl" / "main.py").read_text(encoding="utf-8"))

shutil.rmtree(TMP, ignore_errors=True)
print(f"\n==== {len(PASS)} passed, {len(FAIL)} failed ====")
if FAIL:
    print("FAILED:", *FAIL, sep="\n  - ")
raise SystemExit(1 if FAIL else 0)
