# Design — Salvage

The web page for this build, and only this build. It shares no visual language
with anything else its author has built, deliberately: a person whose file just
broke is not the same person, on the same day, as somebody reviewing a document
at their desk.

## The world

A postal recovery notice — what an institution sends you when your item arrived
damaged. A printed wrapper, chevron edge tape, a docket number, a rubber-stamped
verdict, and language that takes responsibility in plain words.

It was chosen because it carries the product's truth exactly: careful custody of
something that is not ours, an honest account of what survived, and bad news
delivered in the same voice as good news. It is not the SaaS uploader page — no
hero, no feature cards, no dashed rectangle — and it is not a terminal.

## Colour

Committed: the institution's blue owns the marks and the actions, red belongs to
damage, and the ground is a cool form stock so the page never reads as warm
stationery.

| Token | Light | Meaning |
|---|---|---|
| `--stock` / `--sheet` | `#e9ebe6` / `#fcfcfa` | the desk, the notice |
| `--ink` / `--ink-2` / `--ink-3` | `#14171a` / `#494f55` / `#7b8188` | text, secondary, tertiary |
| `--post-blue` | `#173f8a` | the institution: marks, stamps, the download action |
| `--post-red` | `#c2222c` | damage: the failed stamp, losses, refusals |
| `--kept` | `#16603c` | one tick, beside something that survived |

Red is never used for emphasis or decoration — only for something that was lost
or refused. Dark mode is the same notice under a desk lamp.

## Type

The system UI stack, set large: 17px body, 28–38px headline, short measure. One
monospace, used only for the record line — filename, size, docket — because that
is a form field, not a costume.

## Devices with a fixed meaning

- **Chevron tape** — edges anything in our custody: the notice itself, and the
  panel holding the repaired file. Nothing else.
- **The stamp** — one verdict, landing once with a small rotation: *Recovered*,
  *Recovered in part*, or *Could not be repaired*.
- **Stations** — the engine's real stages, shown while it works, then folded
  behind a disclosure once the verdict is known. The answer outranks the working.
- **The record line** — file, size, docket. A receipt, so the page has a name for
  the thing it is holding.
- **The sheet** — the rebuilt document itself, rendered for reading, between the
  losses and the handover. It carries no chevron tape: tape means *in our
  custody* and edges only the notice and the panel holding the file, and this is
  the contents rather than the custody. It sits above the download because the
  question it answers is asked before the download, not after.

## Rules that are not style

- A failure renders no "what came through" section and no download. (BUG-012.)
- Losses are set at the same weight as recoveries. Styling losses as a footnote
  would be the overclaim this build refuses, expressed in CSS.
- Retention is stated where the file is handed over, before it can expire.
- The verdict is a claim and the sheet is the thing itself. Where both are on
  screen the sheet is never smaller than the claim about it.
- No engine vocabulary reaches the page — not in a stage line, not in a list, not
  in an error. Enforced over rendered output, for every fixture. (BUG-014, BUG-020.)
- No sentence claims a complete, perfect or guaranteed repair. Also enforced over
  rendered output, with negations understood: *"not a complete repair"* is the
  sentence this product exists to say.

## Mobile

Designed at 400px first. One column throughout, controls at full width, the
verdict and the download reachable without a horizontal thought.
