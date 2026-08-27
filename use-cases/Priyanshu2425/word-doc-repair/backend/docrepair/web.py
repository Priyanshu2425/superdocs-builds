"""The web page. This is the product; the CLI is the same engine with a
different front door.

Progress is streamed as Server-Sent Events, not simulated: the recovery runs on
a worker thread and pushes each stage as it happens, and the response ends with
a final event carrying the manifest. A page that fakes a progress bar after the
work is already done is lying to the person watching it.

One flow, one file at the end of it. The brief for this build says a strong
result is a reviewer running a broken DOCX through the tool and getting "a
valid, **styled** file back with a clear summary of what was recovered", so the
styling pass is part of the recovery and not a button beside it.

  1. the local rebuild reads the damaged file and writes a clean one, and
  2. that file goes to SuperDocs to be styled, and the styled file is what is
     handed over.

Step 2 can fail in every way a network call can fail. When it does, the person
gets the local rebuild and a sentence saying why — the plain file is the
fallback, not the product. Both files stay downloadable either way, because
somebody who preferred the plain one should not have to run it again to get it.
"""

from __future__ import annotations

import json
import os
import queue
import re
import shutil
import tempfile
import threading
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, Response, StreamingResponse

app = FastAPI(title="Salvage — recover a broken Word doc")

MAX_BYTES = 20 * 1024 * 1024
KEEP_MOST_RECENT = 64

#: The sentence a stale token gets everywhere it can be asked about — the
#: download route, the retry route, and every new counter route below. One
#: string, reused, so it never drifts between call sites.
NOT_HELD = (
    "That repaired file is no longer being held. Repair the document "
    "again to get a fresh copy — your original was never changed."
)

# Repaired files held briefly in memory. Nothing is written to disk beyond the
# local rebuild the engine itself writes; this map only remembers where it is so
# a download or a styling pass can find it again.
_READY: dict[str, str] = {}
#: When each `_READY` entry was (re)created — the other half of the retention
#: promise the count-based cap alone never enforced. See `_sweep`.
_READY_AT: dict[str, float] = {}


def _hold(path: str) -> str:
    while len(_READY) >= KEEP_MOST_RECENT:
        oldest = next(iter(_READY))
        _READY.pop(oldest)
        _READY_AT.pop(oldest, None)
    token = uuid.uuid4().hex
    _READY[token] = path
    _READY_AT[token] = time.time()
    return token


# -- the styling counter's sessions ------------------------------------------
#
# `superdocs_client.style()` mints a session and discards it -- fine for one
# formatting pass, not for a conversation. A `StyleSession` is what lets a
# `session_id` survive between turns. See docs/PRD-SET-IT-YOUR-WAY.md §5.

TTL_SECONDS = 30 * 60   # the retention sentence frontend/src/App.tsx makes
TURNS_CAP = 10          # PRD §7 -- a free, local "no" once reached


@dataclass
class StyleSession:
    """One open conversation with SuperDocs about a held rebuild.

    `rebuild` is the cumulative B28 anchor and is never overwritten once the
    session opens. `versions` holds every accepted export, oldest first --
    `versions[-1]` is what a download returns. `authored` is the running
    B20'/B27 baseline: words the person supplied verbatim at the counter,
    net of anything they asked removed.
    """

    session_id: str
    rebuild: bytes
    filename: str = "recovered.docx"
    dir: str = ""
    versions: list[bytes] = field(default_factory=list)
    #: The current version rendered for reading, so V5's "with your changes"
    #: side of the toggle has something to show. Cached when a version is
    #: accepted rather than rebuilt per request -- the page asks for session
    #: state far more often than the document changes.
    preview: str = ""
    authored: Counter = field(default_factory=Counter)
    turns_used: int = 0
    last_used_at: float = field(default_factory=time.time)
    #: Each: {"asked", "note", "turn_index", "applied", "supplied"} -- the
    #: last kept only so a revert can unwind what that turn added to
    #: `authored`. No cost on a receipt: what a turn costs us is ours to
    #: know, and is not something the person can act on.
    receipts: list[dict] = field(default_factory=list)
    #: A review waiting on the person: the job that paused, the changes it
    #: proposed, and everything the turn had worked out before it stopped, so
    #: deciding does not have to recompute it. `None` when nothing is
    #: pending. Kept on the session rather than in the request so that
    #: reopening the counter finds the review instead of losing it -- the same
    #: promise the session already makes about receipts.
    pending: dict | None = None


_SESSIONS: dict[str, StyleSession] = {}
#: The most recent `/api/download/{token}` URL for a session's own exports,
#: kept apart from the dataclass because it is a view over `_READY`, not
#: state the session owns.
_LATEST_DOWNLOAD: dict[str, str] = {}

#: One lock per token, so opening two different documents at once does not
#: queue. Guarded by its own lock because the registry itself is shared.
_OPEN_LOCKS: dict[str, threading.Lock] = {}
_OPEN_LOCKS_GUARD = threading.Lock()


def _open_lock_for(token: str) -> threading.Lock:
    with _OPEN_LOCKS_GUARD:
        return _OPEN_LOCKS.setdefault(token, threading.Lock())


def _sweep() -> None:
    """Idle expiry, actually enforced -- PRD §5's "retention gap".

    `_READY` used to evict only by count and never by time, so the page's
    "held for about thirty minutes" promise was asserted, not kept. This drops
    both stale sessions and stale held files once they have sat past
    `TTL_SECONDS` since they were last used, and leaves the count-based cap in
    `_hold` standing as a second, independent bound.
    """
    now = time.time()
    for token, session in list(_SESSIONS.items()):
        if now - session.last_used_at > TTL_SECONDS:
            _dispose(token)
    for token, at in list(_READY_AT.items()):
        if now - at > TTL_SECONDS:
            _READY.pop(token, None)
            _READY_AT.pop(token, None)


def _dispose(token: str) -> bool:
    """Drop a session and let its directory go. `True` if there was one."""
    session = _SESSIONS.pop(token, None)
    if session is None:
        return False
    if session.dir:
        shutil.rmtree(session.dir, ignore_errors=True)
    _LATEST_DOWNLOAD.pop(token, None)
    return True


#: Anything that can run, fetch, or navigate. SuperDocs' HTML is not hostile,
#: but it is not ours either, and it reaches the page through
#: `dangerouslySetInnerHTML`. The recovery's own preview is safe because the
#: engine escapes every piece of text and emits no attribute it did not
#: construct; this markup carries neither guarantee, so it is scrubbed before
#: it is trusted with the same rendering path.
_STRIP_TAGS = re.compile(
    r"<\s*/?\s*(script|iframe|object|embed|link|meta|base|form|style)\b[^>]*>",
    re.I)
_STRIP_BLOCKS = re.compile(
    r"<\s*(script|style)\b[^>]*>.*?<\s*/\s*\1\s*>", re.I | re.S)
_STRIP_HANDLERS = re.compile(r"\son[a-z]+\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)", re.I)
_STRIP_JS_URLS = re.compile(
    r"\s(href|src|xlink:href)\s*=\s*(\"\s*javascript:[^\"]*\"|'\s*javascript:[^']*'"
    r"|javascript:[^\s>]+)", re.I)


def _safe_html(html: str) -> str:
    """SuperDocs' own rendering, with anything executable taken out.

    Kept deliberately blunt: drop the tags that can run or fetch, drop every
    `on*` handler, drop `javascript:` targets. Formatting -- which is the
    whole reason to show this rather than a rebuild of it -- is left alone.
    """
    if not html:
        return ""
    out = _STRIP_BLOCKS.sub("", html)
    out = _STRIP_TAGS.sub("", out)
    out = _STRIP_HANDLERS.sub("", out)
    out = _STRIP_JS_URLS.sub("", out)
    return out


def _preview_of(blob: bytes) -> str:
    """The changed document itself, rendered for reading.

    V5's toggle asks "as it came back" against "with your changes", and a
    toggle that cannot show the second is decoration. The verdict is a claim
    and the sheet is the thing itself, so after a turn the page has to be able
    to show the thing, not just offer a file to download and hope.

    Built on the same path the recovery uses -- salvage the members, collect
    the pictures, read the blocks, render with the images inline -- rather
    than a second renderer that could disagree with the first. Empty on any
    failure: an unreadable preview costs the toggle, never the turn.
    """
    from docrepair import docx as _docx, media as _media, salvage as _salvage
    from docrepair.engine import DOCUMENT_PART, DOC_RELS_PART

    try:
        s = _salvage.salvage_members(blob)
        targets = _media.relationship_targets(s.members.get(DOC_RELS_PART))
        pictures = _media.collect(s.members)
        blocks = _docx.read_blocks(s.members[DOCUMENT_PART], targets, pictures)
        return _docx.blocks_to_html(blocks, inline_images=True)
    except Exception:  # noqa: BLE001 -- a preview is never worth failing a turn
        return ""


def _write_version(session: StyleSession, blob: bytes) -> str:
    """Hold an accepted export inside the session's own directory (O3) and
    return the download URL for it."""
    path = Path(session.dir) / f"{uuid.uuid4().hex}.docx"
    path.write_bytes(blob)
    return f"/api/download/{_hold(str(path))}"


def _styling_key() -> str | None:
    key = os.environ.get("SUPERDOCS_API_KEY")
    return key.strip() or None if key else None


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return (Path(__file__).parent.parent / "static" / "index.html").read_text()


@app.get("/api/capabilities")
def capabilities() -> dict:
    """What this copy of the page can actually do, asked before anything is
    offered so the styling step is genuinely available or plainly explained."""
    on = _styling_key() is not None
    return {
        "styling": on,
        "note": (
            "Recovered documents are styled through SuperDocs before you get them."
            if on else
            "This copy of the page has no SuperDocs key, so documents come back "
            "as the plain rebuild — complete, but not styled."
        ),
    }


@app.post("/api/recover")
async def api_recover(file: UploadFile):
    """Upload a damaged doc; stream SSE progress then a manifest event."""
    data = await file.read()
    if not data:
        raise HTTPException(400, "That file is empty.")
    if len(data) > MAX_BYTES:
        raise HTTPException(413, "That file is larger than 20 MB.")

    q: queue.Queue = queue.Queue()
    result: dict = {}

    def work() -> None:
        try:
            from docrepair import engine

            def say(stage: str, message: str) -> None:
                q.put({"stage": stage, "message": message})

            # O3: a directory of this request's own, not the process cwd, so
            # concurrent recoveries never race on one `local_rebuild.docx`.
            out_dir = tempfile.mkdtemp(prefix="salvage-")
            res = engine.repair(
                data, file.filename or "document.docx", on_progress=say,
                out_dir=out_dir)
            token = _hold(res.output_path)
            payload = {
                "token": token,
                **res.as_payload(
                    download=f"/api/download/{token}",
                    # Kept as a retry: the docs warn a first request in a fresh
                    # session can be slow or fail while things warm up.
                    style=f"/api/style/{token}" if res.ok else None,
                ),
            }

            # The styling pass, in the same run and on the same progress stream.
            # A person watching this is watching one recovery, not a recovery
            # and then a decision they did not know they had to make.
            if res.ok:
                payload.update(_style(res, f"/api/download/{token}", say))

            result["payload"] = payload
        except Exception as exc:  # never leak a stack trace to a consumer
            failed = engine.Result()
            failed.lost = [f"this file could not be read: {type(exc).__name__}"]
            failed.output_path = ""
            result["payload"] = failed.as_payload()
        finally:
            q.put(None)

    threading.Thread(target=work, daemon=True).start()

    def stream():
        while True:
            item = q.get()
            if item is None:
                break
            yield f"data: {json.dumps(item)}\n\n"
        yield f"data: {json.dumps({'done': True, **result['payload']})}\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")


def _style(res, plain: str, say, client=None) -> dict:
    """Run the styling pass and say what it produced.

    Returns the part of the manifest that describes it. `download` is
    repointed at the styled file when there is one, because that is the file
    this product promises; `plain_download` always stays available, so
    preferring the unstyled copy never means running the recovery again.
    """
    from docrepair.superdocs_client import SuperDocsClient

    # Injectable so the styled path can be tested without a network or a key.
    # A test that can only reach the failure branch proves the failure branch.
    styling = (client or SuperDocsClient()).style(
        res.output, Path(res.output_path).name or "recovered.docx",
        on_progress=say,
    )

    if not styling.ok:
        return {
            "styled": False,
            "styling_note": styling.note,
            "styling_rejected": styling.rejected_for_content,
            "plain_download": plain,
        }

    path = Path(res.output_path).with_name("final_recovered.docx")
    path.write_bytes(styling.output)
    token = _hold(str(path))
    return {
        "styled": True,
        "styling_note": styling.note,
        "styling_rejected": False,
        "download": f"/api/download/{token}",
        "plain_download": plain,
        "ops_charged": styling.ops_charged,
    }


@app.post("/api/style/{token}")
def api_style(token: str):
    """Style a rebuild again, for a caller who wants to retry the pass.

    The recovery already runs this; the docs warn that a first request in a
    fresh session can be slow or fail while things warm up, so a retry that does
    not repeat the whole recovery is worth keeping. On any failure the local
    rebuild is unchanged and still theirs.
    """
    path = _READY.get(token)
    if path is None:
        raise HTTPException(404, NOT_HELD)

    from docrepair.superdocs_client import SuperDocsClient

    blob = Path(path).read_bytes()
    styling = SuperDocsClient().style(blob, Path(path).name)
    if not styling.ok:
        return {"styled": False, "path": path, "download": None,
                "note": styling.note}

    out = Path(path).with_name("final_recovered.docx")
    out.write_bytes(styling.output)
    return {
        "styled": True,
        "path": str(out),
        "download": f"/api/download/{_hold(str(out))}",
        "note": styling.note,
    }


@app.get("/api/download/{token}")
def download(token: str) -> Response:
    path = _READY.get(token)
    if path is None:
        raise HTTPException(404, NOT_HELD)
    try:
        blob = Path(path).read_bytes()
    except OSError:
        # The token is still remembered but the file behind it is gone --
        # e.g. a styling-counter session that was disposed. Same sentence:
        # from a consumer's chair this is exactly the same situation.
        raise HTTPException(404, NOT_HELD)
    return Response(
        blob,
        media_type=("application/vnd.openxmlformats-officedocument."
                     "wordprocessingml.document"),
        headers={"Content-Disposition": f'attachment; filename="{Path(path).name}"'},
    )


# -- the styling counter's endpoints ------------------------------------------
#
# Added under the existing `POST /api/style/{token}` retry route, which stays
# exactly as it is above. See docs/PRD-SET-IT-YOUR-WAY.md §12.

def _opened(token: str, session: StyleSession) -> dict:
    """What opening a counter answers with, whether it was just minted or was
    already standing."""
    return {
        "session": session.session_id,
        "turns_left": TURNS_CAP - session.turns_used,
        "turns_cap": TURNS_CAP,
        "retention_seconds": TTL_SECONDS,
        "receipts": list(session.receipts),
    }


def _open_session(token: str, client=None) -> dict:
    """Open a SuperDocs conversation on a held rebuild. B22: the allowance is
    read before anything else, and an unreadable balance is never zero.

    Opening is idempotent, and has to be. A counter that is already standing
    is *resumed*, never replaced: minting a second `StyleSession` for a token
    throws away the receipts, the accepted versions, the words the person
    supplied, and any change waiting to be decided -- and it uploads the same
    document a second time to do it.

    The lock is not belt-and-braces. Two opens for one token arrive together
    in the ordinary case -- React re-mounts the screen in development, a
    double click, a retry after a slow first attempt -- and both would find no
    session and both would mint one. Held across the network calls on purpose:
    the second request waits, then finds the first one's session and returns
    it, which is the answer it wanted anyway.
    """
    _sweep()
    path = _READY.get(token)
    if path is None:
        raise HTTPException(404, NOT_HELD)

    with _open_lock_for(token):
        standing = _SESSIONS.get(token)
        if standing is not None:
            standing.last_used_at = time.time()
            return _opened(token, standing)

        from docrepair.superdocs_client import SuperDocsClient

        opener = client or SuperDocsClient()
        filename = Path(path).name or "recovered.docx"
        rebuild = Path(path).read_bytes()

        try:
            allowance = opener.allowance()
            session_id = opener.open_session(rebuild, filename)
        except Exception:  # noqa: BLE001 -- never leak a stack trace to a consumer
            raise HTTPException(
                503,
                "The counter cannot be reached just now. Your document is unchanged.")

        session = _SESSIONS[token] = StyleSession(
            session_id=session_id, rebuild=rebuild, filename=filename,
            dir=tempfile.mkdtemp(prefix="style-"),
        )
        return _opened(token, session)


@app.post("/api/style/{token}/open")
def api_style_open(token: str):
    return _open_session(token)


def _safe_change(change: dict) -> dict:
    """One proposed change, ready to be read.

    `old_html` and `new_html` are SuperDocs' markup and go to the page as
    markup -- a diff that is not rendered is not a diff. That is third-party
    HTML reaching `dangerouslySetInnerHTML`, so it is scrubbed here on the
    same path the sheet already takes (`_safe_html`), rather than trusted
    because of where it came from. Only the fields the page reads are passed
    on; the job id stays ours.
    """
    return {
        "change_id": str(change.get("change_id") or ""),
        "operation": str(change.get("operation") or "edit"),
        "chunk_id": change.get("chunk_id"),
        "old_html": _safe_html(change.get("old_html") or "") or None,
        "new_html": _safe_html(change.get("new_html") or "") or None,
        "ai_explanation": str(change.get("ai_explanation") or ""),
    }


def _session_payload(token: str, session: StyleSession) -> dict:
    return {
        "turns_left": TURNS_CAP - session.turns_used,
        "turns_cap": TURNS_CAP,
        "receipts": list(session.receipts),
        "download": _LATEST_DOWNLOAD.get(token),
        # The thing itself, not a claim about it -- empty until a turn has
        # actually changed something, at which point the toggle has two sides.
        "preview_html": session.preview,
        # B2: always the rebuild, on every response, no matter how many turns
        # have run.
        "plain_download": f"/api/download/{token}",
        "authored_words": sum(session.authored.values()),
        # Only what the person needs to decide: what it would change, and why
        # SuperDocs says it wants to. Never the job id -- that is ours.
        "pending": None if not session.pending else {
            "asked": session.pending["asked"],
            "changes": [_safe_change(c) for c in session.pending["changes"]],
        },
    }


@app.get("/api/style/{token}/session")
def api_style_session(token: str):
    _sweep()
    session = _SESSIONS.get(token)
    if session is None:
        raise HTTPException(404, NOT_HELD)
    return _session_payload(token, session)


@app.delete("/api/style/{token}/session")
def api_style_dispose(token: str):
    _sweep()
    if not _dispose(token):
        raise HTTPException(404, NOT_HELD)
    return {"disposed": True}


def _land(token: str, session: StyleSession, client, turn, *, asked: str,
          supplied: str, provisional) -> dict:
    """Record a finished turn and hand back the page's manifest.

    Shared by the turn route -- for a turn that ended without anything to
    decide -- and by the decision route, since both end the same way once the
    turn is over.
    """
    applied = bool(turn.ok)
    if applied:
        session.versions.append(turn.output)
        _LATEST_DOWNLOAD[token] = _write_version(session, turn.output)
        # SuperDocs' own HTML first, because it is the only one that carries
        # formatting: a preview rebuilt from the exported file goes through
        # `blocks_to_html`, which keeps structure and text and drops every
        # font size, weight and margin. Ask for smaller headings and that
        # rebuild comes back identical, so the page shows no change for a
        # change that really happened. Fall back to the rebuild when the read
        # fails -- a structural preview beats none.
        session.preview = (_safe_html(client.document_html(session.session_id))
                           or _preview_of(turn.output))
        # The words the person supplied are theirs from here on, and are
        # counted as theirs in the handover (B27).
        session.authored = provisional

    session.receipts.append({
        "asked": asked,
        "note": turn.note,
        "turn_index": getattr(turn, "turn_index", None),
        "applied": applied,
        "supplied": supplied,
    })

    return {
        "applied": applied,
        "note": turn.note,
        **_session_payload(token, session),
    }


@app.post("/api/style/{token}/turn")
def api_style_turn(token: str, body: dict):
    """One instruction, streamed exactly like `/api/recover`: SSE stage lines
    on a worker thread, then a final line carrying the manifest -- B5."""
    message = str((body or {}).get("message", ""))

    _sweep()
    session = _SESSIONS.get(token)
    if session is None:
        raise HTTPException(404, NOT_HELD)

    q: queue.Queue = queue.Queue()
    result: dict = {}

    def work() -> None:
        try:
            from docrepair import counter
            from docrepair.superdocs_client import SuperDocsClient

            client = SuperDocsClient()

            def say(stage: str, msg: str) -> None:
                q.put({"stage": stage, "message": msg})

            turns_left = TURNS_CAP - session.turns_used
            # B22, read before anything billable is even considered.
            allowance = client.allowance()

            refusal = counter.why_refused_before_sending(
                message, turns_left=turns_left,
                allowance_known=allowance.known,
                allowance_remaining=allowance.remaining,
            )
            if refusal:
                # The "no" made before anything is sent. It still goes
                # in the ledger. A refusal that leaves no trace is a change
                # the person asked for and cannot afterwards see they asked
                # for -- and the receipts are where the page says which of
                # the two kinds of no this was, and what it cost.
                session.receipts.append({
                    "asked": message,
                    "note": refusal,
                    "turn_index": None,      # never sent, so nothing to revert
                    "applied": False,
                    "supplied": "",
                })
                result["payload"] = {
                    "applied": False,
                    "note": refusal,
                    **_session_payload(token, session),
                    "turns_left": turns_left,
                }
                return

            supplied = counter.supplied_text(message)
            # B27: only the person moves the baseline, and only by supplying
            # the exact text. Provisionally here, so the guard will accept
            # those words in the file that comes back -- but committed to the
            # session only if the turn actually lands (below). A baseline
            # moved by an edit that was refused would go on authorising words
            # the document never received, which is the leak B20' exists to
            # close rather than to open somewhere quieter.
            provisional = (counter.with_supplied(session.authored, added=supplied)
                           if supplied else session.authored)

            authorised = counter.baseline_from(session.rebuild) + provisional
            sent = session.versions[-1] if session.versions else session.rebuild

            turn = client.turn(session.session_id, message, sent=sent,
                               authorised=authorised, on_progress=say)

            session.last_used_at = time.time()

            if turn.proposed:
                # The job is paused holding the change. Nothing has been
                # applied and nothing is counted yet: a turn the person
                # discards costs them nothing, and the platform does not bill
                # a denied review either.
                session.pending = {
                    "job_id": turn.job_id,
                    "changes": turn.pending,
                    "asked": message,
                    "supplied": supplied,
                    "provisional": provisional,
                    "sent": sent,
                    "authorised": authorised,
                }
                result["payload"] = {
                    "applied": False,
                    "proposed": True,
                    "note": turn.note,
                    **_session_payload(token, session),
                }
                return

            # Nothing to decide -- the job finished, failed, or was never
            # answerable. Either way it is over, and it counts.
            session.turns_used += 1

            result["payload"] = _land(token, session, client, turn,
                                      asked=message, supplied=supplied,
                                      provisional=provisional)
        except Exception:  # noqa: BLE001 -- never leak a stack trace
            result["payload"] = {
                "applied": False,
                "note": "That change could not be completed just now. Your "
                        "document is unchanged.",
                **_session_payload(token, session),
            }
        finally:
            q.put(None)

    threading.Thread(target=work, daemon=True).start()

    def stream():
        while True:
            item = q.get()
            if item is None:
                break
            yield f"data: {json.dumps(item)}\n\n"
        yield f"data: {json.dumps({'done': True, **result['payload']})}\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")


@app.post("/api/style/{token}/approve")
def api_style_decide(token: str, body: dict):
    """Answer the review the counter is holding -- the fourth contract call.

    Streamed like the turn it finishes, on the same SSE shape, because
    approving is not instant: the platform resumes the job and applies the
    change, and only then is there a file to export.
    """
    approved = bool((body or {}).get("approved"))

    _sweep()
    session = _SESSIONS.get(token)
    if session is None:
        raise HTTPException(404, NOT_HELD)
    pending = session.pending
    if pending is None:
        raise HTTPException(409, "There is nothing waiting to be decided.")

    q: queue.Queue = queue.Queue()
    result: dict = {}

    def work() -> None:
        try:
            from docrepair.superdocs_client import SuperDocsClient

            client = SuperDocsClient()

            def say(stage: str, msg: str) -> None:
                q.put({"stage": stage, "message": msg})

            turn = client.decide(
                session.session_id, pending["job_id"], pending["changes"],
                approved, sent=pending["sent"],
                authorised=pending["authorised"], on_progress=say)

            # Decided either way, so the review is over and the job is no
            # longer holding the session.
            session.pending = None
            session.last_used_at = time.time()

            if approved:
                # Only an applied change is counted. Reading a proposal and
                # saying no costs the person nothing -- and costs us nothing
                # either, since a denied review-mode change is not billed.
                session.turns_used += 1

            result["payload"] = _land(
                token, session, client, turn, asked=pending["asked"],
                supplied=pending["supplied"] if approved else "",
                provisional=(pending["provisional"] if approved
                             else session.authored))
        except Exception:  # noqa: BLE001 -- never leak a stack trace
            # The review is cleared regardless. Leaving it on screen would
            # offer a decision that can no longer be made -- the job it
            # belongs to is answered, expired, or unreachable.
            session.pending = None
            result["payload"] = {
                "applied": False,
                "note": "That decision could not be completed just now. Your "
                        "document is unchanged.",
                **_session_payload(token, session),
            }
        finally:
            q.put(None)

    threading.Thread(target=work, daemon=True).start()

    def stream():
        while True:
            item = q.get()
            if item is None:
                break
            yield f"data: {json.dumps(item)}\n\n"
        yield f"data: {json.dumps({'done': True, **result['payload']})}\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")


@app.post("/api/style/{token}/revert")
def api_style_revert(token: str):
    """Undo the newest applied change -- free, and never counted against the
    turn cap. Returns `compose_text` so the field can be refilled."""
    _sweep()
    session = _SESSIONS.get(token)
    if session is None:
        raise HTTPException(404, NOT_HELD)

    if session.pending is not None:
        # SuperDocs refuses a revert while a review is open (409), and it is
        # right to: the newest change is not decided yet, so there is no
        # settled state to go back to. Said rather than sent and failed.
        return {
            "ok": False,
            "note": "There is a change waiting for you. Decide on that one "
                    "first, then you can put an earlier change back.",
            "compose_text": "",
            **_session_payload(token, session),
        }

    applied_receipts = [r for r in session.receipts if r.get("applied")]
    if not applied_receipts or not session.versions:
        return {
            "ok": False,
            "note": "There is nothing yet to put back.",
            "compose_text": "",
            **_session_payload(token, session),
        }

    from docrepair.superdocs_client import SuperDocsClient

    client = SuperDocsClient()
    last = applied_receipts[-1]

    try:
        reverted = client.revert(session.session_id, last.get("turn_index"))
    except Exception:  # noqa: BLE001 -- never leak a stack trace
        return {
            "ok": False,
            "note": "The counter cannot be reached just now. Your document is unchanged.",
            "compose_text": "",
            **_session_payload(token, session),
        }

    if not reverted.ok:
        return {
            "ok": False,
            "note": reverted.note,
            "compose_text": "",
            **_session_payload(token, session),
        }

    session.versions.pop()
    # The sheet follows the file back. A toggle still showing the change that
    # was just put back would be the page disagreeing with itself about what
    # the document now says.
    session.preview = (_preview_of(session.versions[-1])
                       if session.versions else "")
    # By identity, not `==` -- two receipts can read alike (same message,
    # same note) and `list.remove` would happily drop the wrong one.
    for i in range(len(session.receipts) - 1, -1, -1):
        if session.receipts[i] is last:
            del session.receipts[i]
            break
    session.last_used_at = time.time()

    if last.get("supplied"):
        from docrepair import counter
        # B28: recomputed from the rebuild plus authored, minus anything the
        # reverted turn added -- not left to drift ahead of what actually
        # shipped.
        session.authored = counter.with_supplied(
            session.authored, removed=last["supplied"])

    _LATEST_DOWNLOAD[token] = (
        _write_version(session, session.versions[-1]) if session.versions else None)

    return {
        "ok": True,
        "note": reverted.note,
        "compose_text": reverted.compose_text,
        **_session_payload(token, session),
    }


def main() -> None:
    import uvicorn

    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(app, host="127.0.0.1", port=port)


if __name__ == "__main__":
    main()
