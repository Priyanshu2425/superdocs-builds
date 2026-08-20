"""Read structure out of a damaged document part, and write a valid one back.

The writer matters as much as the reader. A tool that salvages text and hands
back a .txt has not repaired anything -- the owner wanted a Word file. So this
builds a real DOCX: a correct `[Content_Types].xml`, the two relationship
parts, a stylesheet with actual heading styles, and a document body.

The output is deliberately plain. Recovering someone's original theme is not
possible from a broken file, and pretending otherwise would be the "silent
claim of a full fix" the brief warns against. What it guarantees is a file that
opens, with headings that are headings and tables that are tables.
"""

from __future__ import annotations

import base64
import re
import zipfile
from dataclasses import dataclass
from xml.etree import ElementTree as ET

from . import media
from .media import Image
from .salvage import W

def content_types(extensions: list[str] | None = None) -> str:
    """Word will not open a package whose parts have no declared type, so an
    image extension carried into the rebuild has to be declared here as well as
    written into the archive. Missing this produces a file that opens on some
    machines and is called corrupt on others, which is the worst of the three
    possible outcomes."""
    defaults = "".join(
        f'<Default Extension="{e}" ContentType="{media.CONTENT_TYPES[e]}"/>'
        for e in sorted(set(extensions or [])) if e in media.CONTENT_TYPES
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        + defaults +
        '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        '<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
        "</Types>"
    )


CONTENT_TYPES = content_types()

ROOT_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>"""

IMAGE_REL = ("http://schemas.openxmlformats.org/officeDocument/2006/"
             "relationships/image")


def doc_rels(images: list[tuple[str, str]] | None = None) -> str:
    """`rId1` is always the stylesheet; images take rId2 upwards."""
    extra = "".join(
        f'<Relationship Id="{rid}" Type="{IMAGE_REL}" Target="{target}"/>'
        for rid, target in (images or [])
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
        + extra + "</Relationships>"
    )


DOC_RELS = doc_rels()


def _style(sid: str, name: str, size: int, bold: bool, outline: int | None) -> str:
    outline_xml = f'<w:outlineLvl w:val="{outline}"/>' if outline is not None else ""
    return (
        f'<w:style w:type="paragraph" w:styleId="{sid}">'
        f'<w:name w:val="{name}"/><w:qFormat/>'
        f'<w:pPr><w:spacing w:before="240" w:after="120"/>{outline_xml}</w:pPr>'
        f'<w:rPr><w:rFonts w:ascii="Calibri" w:hAnsi="Calibri"/>'
        f'<w:sz w:val="{size}"/>{"<w:b/>" if bold else ""}</w:rPr></w:style>'
    )


STYLES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
    '<w:docDefaults><w:rPrDefault><w:rPr>'
    '<w:rFonts w:ascii="Calibri" w:hAnsi="Calibri"/><w:sz w:val="22"/>'
    '</w:rPr></w:rPrDefault></w:docDefaults>'
    + _style("Normal", "Normal", 22, False, None)
    + _style("Title", "Title", 56, True, 0)
    + _style("Heading1", "heading 1", 32, True, 0)
    + _style("Heading2", "heading 2", 26, True, 1)
    + _style("Heading3", "heading 3", 24, True, 2)
    + "</w:styles>"
)


@dataclass
class Block:
    kind: str          # "heading" | "paragraph" | "table" | "image"
    text: str = ""
    level: int = 1
    rows: list[list[str]] | None = None
    image: Image | None = None


def read_blocks(document_xml: bytes, targets: dict[str, str] | None = None,
                images: dict[str, Image] | None = None) -> list[Block]:
    """Parse the body into blocks, keeping the structure that survived.

    `targets` and `images` are what makes a picture land back where it was: the
    relationship map says which file a drawing points at, and without it a
    drawing is a reference to nothing. Both optional, because both parts can be
    damaged, and a document with unplaceable images is still a document.
    """
    targets = targets or {}
    images = images or {}
    root = ET.fromstring(document_xml)
    body = root.find(f"{W}body")
    if body is None:
        return []

    blocks: list[Block] = []
    for el in body:
        if el.tag == f"{W}p":
            for rid in media.embedded_ids(el):
                img = images.get(targets.get(rid, ""))
                if img is not None:
                    blocks.append(Block("image", text=img.name, image=img))
            text = _para_text(el)
            if not text.strip():
                continue
            level = _heading_level(el)
            blocks.append(
                Block("heading", text, level) if level else Block("paragraph", text)
            )
        elif el.tag == f"{W}tbl":
            rows = []
            for tr in el.findall(f"{W}tr"):
                rows.append([_cell_text(tc) for tc in tr.findall(f"{W}tc")])
            if rows:
                blocks.append(Block("table", rows=rows))
    return blocks


def _para_text(p: ET.Element) -> str:
    return "".join(t.text or "" for t in p.iter(f"{W}t"))


def _cell_text(tc: ET.Element) -> str:
    return " ".join(_para_text(p) for p in tc.findall(f"{W}p")).strip()


def _heading_level(p: ET.Element) -> int | None:
    ppr = p.find(f"{W}pPr")
    if ppr is None:
        return None
    style = ppr.find(f"{W}pStyle")
    if style is None:
        return None
    val = style.get(f"{W}val", "")
    m = re.fullmatch(r"[Hh]eading\s*([1-9])", val)
    if m:
        return int(m.group(1))
    if val.lower() in {"title"}:
        return 1
    return None


def _esc(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _p(text: str, style: str | None = None) -> str:
    ppr = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
    return f"<w:p>{ppr}<w:r><w:t xml:space=\"preserve\">{_esc(text)}</w:t></w:r></w:p>"


def _table(rows: list[list[str]]) -> str:
    out = [
        '<w:tbl><w:tblPr><w:tblStyle w:val="TableGrid"/>'
        '<w:tblW w:w="0" w:type="auto"/>'
        '<w:tblBorders>'
        + "".join(
            f'<w:{e} w:val="single" w:sz="4" w:color="BFBFBF"/>'
            for e in ("top", "left", "bottom", "right", "insideH", "insideV")
        )
        + "</w:tblBorders></w:tblPr>"
    ]
    for row in rows:
        out.append("<w:tr>")
        for cell in row:
            out.append(
                '<w:tc><w:tcPr><w:tcW w:w="0" w:type="auto"/></w:tcPr>'
                + _p(cell) + "</w:tc>"
            )
        out.append("</w:tr>")
    out.append("</w:tbl>")
    return "".join(out)


def image_blocks(blocks: list[Block]) -> list[Block]:
    return [b for b in blocks if b.kind == "image" and b.image is not None]


def _rel_ids(blocks: list[Block]) -> dict[str, str]:
    """One relationship id per distinct image, starting after the stylesheet.

    Keyed by member name rather than by position, so the same picture used twice
    is carried once and referenced twice — which is how the original stored it.
    """
    ids: dict[str, str] = {}
    for b in image_blocks(blocks):
        ids.setdefault(b.image.name, f"rId{len(ids) + 2}")
    return ids


def build_document_xml(blocks: list[Block]) -> str:
    rel_ids = _rel_ids(blocks)
    body = []
    for n, b in enumerate(blocks, 1):
        if b.kind == "image" and b.image is not None:
            body.append(media.drawing_xml(rel_ids[b.image.name], b.image, n,
                                          b.image.name.rsplit("/", 1)[-1]))
        elif b.kind == "heading":
            body.append(_p(b.text, f"Heading{min(max(b.level, 1), 3)}"))
        elif b.kind == "table" and b.rows:
            body.append(_table(b.rows))
            body.append(_p(""))
        else:
            body.append(_p(b.text))
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
        ' xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
        ' xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"'
        ' xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'
        ' xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture">'
        "<w:body>" + "".join(body) +
        '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/>'
        '<w:pgMar w:top="1134" w:right="1134" w:bottom="1134" w:left="1134"/></w:sectPr>'
        "</w:body></w:document>"
    )


def write_docx(blocks: list[Block]) -> bytes:
    import io

    rel_ids = _rel_ids(blocks)
    by_name = {b.image.name: b.image for b in image_blocks(blocks)}
    extensions = [img.ext for img in by_name.values()]

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        # mimetype-equivalent ordering is not required for OOXML, but the
        # content types part must be present and first is conventional.
        z.writestr("[Content_Types].xml", content_types(extensions))
        z.writestr("_rels/.rels", ROOT_RELS)
        # document.xml goes early, as Word writes it. The order is not
        # cosmetic: a truncated download loses the END of the file, so the part
        # carrying the content is the one you want furthest from the cut.
        z.writestr("word/document.xml", build_document_xml(blocks))
        z.writestr("word/_rels/document.xml.rels",
                   doc_rels([(rel_ids[n], n.split("word/", 1)[-1])
                             for n in by_name]))
        z.writestr("word/styles.xml", STYLES)
        for name, img in by_name.items():
            # Already compressed. Deflating a PNG again costs time and saves
            # nothing, and this runs while somebody is watching a progress bar.
            z.writestr(name, img.data, zipfile.ZIP_STORED)
    return buf.getvalue()


def blocks_to_html(blocks: list[Block], inline_images: bool = False,
                   image_budget: int = 3_000_000) -> str:
    """For the SuperDocs path, which takes documents and HTML, never raw
    Word XML -- the docs are explicit that there is no endpoint for that.

    `inline_images` is for the preview the page shows before anybody downloads
    anything: the pictures have to be visible for the preview to answer the
    question it exists to answer. They are embedded as data URIs up to a budget,
    and past it the image is named rather than shown -- an image that silently
    fails to load in a preview would read as an image that was not recovered.
    """
    out = []
    spent = 0
    for b in blocks:
        if b.kind == "image" and b.image is not None:
            if not inline_images:
                continue
            if spent + len(b.image.data) > image_budget:
                out.append(
                    '<p class="image-omitted">An image was recovered and is in '
                    "the file. It is not shown here because the preview would be "
                    "too large to load.</p>"
                )
                continue
            spent += len(b.image.data)
            src = ("data:" + b.image.content_type + ";base64,"
                   + base64.b64encode(b.image.data).decode("ascii"))
            out.append(f'<img src="{src}" alt="A picture recovered from your document">')
        elif b.kind == "heading":
            lvl = min(max(b.level, 1), 3)
            out.append(f"<h{lvl}>{_esc(b.text)}</h{lvl}>")
        elif b.kind == "table" and b.rows:
            rows = "".join(
                "<tr>" + "".join(f"<td>{_esc(c)}</td>" for c in r) + "</tr>"
                for r in b.rows
            )
            out.append(f"<table>{rows}</table>")
        else:
            out.append(f"<p>{_esc(b.text)}</p>")
    return "\n".join(out)
