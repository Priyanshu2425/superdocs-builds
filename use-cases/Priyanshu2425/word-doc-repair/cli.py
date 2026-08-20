#!/usr/bin/env python3
"""Same engine as the web page, for people who live in a terminal.

The web page is the product. This exists because a batch of fifty files is not
a drag-and-drop job, and because a shared engine means the two front doors can
never disagree about what was recovered.

    python3 cli.py broken.docx
    python3 cli.py broken.docx -o fixed.docx
    python3 cli.py *.docx --quiet
    python3 cli.py broken.docx --via-superdocs   # also style it through SuperDocs
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from docrepair import repair
from docrepair.docx import read_blocks


def _style_through_superdocs(r, quiet: bool):
    """Optional second pass. Never allowed to cost the caller the local result."""
    import io
    import os
    import zipfile

    from docrepair.styled_export import styled_export
    from docrepair.superdocs_client import HttpTransport, SuperDocsClient

    key = os.environ.get("SUPERDOCS_API_KEY")
    if not key:
        print("  SUPERDOCS_API_KEY is not set; keeping the plain rebuild.", file=sys.stderr)
        return None
    with zipfile.ZipFile(io.BytesIO(r.output)) as z:
        blocks = read_blocks(z.read("word/document.xml"))
    client = SuperDocsClient(HttpTransport(key))
    res = styled_export(
        client, f"repair-{os.getpid()}", blocks,
        on_progress=None if quiet else lambda s, m: print(f"  {m}", file=sys.stderr),
    )
    return res


def one(path: Path, out: Path | None, quiet: bool, via_superdocs: bool = False) -> bool:
    data = path.read_bytes()
    r = repair(data, path.name,
               on_progress=None if quiet else lambda s, m: print(f"  {m}", file=sys.stderr))

    print(f"\n{path.name}")
    print(f"  {r.summary()}")
    for line in r.recovered:
        print(f"  + {line}")
    for line in r.lost:
        print(f"  - {line}")

    if not r.ok:
        return False

    blob = r.output
    if via_superdocs:
        styled = _style_through_superdocs(r, quiet)
        if styled and styled.ok:
            blob = styled.output
            qualifier = "" if styled.ops_confirmed else " estimated"
            unit = "operation" if styled.ops_charged == 1 else "operations"
            print(f"  + styled through SuperDocs ({styled.ops_charged}{qualifier} {unit})")
        elif styled:
            for n in styled.notes[-1:]:
                print(f"  · {n}")

    dest = out or path.with_name(r.filename)
    dest.write_bytes(blob)
    print(f"  → {dest}")
    return True


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("files", nargs="+", type=Path)
    p.add_argument("-o", "--out", type=Path, help="output path (single input only)")
    p.add_argument("-q", "--quiet", action="store_true", help="hide per-stage progress")
    p.add_argument("--via-superdocs", action="store_true",
                   help="also style the result through SuperDocs (needs SUPERDOCS_API_KEY)")
    a = p.parse_args(argv)

    if a.out and len(a.files) > 1:
        p.error("--out takes a single input file")

    failures = 0
    for f in a.files:
        if not f.exists():
            print(f"{f}: no such file", file=sys.stderr)
            failures += 1
            continue
        if not one(f, a.out, a.quiet, a.via_superdocs):
            failures += 1

    if failures:
        print(f"\n{failures} of {len(a.files)} file(s) could not be repaired.", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
