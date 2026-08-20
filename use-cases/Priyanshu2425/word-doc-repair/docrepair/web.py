"""The web page. This is the product; the CLI is the same engine with a
different front door.

Progress is streamed, not simulated. The repair runs on a worker thread and
pushes each stage onto a queue as it happens, and the response is newline-
delimited JSON the browser reads incrementally. A page that fakes a progress
bar after the work is already done is lying to the person watching it.
"""

from __future__ import annotations

import json
import queue
import threading
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, Response, StreamingResponse

from .engine import Repair, repair

app = FastAPI(title="Repair my broken Word doc")

# Repaired files, held in memory briefly. Nothing is written to disk: the file
# someone uploads is their damaged document, and keeping it is not ours to do.
#
# Downloads are NOT one-shot. Clicking the button twice is the most ordinary
# thing a person does, and the first version dropped the file on the first
# click -- so the second attempt 404'd and their only copy was gone. Entries
# now survive for a short window and are evicted oldest-first.
_READY: "OrderedDict[str, tuple[str, bytes, float]]" = None  # set below
MAX_BYTES = 20 * 1024 * 1024
KEEP_SECONDS = 30 * 60
KEEP_MOST_RECENT = 32

from collections import OrderedDict  # noqa: E402

_READY = OrderedDict()


def _evict(now: float) -> None:
    for token, (_, _, at) in list(_READY.items()):
        if now - at > KEEP_SECONDS:
            del _READY[token]
    while len(_READY) > KEEP_MOST_RECENT:
        _READY.popitem(last=False)


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return (Path(__file__).parent.parent / "static" / "index.html").read_text()


@app.post("/api/repair")
async def api_repair(file: UploadFile):
    data = await file.read()
    if not data:
        raise HTTPException(400, "That file is empty.")
    if len(data) > MAX_BYTES:
        raise HTTPException(413, "That file is larger than 20 MB.")

    q: queue.Queue = queue.Queue()
    result: dict = {}

    def work() -> None:
        try:
            r = repair(data, file.filename or "document.docx",
                       on_progress=lambda stage, msg: q.put({"stage": stage, "message": msg}))
            token = uuid.uuid4().hex
            if r.ok:
                import time as _time

                _evict(_time.monotonic())
                _READY[token] = (r.filename, r.output, _time.monotonic())
            result["report"] = r.as_payload(
                f"/api/download/{token}" if r.ok else None)
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
            result["report"] = failed.as_payload()
            result["report"]["summary"] = (
                "Something went wrong while reading this file, so it was left "
                "alone. Nothing was changed and nothing was saved.")
        finally:
            q.put(None)

    threading.Thread(target=work, daemon=True).start()

    def stream():
        while True:
            item = q.get()
            if item is None:
                break
            yield json.dumps(item) + "\n"
        yield json.dumps({"done": True, **result.get("report", {})}) + "\n"

    return StreamingResponse(stream(), media_type="application/x-ndjson")


@app.get("/api/download/{token}")
def download(token: str) -> Response:
    import time as _time

    _evict(_time.monotonic())
    if token not in _READY:
        raise HTTPException(
            404,
            "That repaired file is no longer being held. Repair the document again "
            "to get a fresh copy — your original was never changed.",
        )
    name, blob, _ = _READY[token]   # repeat downloads are fine
    return Response(
        blob,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


def main() -> None:
    import os

    import uvicorn

    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(app, host="127.0.0.1", port=port)


if __name__ == "__main__":
    main()
