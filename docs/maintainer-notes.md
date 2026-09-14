# Maintainer notes

Things that were learned the hard way. Most of them are not guessable from
the code, and several took a full debugging session to find.

## Monash specifics

**The mobile web service is disabled.** `admin/tool/mobile/launch.php`
answers *"Web service is not available"*, so the approach every other Moodle
downloader takes (get a token, call the official API) does not work here.

**The web UI's own AJAX endpoint does work**, using nothing but the browser
session:

```
POST /lib/ajax/service.php?sesskey=...&info=core_courseformat_get_state
```

It returns every section and activity with ids, plugin names, parent links
and `uservisible`. `core_course_get_contents` is *not* callable this way.
See `structure.py`. It is used to catch activities the rendered page hides -
an activity with a release date in the future is drawn as plain text with no
link, so parsing the markup cannot see it exists.

**Section titles from the API do not contain "Week N".** The custom course
format renders that separately, so week matching still comes from the HTML.
The two sources are joined on section number.

**Weeks are nested.** `Week N` is a subsection of a `Learning` section, and
each week holds `Own-time` / `Real-time` / `Wrap-up` subsections that hold
the actual material. Section numbers are not sequential: week 1 is section 7,
week 2 is section 11. Children inherit their parent's week.

## Recordings

**Staff publish them four different ways** and all four are in use: a link, a
bare URL typed into the page text, an `<iframe>` embed, and a `page` or `url`
activity that wraps one of the above. Wrapper activities are opened and
followed two levels deep.

**Panopto authorises each recording separately.** Signing in to the Panopto
site is not enough: `DeliveryInfo.aspx` returns an empty object for a
recording whose viewer has not been opened, which looks exactly like "this
video has no captions". Opening `Viewer.aspx?id=<guid>` performs the
per-recording authorisation - and that page may itself ask for the
institution login again. This is why videos titled "(with subtitles)" were
reported as having none.

**The caption language code is per-session.** Take it from
`AvailableCaptions`; requesting language 0 returns an empty file on recordings
that use 15.

**YouTube blocks by IP, not by tool.** `youtube-transcript-api` and `yt-dlp`
both return 429 from a blocked address - swapping libraries does not help.
What helps: fetching each video once (the manifest), spacing requests, a cap
per sync, and asking Gemini to read the video from Google's side when we are
blocked.

**That cap has to span the whole run.** It was counted inside the per-unit
transcript pass, so it reset on every unit - four units at eight each is the
thirty-two request burst the cap exists to prevent, which is precisely how an
address gets blocked. It is a `YouTubeBudget` handed to each unit now, so the
limit means what its name says.

**Zoom cloud recordings cannot be transcribed.** They are a share link plus a
passcode with no caption endpoint. Staff sometimes paste the passcode into
the link itself, which produces a broken address - it is split back out.

## AI

**Verbatim transcription trips the recitation filter.** Gemini refuses with
`finishReason: RECITATION`. Asking for the same content "in your own words"
gets it through, so that is the automatic second attempt.

**Free tiers rate-limit by the minute and by the day.** A transient 403 means
throttling, not a bad key - do not accuse the key. Once a quota error is
final, stop asking for the rest of the run: without that circuit breaker a
large first sync spends an hour in backoff. Anything left is picked up on a
later sync, because a transcript missing its summary is retried.

**Pinned Gemini model names 404 or 429 on a free key.** The `-latest` aliases
are the ones the free tier serves.

## Tests

```
python tests/uat.py
```

A hundred and four checks, no network, and it touches nothing real - `LOCALAPPDATA` is
redirected to a temp folder at import time, so the lock file, settings and sync
history all land there rather than in the copy you actually use. Exits non-zero
on failure, so a hook or CI step can gate on it.

**Run it after any change to downloads, notes, captions or AI.** It has caught
several regressions a type checker could not: the YouTube cap that reset per
unit, the double-encoded em dash, and the `needs-signin` state that nothing
emitted. Each check names the failure it stands guard over - that commentary is
the valuable part, so keep it when editing.

Section `L` covers filenames that arrive mis-decoded. Moodle sends the name in a Content-Disposition header, and HTTP header values are Latin-1 by specification, so a UTF-8 en dash came back as three characters - two of them C1 controls, invisible in Explorer and rejected outright by OneDrive. `repair_names()` runs before every sync and renames the files already on disk, moving the manifest entry with them; without that the manifest points at a path that no longer exists and the file is downloaded again under the same broken name, which is why renaming by hand never worked.

Two of the sections read the source as bytes or text rather than calling it,
because the bug they pin is invisible to an import: `J` scans for
double-encoded punctuation, and `K` checks the unpack sweep is still wired into
`cli()`.

## Grades

Three modules, kept apart because they fail in different ways:

- `gradebook.py` parses Moodle's user report. Four columns only - Monash has
  the weight, contribution and course-total columns turned off in every unit
  checked, so this stops at "what did I score, out of what".
- `assessplan.py` reads the unit's own PDFs and Word documents for the marking
  scheme. Documents are ranked (unit overview first, transcripts last) and
  reading stops once one document's own numbers reach 100%.
- `grades.py` holds the model, the arithmetic and the merge. It imports
  nothing that touches the network, which is why the whole thing is testable
  from saved HTML.

**Entries are never pooled across documents.** An assignment brief breaks its
own 15% into tasks worth 6% and 3%; adding those to the unit overview's list
produced a unit worth 118%. Only a document that states a whole scheme is
believed, and only a complete scheme is allowed to create items.

**Name matching has two rules, both from real failures.** A shared number is
never enough on its own - "Quiz 1" and "Individual Assignment 1" matched on
the digit alone and walked the assignment weights down the quiz list. A
differing number disqualifies - "Individual Assignment 1" and "Individual
Assignment 3" otherwise match on every word. Section M pins both directly,
because the fixture-level checks did not: with the quizzes bound into a block,
no bare quiz reaches the matcher and the bug cannot reappear there.

**A weight is only as good as its source,** so every one carries where it came
from and the GUI prints it under the row. The order is document, then a number
in the item's own name, then the range - a range of 0-15 is only probably a
15% weight, and is 0-100 far too often to trust.

**Anything the user edits is locked.** `Item.edited` lists the fields they have
touched and `Part.edited` does the same for one quiz; a sync updates
everything else and leaves those alone. This is the feature, not a safety net:
Moodle cannot know about a fourth quiz it has not created, and the user can.

## Design decisions worth keeping

- Word first, plain text second, Markdown off. Most readers have never opened
  a `.md` file, and chatbots accept `.docx` and `.txt`.
- Video files are not downloaded. The text is the point.
- The download record is a convenience, not a source of truth: an identical
  file already on disk is adopted, so a lost manifest costs one fetch rather
  than filling folders with numbered duplicates.
- Failures say whether they are final or will be retried. Silent skipping is
  what made several of the bugs above invisible for so long.
