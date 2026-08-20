"""The web page. This is the product; the CLI is the same engine with a
different front door.

Progress is streamed, not simulated. The repair runs on a worker thread and
pushes each stage onto a queue as it happens, and the response is newline-
delimited JSON the browser reads incrementally. A page that fakes a progress
bar after the work is already done is lying to the person watching it.

Two things are handed over, in this order and never the other way round:

  1. the local rebuild, which needs no key, no network and no account, and
  2. optionally, the same content styled through SuperDocs.

The second is a *second* file, offered after the first is already downloadable.
It was a `--via-superdocs` flag on the CLI and nothing else, which meant the
card's own surface -- API and export -- was reachable only by somebody who had
read the source and set an environment variable. A capability the product's
user cannot reach is not the same failure as an absent one, but on a card
banded *API + export* it is close enough to matter.
"""

from __future__ import annotations

import json
import os
import queue
import threading
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, Response, StreamingResponse

from .docx import Block
from .engine import Repair, repair

app = FastAPI(title="Repair my broken Word doc")


@dataclass
class Held:
    """A finished file, waiting to be collected.

    The blocks ride along so the styling pass does not have to re-open and
    re-parse the file this process just wrote. They are the recovered structure
    itself; sending it as HTML is what the styling pass does with them.
    """

    filename: str
    blob: bytes
    at: float
    blocks: list[Block] = field(default_factory=list)


# Repaired files, held in memory briefly. Nothing is written to disk: the file
# someone uploads is their damaged document, and keeping it is not ours to do.
#
# Downloads are NOT one-shot. Clicking the button twice is the most ordinary
# thing a person does, and the first version dropped the file on the first
# click -- so the second attempt 404'd and their only copy was gone. Entries
# now survive for a short window and are evicted oldest-first.
_READY: "OrderedDict[str, Held]" = OrderedDict()
MAX_BYTES = 20 * 1024 * 1024
KEEP_SECONDS = 30 * 60
KEEP_MOST_RECENT = 32


def _evict(now: float) -> None:
    for token, held in list(_READY.items()):
        if now - held.at > KEEP_SECONDS:
            del _READY[token]
    while len(_READY) > KEEP_MOST_RECENT:
        _READY.popitem(last=False)


def _hold(filename: str, blob: bytes, blocks: list[Block] | None = None) -> str:
    import time as _time

    _evict(_time.monotonic())
    token = uuid.uuid4().hex
    _READY[token] = Held(filename, blob, _time.monotonic(), blocks or [])
    return token


def _ndjson(work, result: dict) -> StreamingResponse:
    """Run `work(say)` on a thread and stream what it says, then the result.

    Both endpoints answer the same way, because both are a person watching
    something slow happen to their document. Factored out rather than written
    twice so a fix to one is a fix to both.
    """
    q: queue.Queue = queue.Queue()

    def run() -> None:
        try:
            work(lambda stage, message: q.put({"stage": stage, "message": message}))
        finally:
            q.put(None)

    threading.Thread(target=run, daemon=True).start()

    def stream():
        while True:
            item = q.get()
            if item is None:
                break
            yield json.dumps(item) + "\n"
        yield json.dumps({"done": True, **result.get("payload", {})}) + "\n"

    return StreamingResponse(stream(), media_type="application/x-ndjson")


def _styling_key() -> str | None:
    key = os.environ.get("SUPERDOCS_API_KEY")
    return key.strip() or None if key else None


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return (Path(__file__).parent.parent / "static" / "index.html").read_text()


@app.get("/api/capabilities")
def capabilities() -> dict:
    """What this particular copy of the page can actually do.

    Asked by the page before it offers anything, so the styling step is either
    genuinely available or plainly explained -- never a button that fails when
    somebody presses it.
    """
    on = _styling_key() is not None
    return {
        "styling": on,
        "note": (
            "Styling is available on this page."
            if on
            else "Styling is switched off on this copy of the page, so the "
            "rebuilt file is the plain one. Nothing else is affected."
        ),
    }


@app.post("/api/repair")
async def api_repair(file: UploadFile):
    data = await file.read()
    if not data:
        raise HTTPException(400, "That file is empty.")
    if len(data) > MAX_BYTES:
        raise HTTPException(413, "That file is larger than 20 MB.")

    result: dict = {}

    def work(say) -> None:
        try:
            r = repair(data, file.filename or "document.docx",
                       on_progress=lambda stage, msg: say(stage, msg))
            token = _hold(r.filename, r.output, _blocks_of(r)) if r.ok else ""
            result["payload"] = r.as_payload(
                f"/api/download/{token}" if r.ok else None,
                f"/api/style/{token}" if r.ok else None,
            )
        except Exception as e:  # never leak a stack trace to a consumer
            # Built from a Repair rather than written out by hand, so this
            # cannot become a second, drifting version of the wire shape --
            # and so the honesty guards, which run over what reaches the page,
            # cover this path too. The exception type never travels: it is
            # engine vocabulary, and BUG-020 was a class name reaching a
            # worried person.
            failed = Repair(filename="")
            failed.lost.append(
                "this file could not be read at all, and nothing was changed")
            failed.stages.append(("open", f"stopped: {type(e).__name__}"))
            result["payload"] = failed.as_payload()
            result["payload"]["summary"] = (
                "Something went wrong while reading this file, so it was left "
                "alone. Nothing was changed and nothing was saved.")

    return _ndjson(work, result)


def _blocks_of(r: Repair) -> list[Block]:
    """The recovered structure, read back out of the file just written.

    Read from the output rather than kept from the run because the output is
    what the person downloads: styling a structure that differs from the file
    in their hands would make the two deliverables disagree.
    """
    import io
    import zipfile

    from .docx import read_blocks

    try:
        with zipfile.ZipFile(io.BytesIO(r.output)) as z:
            return read_blocks(z.read("word/document.xml"))
    except Exception:
        return []


@app.post("/api/style/{token}")
def api_style(token: str):
    """The optional second pass: the four-call SuperDocs contract, on request.

    It is never automatic. Someone whose document just broke should not have it
    sent to a third-party service because a page decided that for them, and the
    plain file is already downloadable before this button exists.
    """
    import time as _time

    _evict(_time.monotonic())
    held = _READY.get(token)
    if held is None:
        raise HTTPException(
            404,
            "That repaired file is no longer being held. Repair the document again "
            "to get a fresh copy — your original was never changed.",
        )
    if not held.blocks:
        raise HTTPException(
            409,
            "There is no readable structure in this result to style. The rebuilt "
            "file above is unchanged and still yours.",
        )
    key = _styling_key()
    if key is None:
        raise HTTPException(
            409,
            "Styling is switched off on this copy of the page. The rebuilt file "
            "above is unchanged and still yours.",
        )

    result: dict = {}

    def work(say) -> None:
        from .styled_export import styled_export
        from .superdocs_client import HttpTransport, SuperDocsClient

        from .styled_export import StyledResult

        try:
            client = SuperDocsClient(HttpTransport(key))
            res = styled_export(
                client, f"salvage-{token[:12]}", held.blocks,
                on_progress=lambda stage, msg: say(stage, msg),
            )
        except Exception:
            # `styled_export` already degrades on everything it can see. This
            # is the belt on top of it: whatever went wrong here, the person
            # keeps the file they already had and is told so in their own
            # words. No exception class travels -- that was BUG-020.
            res = StyledResult()
            res.notes.append("Styling did not work. The rebuilt file above is "
                             "unchanged and still yours.")
        styled_token = ""
        if res.ok:
            styled_token = _hold(_styled_name(held.filename), res.output)
        result["payload"] = {
            "ok": res.ok,
            "notes": res.notes,
            # Two different "no": one where nothing came back, one where
            # something came back and was refused. The page says them
            # differently because they mean different things to a person.
            "rejected_for_content": res.rejected_for_content,
            "filename": _styled_name(held.filename),
            "download": f"/api/download/{styled_token}" if res.ok else None,
            "ops_charged": res.ops_charged,
            "ops_confirmed": res.ops_confirmed,
            "allowance_known": res.allowance_known,
            "allowance_remaining": res.allowance_remaining,
            "warnings": len(res.warnings),
        }

    return _ndjson(work, result)


def _styled_name(filename: str) -> str:
    stem = filename[:-5] if filename.lower().endswith(".docx") else filename
    return f"{stem}-styled.docx"


@app.get("/api/download/{token}")
def download(token: str) -> Response:
    import time as _time

    _evict(_time.monotonic())
    held = _READY.get(token)
    if held is None:
        raise HTTPException(
            404,
            "That repaired file is no longer being held. Repair the document again "
            "to get a fresh copy — your original was never changed.",
        )
    return Response(     # repeat downloads are fine
        held.blob,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f'attachment; filename="{held.filename}"'},
    )


def main() -> None:
    import uvicorn

    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(app, host="127.0.0.1", port=port)


if __name__ == "__main__":
    main()
