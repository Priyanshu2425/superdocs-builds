"""The repair, as stages someone can watch, and a report they can read.

The stages are real branch points, not labels. What happens at each one depends
on what the previous one found, and the report says which path was taken --
because "we recovered your text but lost your tables" and "we recovered
everything" should not look the same to the person who gets the file back.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from . import docx, media, salvage
from .salvage import plural

Progress = Callable[[str, str], None]  # (stage, human-readable message)

DOCUMENT_PART = "word/document.xml"
DOC_RELS_PART = "word/_rels/document.xml.rels"

#: Parts that carry a person's words but are not the body of the document.
#: Each is recovered as text and set down at the end under its own heading,
#: because a rebuild cannot put a footnote back at the foot of the page it
#: belonged to -- and a footnote silently folded into the body would change what
#: the document says.
ASIDE_PARTS: list[tuple[str, str]] = [
    ("word/footnotes.xml", "Footnotes"),
    ("word/endnotes.xml", "Endnotes"),
    ("word/header", "Page header"),
    ("word/footer", "Page footer"),
]

#: Boilerplate Word writes into every footnotes part whether or not the document
#: has footnotes. Carrying it into the rebuild would invent an aside that never
#: existed.
_EMPTY_ASIDE = {"", "-", "\u2014"}


@dataclass
class Repair:
    ok: bool = False
    output: bytes = b""
    filename: str = "repaired.docx"
    recovered: list[str] = field(default_factory=list)
    lost: list[str] = field(default_factory=list)
    stages: list[tuple[str, str]] = field(default_factory=list)
    structure_preserved: bool = True
    counts: dict[str, int] = field(default_factory=dict)
    #: The rebuilt document rendered for reading, pictures included. Shown
    #: before the download, never instead of it.
    preview_html: str = ""
    #: Whether the file's own index had to be gone around. Carried on the report
    #: rather than passed between stages, because the stage that discovers it and
    #: the stage that is allowed to say it are not the same one.
    container_was_damaged: bool = False

    def as_payload(self, download: str | None = None) -> dict:
        """The one shape this result takes on the wire.

        Here rather than in `web.py` because two callers need it — the endpoint
        and the pytest that records the page's fixtures — and a hand-copied
        second version is how a fixture drifts from the thing it imitates while
        still claiming to be captured from it. That is BUG-015's shape, and the
        capture's own docstring says *recorded exactly as the web endpoint
        streams it*, which nothing was holding it to.
        """
        return {
            "ok": self.ok,
            "summary": self.summary(),
            "recovered": self.recovered,
            "lost": self.lost,
            "counts": self.counts,
            "structure_preserved": self.structure_preserved,
            "filename": self.filename,
            "preview_html": self.preview_html,
            "download": download if self.ok else None,
        }

    def summary(self) -> str:
        """Plain language. Never claims a complete repair."""
        if not self.ok:
            reason = self.lost[0] if self.lost else "no readable content was found inside it"
            reason = reason[0].upper() + reason[1:]
            if not reason.endswith("."):
                reason += "."
            return (
                f"This file could not be repaired. {reason} "
                "Nothing was invented to fill the gap — if there is another copy, "
                "even an older one, that is the better starting point."
            )
        head = (
            "Recovered what could be read and rebuilt it as a valid Word file. "
            "This is a best-effort recovery, not a complete repair."
        )
        if not self.structure_preserved:
            head += (
                " The document's structure was too damaged to read, so the text was "
                "extracted run by run — you are getting the words back, but the "
                "headings, tables and formatting are gone."
            )
        return head


class _Stop(Exception):
    """A stage decided the repair cannot go further.

    Raised rather than returned so each stage can refuse from wherever it
    discovers the refusal, and `repair` stays a narrative of the happy path with
    the refusals visible in one place. Every one of them has already written its
    reason into the report before it raises.
    """


def repair(data: bytes, filename: str = "document.docx",
           on_progress: Progress | None = None) -> Repair:
    """The whole repair, as the sequence of decisions it actually is.

    Each stage is its own function, because each one is a different judgement
    about somebody's document and they are worth reading separately. What stays
    here is the order, which is the part that has to be right: nothing is
    claimed as recovered before it has been read, and nothing is written before
    everything that could refuse has had its chance to.
    """
    r = Repair(filename=_repaired_name(filename))

    def stage(name: str, msg: str) -> None:
        r.stages.append((name, msg))
        if on_progress:
            on_progress(name, msg)

    try:
        s = _open_container(data, r, stage)
        _take_inventory(s, r, stage)
        pictures = media.collect(s.members)
        blocks, structure_read = _read_body(s, pictures, r, stage)
        unplaced = _place_pictures(blocks, pictures, r, stage)
        asides = _read_asides(s.members, stage)
        blocks.extend(asides.blocks)
        _count_what_came_through(blocks, r, stage, structure_read)
        _account_for_pictures(pictures, unplaced, asides, r)
    except _Stop:
        return r

    stage("write", "Rebuilding a clean Word file…")
    r.output = docx.write_docx(blocks)
    # What is actually in the file, so somebody can look before they decide
    # whether it was worth anything. From the research, on a paid repair tool:
    # "It just said 'repair successful'. I didn't know if it had actually got
    # anything." A claim with nothing behind it is what breaks trust.
    r.preview_html = docx.blocks_to_html(blocks, inline_images=True)
    r.ok = True
    stage("done", "Done. The rebuilt file is ready to download.")
    return r


def _open_container(data: bytes, r: Repair, stage: Progress) -> salvage.Salvage:
    """Get at the parts, going around the index rather than through it."""
    stage("open", "Opening the file…")
    s = salvage.salvage_members(data)
    for note in s.notes:
        stage("open", _sentence(note))
    # The stage log carries every diagnostic. "What came through" is the list a
    # worried person reads, so it gets outcomes -- what happened to their
    # content -- and not a narration of how the file was opened. The one line
    # about the damaged index is held back until there is recovered content to
    # attach it to, because on a total failure it would be an overclaim.
    r.container_was_damaged = any("internal index was damaged" in n for n in s.notes)
    r.lost.extend(s.unrecovered)

    if not s.members:
        stage("open", "No readable parts were found inside the file.")
        # `salvage` has already said this if it found nothing; saying it twice
        # reads as two separate problems.
        if not any("no readable parts" in l for l in r.lost):
            r.lost.append("the file contained no readable parts")
        raise _Stop
    return s


def _take_inventory(s: salvage.Salvage, r: Repair, stage: Progress) -> None:
    """What is here, what can be rebuilt, and what cannot be done without."""
    stage("inventory", f"Found {plural(len(s.members), 'section')} inside the document.")
    missing = [p for p in ("[Content_Types].xml", "_rels/.rels", DOCUMENT_PART)
               if p not in s.members]
    rebuildable = [p for p in missing if p != DOCUMENT_PART]
    if rebuildable:
        stage("inventory",
              f"Rebuilding {plural(len(rebuildable), 'missing structural part')}…")
        r.recovered.append(
            "rebuilt the file's internal structure, which was missing or damaged — "
            "this is standard plumbing and carries none of your content"
        )

    if DOCUMENT_PART not in s.members:
        stage("inventory", "The main document part is missing and cannot be rebuilt.")
        r.lost.append(
            "the main document part is missing entirely, so there is no text to recover"
        )
        raise _Stop


def _read_body(s: salvage.Salvage, pictures: dict[str, media.Image], r: Repair,
               stage: Progress) -> tuple[list[docx.Block], bool]:
    """Repair the body, then read structure out of it — or fall back and say so."""
    stage("xml", "Checking the document body for damage…")
    fixed, notes = salvage.repair_xml(s.members[DOCUMENT_PART])
    for n in notes:
        stage("xml", _sentence(n))
    r.recovered.extend(notes)
    if not notes:
        stage("xml", "The document body was well-formed.")

    stage("read", "Reading headings, paragraphs and tables…")
    targets = media.relationship_targets(s.members.get(DOC_RELS_PART))
    try:
        blocks = docx.read_blocks(fixed, targets, pictures)
        r.structure_preserved = True
    except Exception:
        stage("read", "The document's structure was too damaged to read. "
                      "Recovering the text on its own instead.")
        blocks = [docx.Block("paragraph", t)
                  for t in salvage.text_runs(fixed) if t.strip()]
        r.structure_preserved = False
        r.lost.append(
            "the document structure was unreadable, so headings, tables and "
            "formatting could not be preserved — only the text was recovered"
        )

    if not blocks and not pictures:
        stage("read", "No readable content was found in the document body.")
        r.lost.append("the document body contained no readable text")
        raise _Stop
    return blocks, r.structure_preserved


def _place_pictures(blocks: list[docx.Block], pictures: dict[str, media.Image],
                    r: Repair, stage: Progress) -> list[media.Image]:
    """Collect at the end the pictures the body could not be shown to reference.

    Either the part naming which file each reference points at did not survive,
    or the text fell back to a run-by-run read that carries no positions. They
    are still the owner's pictures, so they go at the end — an image silently
    placed in the wrong paragraph is worse than one obviously placed last.
    """
    placed = {b.image.name for b in docx.image_blocks(blocks)}
    unplaced = [img for name, img in sorted(pictures.items()) if name not in placed]
    if unplaced:
        stage("read", f"Recovered {plural(len(unplaced), 'picture')} "
                      "whose original position could not be worked out.")
        blocks.append(docx.Block("heading", "Pictures recovered from this document",
                                 level=1))
        blocks.extend(docx.Block("image", text=img.name, image=img) for img in unplaced)
    return unplaced


def _count_what_came_through(blocks: list[docx.Block], r: Repair, stage: Progress,
                             structure_read: bool) -> None:
    r.counts = {
        "headings": sum(1 for b in blocks if b.kind == "heading"),
        "paragraphs": sum(1 for b in blocks if b.kind == "paragraph"),
        "tables": sum(1 for b in blocks if b.kind == "table"),
        "pictures": len(docx.image_blocks(blocks)),
    }
    parts = [plural(r.counts[k], k[:-1])
             for k in ("headings", "paragraphs", "tables", "pictures")
             if r.counts[k]]
    tally = parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + f" and {parts[-1]}"
    stage("read", f"Recovered {tally}.")
    if r.container_was_damaged:
        r.recovered.insert(0, "read your content out of a file whose internal index "
                              "was damaged — the damage Word refuses to open")
    if structure_read:
        r.recovered.append(f"kept {tally}, with their structure intact")
    else:
        r.recovered.append(f"recovered the text of {plural(len(blocks), 'paragraph')}")


def _account_for_pictures(pictures: dict[str, media.Image],
                          unplaced: list[media.Image], asides: "Asides",
                          r: Repair) -> None:
    """Say where the pictures and the asides ended up, and where they did not."""
    placed = len(pictures) - len(unplaced)
    if placed:
        r.recovered.append(
            f"carried {plural(placed, 'picture')} back into the document, "
            + ("where they were" if placed > 1 else "where it was")
        )
    if unplaced:
        r.recovered.append(
            f"recovered {plural(len(unplaced), 'picture')} and set "
            f"{'them' if len(unplaced) > 1 else 'it'} at the end, under a heading"
        )
        them = "they are" if len(unplaced) > 1 else "it is"
        belonged = "they belonged" if len(unplaced) > 1 else "it belonged"
        r.lost.append(
            f"the original position of {plural(len(unplaced), 'picture')} could not "
            f"be recovered, so {them} collected at the end rather than put back "
            f"where {belonged}"
        )
    for label, count in asides.recovered:
        r.recovered.append(
            f"recovered the text of {plural(count, label.lower())}"
            if count > 1 else f"recovered the {label.lower()}"
        )
    if asides.blocks:
        r.lost.append(
            "footnotes, headers and footers could not be put back into their "
            "original places, so their text is set down at the end of the "
            "document under its own heading"
        )
    for label in asides.unreadable:
        r.lost.append(f"the {label.lower()} could not be read")


def _sentence(text: str) -> str:
    text = text[0].upper() + text[1:]
    return text if text.endswith((".", "…", "!", "?")) else text + "."


def _repaired_name(filename: str) -> str:
    stem = filename.rsplit("/", 1)[-1]
    if stem.lower().endswith(".docx"):
        stem = stem[:-5]
    return f"{stem}-repaired.docx"


@dataclass
class Asides:
    """Text that belongs to the document but not to its body."""

    blocks: list[docx.Block] = field(default_factory=list)
    recovered: list[tuple[str, int]] = field(default_factory=list)
    unreadable: list[str] = field(default_factory=list)


def _read_asides(members: dict[str, bytes], stage: Progress) -> Asides:
    """Footnotes, endnotes, headers and footers, as text under their own heading.

    They live in their own parts, so they survive exactly the damage that
    destroys the index — the same reason the pictures do. Dropping them was
    losing content that was sitting right there.

    What this deliberately does not do is fold them into the body. A footnote
    read as a paragraph changes what the document says, and a page header
    repeated as body text reads as something the author wrote. They are set
    down at the end, labelled, and the report says they are not where they were.
    """
    out = Asides()
    for prefix, label in ASIDE_PARTS:
        names = sorted(n for n in members
                       if n == prefix or (prefix.endswith(("header", "footer"))
                                          and n.startswith(prefix)
                                          and n.endswith(".xml")))
        texts: list[str] = []
        for name in names:
            try:
                fixed, _ = salvage.repair_xml(members[name])
                runs = [t.strip() for t in salvage.text_runs(fixed) if t.strip()]
            except Exception:
                out.unreadable.append(label)
                continue
            texts.extend(t for t in runs if t not in _EMPTY_ASIDE)
        if not texts:
            continue
        stage("read", f"Recovering the {label.lower()}…")
        out.blocks.append(docx.Block("heading", f"{label}, recovered separately",
                                     level=2))
        out.blocks.extend(docx.Block("paragraph", t) for t in texts)
        out.recovered.append((label, len(texts)))
    return out
