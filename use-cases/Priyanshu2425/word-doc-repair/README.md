# Salvage — repair my broken Word doc

**Assigned build B · band S2 · API + export · SuperDocs**

A `.docx` that will not open leaves its owner with nothing — Word says the file
is corrupt and offers no way forward. Drop it on this page and you get back a
clean, styled Word file, plus a plain account of what came through and what did
not.

![Salvage's report after recovering a truncated document: a stamped verdict, what came through, and the repaired file](screenshot.png)

## What it does

It opens the file the way Word will not, takes out whatever is still readable,
and rebuilds a valid document around it.

A `.docx` is a ZIP of XML parts, and it breaks in a handful of recognisable
ways. Each one needs a different move:

| What is wrong | What it does about it |
|---|---|
| The file's index is damaged or the download was cut short | Reads the internal file headers directly and inflates each part by hand. The index lives at the *end* of the file, so it is the first thing a truncated download loses — while the content itself is usually still there. |
| The document body is malformed — a stray `&`, an invalid character, a tag left open by the cut | Repairs each fault, then closes any elements still open at the end. It never reorders or invents content. |
| Structural parts are missing | Regenerates them. They are standard plumbing and carry none of your content, which is why the report does not list them as a loss. |
| Your pictures are in there somewhere | Carries them back into the rebuilt file. Images are separate members of the archive with their own headers, so they survive exactly the damage that destroys the index — and when the part that said *where* each one belonged did not survive, they go at the end under a heading rather than being placed somewhere plausible and wrong. |
| Footnotes, headers and footers | Recovered as text and set down at the end, each under its own heading. A footnote folded silently into the body would change what the document says. |
| The body is too damaged to parse at all | Falls back to pulling the text out run by run — and **says so**, because you are getting words back without headings or tables, and finding that out later is the failure this tool exists to prevent. |
| The main document part is gone entirely | Says it cannot be repaired. Nothing is invented to fill the gap. |

## What it promises, and what it does not

It never claims a complete repair. A test asserts that — it fails the build if
the words *fully repaired*, *guaranteed*, *perfect* or *100%* appear anywhere in
any output the user can see.

**And it shows you the document before you take it.** The rebuilt file is
rendered on the page — headings, tables, and the pictures — above the download
button, because the question *was any of this worth it* is one people ask before
they act. This is the part of the page whose honesty needs no wording at all:

> It ran for a bit and then wanted forty dollars to download the result. It just
> said "repair successful". I didn't know if it had actually got anything.

**And it checks the styled copy before offering it.** A styling pass has no
reason to change a single word, so the file that comes back is compared against
what went out, word for word, and thrown away if the wording moved at all. This
is not theoretical: on the first live run, a four-line recovered report came
back with three invented paragraphs, a subtotal row, a disclaimer and a
signature block. It opened cleanly and read better than the plain rebuild, and
it was partly fiction — which for a recovery tool is the worst output there is,
because its owner would not notice.

Three things it will always do:

- **Name what it lost.** Structure that could not be preserved, parts cut short
  by the damage, content that was not there to recover.
- **Refuse to call an empty file a success.** A valid document with nothing in it
  is the most dangerous possible output — it opens cleanly, so the owner may not
  notice their content is gone for weeks. That is reported as a failure.
- **Tell you to keep the original.** It is on the result screen, every time.

## What it uses from SuperDocs

| Surface | Used for |
|---|---|
| **REST API** — `POST /v1/documents/upload` | Sending the recovered structure up as clean HTML |
| **Chat editing** — `POST /v1/chat/async` | One edit instruction: restore formatting, change no content |
| **Human-in-the-loop approval** — `POST /v1/chat/{session_id}/approve` | Approving each proposed change, with the second parse its payload requires |
| **Export** — `POST /v1/documents/export` | Getting a styled `.docx` back |

All four are reachable from the page: once the plain file is downloadable, a
**Send it for styling** button offers a second, styled copy. It is never
automatic — someone whose document just broke should not have it sent to a
third-party service because a page decided that for them — and the plain rebuild
is already in their hands before the button exists.

The local rebuild is the default and needs no key, no network and no account —
and **every failure on the SuperDocs path degrades back to it**, which a test
asserts for each failure in turn. Details under
[Where SuperDocs fits](#where-superdocs-fits).

## Set it up

**Requirements.** Python 3.10 or newer, and nothing else. Node is needed only to
change the page — the built bundle is committed, so running it needs no Node and
no network.

```
git clone <this repo> && cd use-cases/Priyanshu2425/word-doc-repair

python3 -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -e ".[web,dev]"

python3 -m docrepair.web                               # http://127.0.0.1:8000
```

Open the address it prints and drop a damaged `.docx` on the page. That is the
whole product; everything below is optional.

| | |
|---|---|
| `PORT=8077 python3 -m docrepair.web` | if 8000 is taken |
| `SUPERDOCS_API_KEY=sk_… python3 -m docrepair.web` | also offers the styled copy (see [Where SuperDocs fits](#where-superdocs-fits)) |
| no key set | the page says styling is off, in a line, and the rest works exactly as before |

Nothing is written to disk and nothing is retained: uploads and repaired files
are held in memory for one download window and then dropped.

Have a folder of them rather than one? The CLI is the same engine:

```
python3 backend/cli.py broken.docx
python3 backend/cli.py broken.docx -o fixed.docx
python3 backend/cli.py *.docx --quiet
python3 backend/cli.py broken.docx --via-superdocs      # needs SUPERDOCS_API_KEY
```

## Test it

Three commands, five files. **None of them needs an API key, a network or a
bucket** — every SuperDocs failure and success is driven through an injected
fake, and every DOCX the automated tests use is written by the tests themselves
at run time and then broken in one specific way. (The eight files under
`manual-test/fixtures/` are committed, because those are for testing by hand.)

```
python3 -m pytest                     # 63 pass, 1 skip — needs nothing installed
pip install -e ".[web,dev]"
python3 -m pytest                     # 76 pass — adds the endpoint tests
cd frontend && npm install && npm test # 46 pass — the page, rendered and driven
```

| Suite | Count | What it holds |
|---|---|---|
| `tests/test_repair.py` | 31 | The engine, over a real DOCX broken eight specific ways. The assertion is never "it did not crash": the output is reopened, every required part checked, the body re-parsed, the table compared cell by cell against the original. |
| `tests/test_styled_export.py` | 21 | The four SuperDocs calls, in order, and every way the path can fail — dead network, exhausted allowance, no job id, empty export, a job that never settles, an unreadable balance. Each one must degrade to the plain rebuild. |
| `tests/test_web.py` | 13 | The two endpoints, called the way the page calls them. Skips rather than fails when the web extra is absent. |
| `tests/test_frontend_fixtures.py` | 11 | Records the page's fixtures from real repairs and fails if they drift. |
| `frontend/…/App.test.tsx` | 46 | The whole page rendered, answered with those recorded streams, driven through every path a person can take. |

**The tests that matter most are the ones about honesty**, and they run over
what is on the screen rather than over the engine's strings:

- a missing document part **fails**, and says why
- an empty body is a **failure**, not a success with no content
- no user-visible string claims a complete, perfect or guaranteed repair —
  with negations understood, because *"not a complete repair"* is the sentence
  this build exists to say
- structural parts the tool rebuilds are never reported as lost content
- no engine vocabulary reaches a worried person — no `ZIP`, `XML`, `CRC`,
  `central directory`, no exception class name
- a styled copy whose wording changed is **thrown away**, not handed over

That distinction — rendered output, not engine output — is not pedantry:
BUG-014 was fixed in the engine and came straight back as BUG-020 in the page.

**Working on the page itself:**

```
cd frontend
npm install
npm run dev        # vite on 5173, /api proxied to the engine on 8000
                   # SALVAGE_API=http://127.0.0.1:8077 npm run dev  if you moved it
npm test           # 46
npm run build      # rewrites backend/static/index.html AND bundle-manifest.json
```

The bundle is committed so a reviewer with no Node still gets the product. The
cost of that decision is that it can fall behind its source and nobody notices,
so the build writes a hash over every source file and a pytest recomputes it.
**Stale bundle, failed build** — if you change anything under `frontend/src`,
run `npm run build` before committing.

**Checking it by hand.** Three pages, all openable straight from disk:

| File | What it is |
|---|---|
| `manual-test/index.html` | The interactive checklist — 19 items, verdicts persist across reloads, exports as Markdown |
| `manual-test/MANUAL_QA_PLAN.html` | The record of an actual run against a live server, with what could not be run marked *not run* rather than passed |
| `manual-test/UI_FLOWS.html` | Every path through the page, screen by screen |
| `SYSTEM_DESIGN.html` | How it is built and why — the architecture, the two paths, the failure matrix |

`manual-test/make_fixtures.py` regenerates the eight broken files in
`manual-test/fixtures/` if you want fresh ones.

## How it is tested

Every test starts from a **real** DOCX this package wrote, then breaks it in one
specific way — truncated container, missing content-types part, unclosed tags,
bare ampersands and control characters, missing document part, not a ZIP at all,
empty body, and an illustrated document both whole and cut short. The assertion is not "it did not crash": the output is reopened as a
ZIP, every required part is checked, the body is re-parsed, and the recovered
table is compared cell by cell against the original.

The tests that matter most are the ones about honesty:

- a missing document part **fails**, and says why
- an empty body is a **failure**, not a success with no content
- no user-visible string overclaims
- structural parts the tool rebuilds are never reported as lost content
- the user-facing lists contain no jargon — no `ZIP`, `XML`, `CRC`, or
  `central directory` leaking out of the engine into a worried person's summary

## Where SuperDocs fits

The four-call contract — upload · edit instruction · approve · export — is built
and tested in `backend/docrepair/styled_export.py`, and runs as an optional
second pass. Set a key and the page offers it; the CLI takes a flag:

```
export SUPERDOCS_API_KEY=your-key-here
python3 -m docrepair.web                     # the button appears on the result screen
python3 backend/cli.py broken.docx --via-superdocs
```

Without a key the page says so in a line rather than showing a button that
fails when somebody presses it: `GET /api/capabilities` is asked before anything
is offered.

**The allowance is read before anything is spent.** `GET /v1/agents/whoami`
carries the remaining balance, and a styling pass that cannot finish is refused
before the first billable call rather than discovered halfway through — trap 3,
asked in advance. A balance that cannot be read is *not* treated as a balance of
zero: a personal key is not an agent key, and refusing on a number nobody
managed to read would be its own kind of bluff.

**And the result is checked, not trusted.** `content_drift` compares the words
that came back against the words that went out. Any addition or removal and the
styled file is discarded with the reason said plainly. The instruction was
tightened at the same time and the same document then came back word for word
identical — but the instruction is the request and the guard is the promise.

Recovered structure goes out as clean **HTML**, never raw Word XML — the docs are
explicit that there is no endpoint for the latter — so headings stay headings and
tables stay tables. The edit instruction restores formatting and forbids content
changes; each proposed change is approved through the HITL endpoint (with the
second parse the payload requires); the export comes back as a styled DOCX.

**Every failure on this path degrades to the local rebuild**, and a test asserts
it for each one: a dead network, an exhausted allowance, no job id, an empty
export, or any unexpected exception. The engine has already produced a valid file
before this runs, and nothing here is allowed to take that away from the user.

**The local rebuild is the default, and that is deliberate.** Someone whose
document is broken should not have to hand it to a third-party service, or wait
on an API, to find out whether anything survived. The offline path answers that
in milliseconds and costs nothing; the SuperDocs path is for producing a
polished, fully-styled export once you know there is something worth styling.

Session state and uploads are held in memory for one download and then dropped.
Nothing is written to disk and nothing is retained.

## Honest limitations

- **The rebuilt file is deliberately plain.** Your original theme, fonts and
  spacing cannot be recovered from a broken file, so it rebuilds with clean
  headings, tables and body text rather than guessing at a design you had.
- **Pictures come back; their captions and wrapping do not.** An image is placed
  inline where the body referenced it, or at the end when nothing survived to
  say where it belonged. Floating positions, text wrap and captions are gone.
- **Footnotes, headers and footers come back as text, not as page furniture.** A
  rebuild cannot put a footnote back at the foot of the page it belonged to, so
  their text is set down at the end under its own heading and the report says so.
- **An image whose header cannot be read is placed at a stated default size**
  rather than at a measurement nobody took. Its aspect ratio is left alone.
- **Comments and tracked changes are not recovered.**
- **The styled copy carries the recovered text and nothing else.** Pictures are
  not sent for styling: the styling pass takes clean HTML, and an image with no
  surviving position is not something a formatting pass can place. The plain
  rebuild is the one that has your pictures in it.
- **A styled copy is not always available.** It needs a key, an allowance, and a
  result that comes back saying exactly what it was given. Any of the three
  missing and you get the plain rebuild and a sentence saying why.
- Direct upload only, so the practical ceiling is about 20 MB.
- `.doc` (the pre-2007 binary format) is not a ZIP at all and is not supported —
  it is reported as unrepairable rather than silently mangled.

## Testing it by hand

The automated suite proves the engine does the right thing. It cannot tell you
whether the downloaded file opens in Word, whether the progress is perceptible,
or whether a non-technical person understands what they got back.

`manual-test/index.html` is a checklist for exactly that — open it in a second
tab. Nineteen items: eight deliberately broken fixtures with their expected
results transcribed from real runs, four edge cases, and seven judgement calls
no test can make. Verdicts and notes persist across reloads, and it exports the
results as Markdown.

Writing it found three defects the suite had missed: a total failure still
rendered a "what came through" claim, one problem was reported twice, and an
exception class name leaked into a stage line. All three are fixed, and the
suite now guards the stage log as well as the summary.

## Shared code — stated plainly

`backend/docrepair/superdocs_client.py` is the same four-call client used by the
quota-aware agent (assigned build A). It is vendored into both so each stands
alone in the builds repository. Reuse is only a shortcut when it is hidden.

## Credit

Built by **Priyanshu Semwal** ([@Priyanshu2425](https://github.com/Priyanshu2425))
for the SuperDocs engineer round, 2026. MIT licensed — see [LICENSE](LICENSE).

Grounded throughout in the SuperDocs API documentation; where the task brief and
the documentation differed, the documentation won.
