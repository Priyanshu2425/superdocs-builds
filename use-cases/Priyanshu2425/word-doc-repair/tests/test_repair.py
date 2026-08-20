"""Keyless, offline. Every test starts from a real DOCX and breaks it."""

import io
import zipfile

import pytest

from docrepair import repair
from docrepair.docx import read_blocks
from tests import broken


def opens_cleanly(data: bytes) -> list:
    """The reviewer's bar: the output is a valid, readable Word file."""
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        assert z.testzip() is None
        for required in ("[Content_Types].xml", "_rels/.rels",
                         "word/document.xml", "word/styles.xml"):
            assert required in z.namelist(), f"output is missing {required}"
        return read_blocks(z.read("word/document.xml"))


def test_a_healthy_document_round_trips_unchanged():
    r = repair(broken.healthy(), "fine.docx")
    assert r.ok
    blocks = opens_cleanly(r.output)
    # The spacer paragraph the writer puts after a table is empty, and empty
    # paragraphs are skipped on read -- so five blocks, not six.
    assert [b.kind for b in blocks] == ["heading", "paragraph", "heading",
                                        "table", "paragraph"]


def test_a_truncated_container_still_yields_a_valid_file():
    r = repair(broken.truncated_container(), "cut.docx")
    assert r.ok
    opens_cleanly(r.output)
    assert any("internal index was damaged" in n for n in r.recovered)


def test_a_missing_content_types_part_is_rebuilt_and_said_so():
    r = repair(broken.missing_content_types(), "ct.docx")
    assert r.ok
    opens_cleanly(r.output)
    assert any("rebuilt the file's internal structure" in n
               and "carries none of your content" in n for n in r.recovered)


def test_unclosed_tags_are_closed_and_the_text_survives():
    r = repair(broken.unclosed_tags(), "cut.docx")
    assert r.ok
    blocks = opens_cleanly(r.output)
    assert any("Quarterly Report" in b.text for b in blocks)
    assert any("closed" in n for n in r.recovered)


def test_bare_ampersands_and_control_characters_are_repaired():
    r = repair(broken.bare_ampersand_and_control_chars(), "amp.docx")
    assert r.ok
    blocks = opens_cleanly(r.output)
    assert any("renewals & upsell" in b.text for b in blocks)
    assert any("ampersand" in n for n in r.recovered)
    assert any("invalid character" in n for n in r.recovered)


def test_the_table_survives_as_a_table_not_as_text():
    """Structure is the thing people lose. Assert it is actually structure."""
    r = repair(broken.healthy(), "t.docx")
    blocks = opens_cleanly(r.output)
    tables = [b for b in blocks if b.kind == "table"]
    assert len(tables) == 1
    assert tables[0].rows[0] == ["Region", "Revenue"]
    assert len(tables[0].rows) == 3


def test_headings_survive_as_headings():
    r = repair(broken.healthy(), "h.docx")
    blocks = opens_cleanly(r.output)
    headings = [b for b in blocks if b.kind == "heading"]
    assert [h.text for h in headings] == ["Quarterly Report", "Regional breakdown"]
    assert [h.level for h in headings] == [1, 2]


# -- the honest-failure half -------------------------------------------------

def test_a_missing_document_part_fails_and_says_why():
    r = repair(broken.missing_document_part(), "gone.docx")
    assert not r.ok
    assert r.output == b""
    assert "could not be repaired" in r.summary()
    assert any("missing entirely" in l for l in r.lost)


def test_a_file_that_was_never_a_docx_fails_cleanly():
    r = repair(broken.not_a_zip_at_all(), "notes.txt")
    assert not r.ok
    assert "could not be repaired" in r.summary()
    assert "Nothing was invented" in r.summary()


def test_an_empty_body_is_reported_rather_than_returned_as_success():
    """An empty valid file is the most dangerous output: it opens fine, so the
    owner may not notice their content is gone until much later."""
    r = repair(broken.empty_body(), "empty.docx")
    assert not r.ok
    assert any("no readable text" in l for l in r.lost)


def test_no_wording_anywhere_claims_a_complete_or_guaranteed_repair():
    for maker in (broken.healthy, broken.truncated_container, broken.unclosed_tags):
        r = repair(maker(), "x.docx")
        text = " ".join([r.summary(), *r.recovered, *(m for _, m in r.stages)]).lower()
        for forbidden in ("fully repaired", "completely repaired", "fully restored",
                          "guaranteed", "perfect", "as good as new", "100%"):
            assert forbidden not in text, f"output claims too much: {forbidden!r}"
        assert "best-effort" in r.summary().lower()


def test_a_structure_loss_is_stated_not_hidden():
    """When only text could be recovered, the user must be told the headings and
    tables are gone — that is exactly the loss they would otherwise find later."""
    from docrepair import engine

    r = engine.Repair()
    r.ok = True
    r.structure_preserved = False
    assert "headings, tables and formatting are gone" in r.summary()


def test_progress_is_reported_stage_by_stage():
    seen = []
    repair(broken.truncated_container(), "p.docx", on_progress=lambda s, m: seen.append(s))
    assert seen[0] == "open"
    assert seen[-1] == "done"
    assert {"inventory", "read", "write"} <= set(seen)


def test_a_rebuilt_structural_part_is_never_reported_as_a_loss():
    """Telling someone they lost `document.xml.rels` is telling them they lost
    something they did not — it is regenerated, and it carries none of their
    content. Nothing in the losses list may name a rebuildable part."""
    from docrepair.salvage import REBUILDABLE

    r = repair(broken.truncated_container(), "cut.docx")
    assert r.ok
    for part in REBUILDABLE:
        assert not any(part in l for l in r.lost), f"reported {part} as lost content"


def test_the_failure_sentence_reads_as_a_sentence():
    r = repair(broken.missing_document_part(), "gone.docx")
    body = r.summary().split("This file could not be repaired. ", 1)[1]
    assert body[0].isupper()
    assert ". Nothing was invented" in r.summary()


def test_the_user_facing_lists_are_outcomes_not_diagnostics():
    """"What came through" is read by someone worried about their document, not
    by an engineer reading a log. Narration of how the file was opened belongs
    in the stage log; this list is about what happened to their content."""
    jargon = ("ZIP", "XML", "CRC", "local header", "zlib", "central directory",
              "ParseError", "Exception", "traceback")
    # Every fixture, and the stage log too -- the stage log is what a user
    # watches while it works, so a leak there is just as visible.
    for maker in (broken.truncated_container, broken.unclosed_tags,
                  broken.missing_content_types, broken.not_a_zip_at_all,
                  broken.bare_ampersand_and_control_chars, broken.empty_body):
        r = repair(maker(), "x.docx")
        joined = " ".join(r.recovered + r.lost + [m for _, m in r.stages])
        for word in jargon:
            assert word not in joined, f"{maker.__name__} leaks {word!r}: {joined}"


def test_counts_are_pluralised_like_english():
    from docrepair.salvage import plural

    assert plural(1, "table") == "1 table"
    assert plural(2, "table") == "2 tables"
    r = repair(broken.healthy(), "h.docx")
    assert "1 table," in " ".join(r.recovered) or "1 table " in " ".join(r.recovered)
    assert "(s)" not in " ".join(r.recovered + r.lost + [m for _, m in r.stages])


def test_a_total_failure_claims_nothing_came_through():
    """The overclaim the manual harness caught: a file that yielded nothing
    still rendered a "what came through" bullet, because the container note was
    appended before anything had been read. A recovery claim on a screen where
    nothing was recovered is exactly what this build promises not to make."""
    for maker in (broken.not_a_zip_at_all, broken.missing_document_part,
                  broken.empty_body):
        r = repair(maker(), "x.docx")
        assert not r.ok
        assert r.recovered == [], f"{maker.__name__} claims {r.recovered} on a failure"


def test_a_loss_is_never_reported_twice():
    """Two phrasings of one problem read to a user as two problems."""
    for maker in (broken.not_a_zip_at_all, broken.missing_document_part,
                  broken.empty_body, broken.truncated_container):
        r = repair(maker(), "x.docx")
        assert len(r.lost) == len(set(r.lost))
        no_parts = [l for l in r.lost if "no readable parts" in l]
        assert len(no_parts) <= 1, r.lost


# -- the pictures, and the page furniture -----------------------------------
#
# "It was a report with photos and a table of numbers... the photos I got off my
# phone again — the ones I still had." The words came back and the pictures did
# not, and re-sourcing the pictures was most of the work.

def _reopen(data: bytes):
    return zipfile.ZipFile(io.BytesIO(data))


def test_a_picture_comes_back_in_the_rebuilt_file():
    r = repair(broken.illustrated(), "site.docx")
    assert r.ok
    with _reopen(r.output) as z:
        names = z.namelist()
        assert "word/media/image1.png" in names
        assert z.read("word/media/image1.png") == broken.PICTURE
        assert b"<w:drawing>" in z.read("word/document.xml")


def test_the_rebuilt_file_declares_the_picture_so_word_will_open_it():
    """A part with no declared type produces a file that opens on some machines
    and is called corrupt on others — the worst of the three outcomes."""
    r = repair(broken.illustrated(), "site.docx")
    with _reopen(r.output) as z:
        types = z.read("[Content_Types].xml").decode()
        rels = z.read("word/_rels/document.xml.rels").decode()
        body = z.read("word/document.xml").decode()
    assert 'Extension="png"' in types and "image/png" in types
    assert "media/image1.png" in rels
    rel_id = rels.split('Id="', 2)[2].split('"')[0]
    assert f'r:embed="{rel_id}"' in body, "the drawing points at a relationship that exists"


def test_a_picture_survives_the_damage_that_destroys_the_index():
    """Media are separate members with their own local headers, which is why
    they survive exactly the truncation Word refuses to open."""
    r = repair(broken.truncated_illustrated(), "site.docx")
    assert r.ok and r.counts["pictures"] == 1
    with _reopen(r.output) as z:
        assert z.read("word/media/image1.png") == broken.PICTURE


def test_a_picture_whose_position_is_lost_is_kept_and_the_report_says_where():
    """Silently placing it in the wrong paragraph would be worse than obviously
    placing it at the end."""
    r = repair(broken.illustrated_without_its_map(), "site.docx")
    assert r.ok and r.counts["pictures"] == 1
    assert any("set it at the end" in n for n in r.recovered)
    assert any("original position" in n for n in r.lost)
    with _reopen(r.output) as z:
        assert b"Pictures recovered from this document" in z.read("word/document.xml")


def test_footnotes_headers_and_footers_come_back_as_text_and_say_they_moved():
    r = repair(broken.illustrated(), "site.docx")
    with _reopen(r.output) as z:
        body = z.read("word/document.xml").decode()
    assert "Measurements taken with a laser rangefinder." in body
    assert "Inspection report" in body and "Page 1 of 1" in body
    assert "Footnotes, recovered separately" in body
    # And the report does not let that pass as a full recovery.
    assert any("could not be put back into their original places" in l for l in r.lost)


def test_an_aside_is_never_folded_into_the_body_where_it_would_change_the_meaning():
    r = repair(broken.illustrated(), "site.docx")
    with _reopen(r.output) as z:
        body = z.read("word/document.xml").decode()
    assert body.index("No further defects") < body.index("Measurements taken")


def test_a_document_with_no_pictures_says_nothing_about_pictures():
    """Absence is stated where it is true, and not where it is not."""
    r = repair(broken.healthy(), "q.docx")
    assert r.counts["pictures"] == 0
    assert not any("picture" in n for n in r.recovered + r.lost)


def test_the_size_is_read_out_of_the_image_rather_than_assumed():
    from docrepair import media

    assert media.image_size(broken.PICTURE) == (8, 6)
    assert media.image_size(b"not an image at all") is None
    img = media.collect({"word/media/x.bin": b"not an image at all"})["word/media/x.bin"]
    assert not img.measured, "an unmeasurable image must not claim a measurement"
    assert (img.width_px, img.height_px) == media.FALLBACK_PX


def test_a_very_wide_picture_is_scaled_to_the_page_and_keeps_its_shape():
    from docrepair.media import MAX_WIDTH_EMU, Image

    wide = Image("word/media/w.png", b"", width_px=4000, height_px=1000, measured=True)
    cx, cy = wide.extent
    assert cx == MAX_WIDTH_EMU
    assert abs(cx / cy - 4.0) < 0.01


# -- seeing it before taking it ---------------------------------------------

def test_the_report_carries_a_preview_of_what_is_in_the_file():
    """'It just said repair successful. I didn't know if it had actually got
    anything.' A claim with nothing behind it is what breaks trust."""
    r = repair(broken.illustrated(), "site.docx")
    assert "<h1>Site inspection</h1>" in r.preview_html
    assert "No further defects were observed." in r.preview_html
    assert 'src="data:image/png;base64,' in r.preview_html


def test_a_failure_carries_no_preview_because_there_is_nothing_to_preview():
    r = repair(broken.missing_document_part(), "x.docx")
    assert not r.ok and r.preview_html == ""


def test_a_preview_too_large_to_load_names_the_picture_instead_of_dropping_it():
    from docrepair import docx as _docx
    from docrepair.media import Image

    blocks = [_docx.Block("image", text="word/media/big.png",
                          image=Image("word/media/big.png", b"x" * 100,
                                      width_px=10, height_px=10, measured=True))]
    html = _docx.blocks_to_html(blocks, inline_images=True, image_budget=10)
    assert "image-omitted" in html and "is in the file" in html
