"""Getting content out of a DOCX that will not open.

A DOCX is a ZIP of XML parts. It breaks in a handful of recognisable ways, and
each one needs a different move:

  * The ZIP central directory is damaged or truncated -- the file "is not a
    valid archive". Members can often still be found by scanning for local file
    headers, which is what `salvage_members` does.
  * `word/document.xml` is malformed -- an unclosed tag, a raw `&`, a stray
    control character. A strict parser refuses the whole document over one bad
    byte, so we repair the common cases and then fall back to pulling text out
    of the runs directly.
  * A part is simply missing. `[Content_Types].xml` and the relationship parts
    are rebuildable from scratch; the document body is not.

Nothing here promises a full repair. Every function reports what it could not
do, because the owner finding out later is the failure mode this tool exists to
prevent.
"""

from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass, field

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

# A local file header. Scanning for these recovers members when the central
# directory -- which is at the END of the file, and so is what a truncated
# download loses first -- is gone.
_LOCAL_HEADER = b"PK\x03\x04"

# Standard OOXML plumbing. These are regenerated on write, so damage to them
# costs the owner nothing and must not be reported as lost content.
REBUILDABLE = {
    "[Content_Types].xml",
    "_rels/.rels",
    "word/_rels/document.xml.rels",
    "word/styles.xml",
}

_ILLEGAL_XML = re.compile(
    rb"[\x00-\x08\x0b\x0c\x0e-\x1f]"  # control characters XML 1.0 forbids
)
# A bare ampersand: one not already starting a valid entity.
_BARE_AMP = re.compile(rb"&(?!#[0-9]+;|#x[0-9a-fA-F]+;|[a-zA-Z][a-zA-Z0-9]*;)")


def plural(n: int, noun: str, plural_form: str | None = None) -> str:
    """Consumer copy, not log output. "1 table" and "2 tables", never "1 table(s)"."""
    return f"{n} {noun}" if n == 1 else f"{n} {plural_form or noun + 's'}"


@dataclass
class Salvage:
    members: dict[str, bytes] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    unrecovered: list[str] = field(default_factory=list)

    def note(self, msg: str) -> None:
        self.notes.append(msg)

    def lost(self, msg: str) -> None:
        self.unrecovered.append(msg)


def salvage_members(data: bytes) -> Salvage:
    """Get whatever parts can be read out of the container."""
    out = Salvage()

    # The happy path first: a readable archive.
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            bad = z.testzip()
            for name in z.namelist():
                try:
                    out.members[name] = z.read(name)
                except Exception:
                    out.lost(f"one section of the file ({name}) is present but its data is unreadable")
            if bad:
                out.note("part of the file failed its integrity check; read what was readable")
            elif out.members and not out.unrecovered:
                out.note("the container opened cleanly")
            return out
    except (zipfile.BadZipFile, OSError):
        out.note(
            "the file's internal index was damaged — this is what Word reports as "
            "a corrupt document — so the content was read directly from inside it"
        )

    # Damaged container. Re-opening a slice does not help -- a truncated ZIP has
    # no central directory anywhere in it -- so read the local file headers by
    # hand and inflate each member's stream directly.
    found, partial = _recover_by_local_headers(data, out)
    if found:
        out.note(f"found and read {plural(found, 'section')} of the file this way")
    else:
        out.lost("no readable parts could be found inside the file")
    return out


def _recover_by_local_headers(data: bytes, out: "Salvage") -> tuple[int, int]:
    """Walk the local file headers and inflate what each one points at.

    The central directory sits at the END of a ZIP, so a truncated download
    loses it first while the members themselves are still largely intact. Each
    local header carries its own name, method and sizes, which is enough.
    """
    import struct
    import zlib

    found = partial = 0
    for match in re.finditer(re.escape(_LOCAL_HEADER), data):
        start = match.start()
        header = data[start:start + 30]
        if len(header) < 30:
            continue
        try:
            (_, _, flags, method, _, _, _, comp_size, uncomp_size,
             name_len, extra_len) = struct.unpack("<IHHHHHIIIHH", header)
        except struct.error:
            continue

        name_start = start + 30
        name = data[name_start:name_start + name_len]
        if len(name) < name_len:
            continue
        try:
            filename = name.decode("utf-8")
        except UnicodeDecodeError:
            continue
        if not filename or filename.endswith("/") or filename in out.members:
            continue

        body_start = name_start + name_len + extra_len
        # Bit 3 means the sizes were written after the data, in a descriptor we
        # cannot rely on here -- so take everything to the next header instead.
        if flags & 0x08 or comp_size == 0:
            nxt = data.find(_LOCAL_HEADER, body_start)
            body = data[body_start:nxt if nxt != -1 else len(data)]
        else:
            body = data[body_start:body_start + comp_size]

        was_short = len(body) < comp_size
        try:
            if method == 0:
                content = body
            elif method == 8:
                # decompressobj, not decompress: a truncated stream still yields
                # everything up to the cut instead of raising.
                content = zlib.decompressobj(-zlib.MAX_WBITS).decompress(body)
            else:
                continue
        except zlib.error:
            continue

        if not content:
            continue
        out.members[filename] = content
        found += 1
        if was_short or (uncomp_size and len(content) < uncomp_size):
            partial += 1
            # A part we rebuild from scratch anyway carries none of the owner's
            # content, so listing it as a loss would tell someone they lost
            # something they did not.
            if filename not in REBUILDABLE:
                out.lost(
                    f"one section of the file ({filename}) was cut short by the "
                    "damage and was recovered only up to the break"
                )
    return found, partial


def repair_xml(raw: bytes) -> tuple[bytes, list[str]]:
    """Fix the XML faults a strict parser refuses to look past."""
    notes: list[str] = []
    fixed = raw

    if fixed.startswith(b"\xef\xbb\xbf"):
        fixed = fixed[3:]
        notes.append("removed a byte-order mark that was placed before the XML declaration")

    cleaned, n = _ILLEGAL_XML.subn(b"", fixed)
    if n:
        fixed = cleaned
        notes.append(f"removed {plural(n, 'invalid character')} that were breaking the document")

    cleaned, n = _BARE_AMP.subn(b"&amp;", fixed)
    if n:
        fixed = cleaned
        notes.append(f"repaired {plural(n, 'stray ampersand')}")

    closed, added = _close_open_tags(fixed)
    if added:
        fixed = closed
        notes.append(
            f"closed {plural(len(added), 'element')} that the damage had left open")

    return fixed, notes


def _close_open_tags(raw: bytes) -> tuple[bytes, list[str]]:
    """Close tags a truncated file left hanging.

    Deliberately simple: it only appends closing tags for elements still open at
    the end. It does not try to reorder or invent content -- guessing at
    structure is how a "repair" quietly changes what a document says.
    """
    try:
        text = raw.decode("utf-8", errors="replace")
    except Exception:
        return raw, []

    stack: list[str] = []
    for m in re.finditer(r"<\s*(/?)([A-Za-z_][\w:.\-]*)([^>]*?)(/?)\s*>", text):
        closing, name, attrs, self_closing = m.groups()
        if name.startswith("?") or name.startswith("!"):
            continue
        if self_closing == "/":
            continue
        if closing == "/":
            if stack and stack[-1] == name:
                stack.pop()
            elif name in stack:
                while stack and stack.pop() != name:
                    pass
        else:
            stack.append(name)

    if not stack:
        return raw, []
    tail = "".join(f"</{n}>" for n in reversed(stack))
    return raw + tail.encode("utf-8"), list(reversed(stack))


def text_runs(document_xml: bytes) -> list[str]:
    """Last resort: pull the text out of `<w:t>` runs with a regex.

    Used when the XML is too damaged to parse even after repair. It loses all
    structure, which is exactly why the report says so rather than presenting
    the result as a recovered document.
    """
    return [
        _unescape(m.group(1).decode("utf-8", errors="replace"))
        for m in re.finditer(rb"<w:t[^>]*>(.*?)</w:t>", document_xml, re.S)
        if m.group(1).strip()
    ]


def _unescape(s: str) -> str:
    return (s.replace("&lt;", "<").replace("&gt;", ">")
             .replace("&quot;", '"').replace("&apos;", "'").replace("&amp;", "&"))
