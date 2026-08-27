"""The styling pass: the path that produces the file the product promises.

The brief for this build is explicit about what a strong result looks like — a
reviewer runs a broken DOCX through the tool and "gets a valid, **styled** file
back with a clear summary of what was recovered". So this is not a ceiling on
top of the local rebuild; it is the main road. The local rebuild is what gets
sent, and what the person still gets if this road is closed.

The call sequence, against the documented endpoints:

  1. POST /v1/documents/upload   multipart file + session_id
                                  -> loads the rebuilt .docx as the session's
                                     active editable document. Parsing is not
                                     billable; images are extracted to cloud
                                     storage and referenced by URL, which is why
                                     the pictures survive without a separate
                                     image call.
  2. POST /v1/chat               {message, session_id}
                                  -> synchronous: the AI applies the edit inline
                                     and the change is live on the session when
                                     the call returns.
  3. POST /v1/documents/export   {session_id, format:"docx"}
                                  -> round-trips through the original docx
                                     renderer, preserving tables, borders,
                                     shading, headers, footers, fonts, inline
                                     styling and embedded images.

On the approval step
--------------------
The brief names a four-call minimum contract: upload, chat, approve, export.
All four are made -- but on the road where a person is actually deciding.

There are two roads through this module and they are not the same shape:

  * `style()`, the automatic pass inside `/api/recover`. A machine-authored,
    formatting-only instruction on a document the person dropped a moment ago.
    They have no basis on which to judge it, and the docs' own recommendation
    is to default to auto-apply and make review the opt-in
    (guides/human-in-the-loop, "Recommended UX pattern"). This road stays
    synchronous and keeps `_why_not_acceptable` -- a styled file that comes
    back with fewer pictures or different words is refused and the plain
    rebuild ships with the reason said out loud.

  * `turn()` / `decide()`, the counter. The person wrote the instruction, so
    they are the only one who can say whether the result is what they meant.
    `approval_mode='ask_every_time'` on `/v1/chat/async` makes the job pause at
    `awaiting_approval` carrying its proposed changes; `decide()` answers with
    `POST /v1/chat/{session_id}/approve`. Nothing is applied until they say so,
    and denying costs nothing -- the platform does not bill a denied
    review-mode change.

This reverses the decision recorded here until 2026-08-27, which was that
adding approve "would mean moving to the async flow purely to have something to
approve". The counter was already on `/v1/chat/async`; it simply sent no
`approval_mode` and cancelled the job if the pause ever appeared. The cost that
argument treated as prohibitive had already been paid. See BUG-097.

Prompting for styling
---------------------
The instruction is bounded on purpose. This document was reconstructed from
damage and its text is the only record of what its owner wrote, so the edit is
allowed to change how it looks and nothing else.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

import requests

BASE = "https://api.superdocs.app"

log = logging.getLogger("superdocs")

#: The styling prompt, sent as `message` (the field the live SuperDocs chat
#: endpoint accepts). Carried verbatim from the spec.
INSTRUCTION = (
    "Formatting only. This document was recovered from a damaged file and its "
    "text is the only record of what its owner wrote. Normalize the formatting, "
    "restore heading styles, and keep every table and every image exactly as "
    "they are. Do NOT add, remove, expand, summarise, complete or reword any "
    "text. Do not add sections, headings, rows, totals, placeholders, "
    "disclaimers, signature blocks, headers, footers or dates. Do not fill "
    "gaps. If a passage looks incomplete, leave it exactly as it is. The "
    "word-for-word text of the output must be identical to the input. "
    "Target max 25 sections."
)

REQUEST_TIMEOUT = 300.0

#: The contract a conversational turn is wrapped in.
#:
#: This used to say "formatting only" and forbid adding, removing, expanding,
#: summarising, completing or rewording anything -- the same bounded contract
#: INSTRUCTION carries for the automatic pass. B20'' retired the refusal that
#: matched it (owner, 2026-08-26), and an envelope still arguing against what
#: the person just typed is worse than none: it makes the model hedge or
#: half-comply on an instruction the owner decided is theirs to give.
#:
#: What it bounds now is scope, not permission. Do what was asked; do not do
#: things that were not asked. That is the discipline worth keeping, because
#: a model handed a sparse recovered document will otherwise helpfully fill
#: it out -- observed on 2026-08-20, when a four-line report came back with
#: invented paragraphs, a subtotal row, a disclaimer and a signature block
#: that nobody had asked for.
TURN_ENVELOPE = (
    "This document was recovered from a damaged file, so its text is the "
    "only surviving record of what its owner wrote. Treat it as precious: "
    "never drop or replace content the request below does not ask you to "
    "touch. Do exactly what the request asks, and nothing beyond it -- add "
    "no sections, headings, rows, totals, placeholders, disclaimers, "
    "signature blocks, headers, footers or dates that were not asked for. "
    "If the request supplies exact words in quotes, use those words exactly "
    "as given.\n\nThe person's request: {message}"
)

#: Async job polling: a small backoff, capped, against a generous overall
#: budget -- async exists precisely so a turn that runs long survives past
#: the ~300s the synchronous endpoint 504s at (PRD §6), so the budget here is
#: wider than REQUEST_TIMEOUT rather than equal to it.
JOB_POLL_INITIAL = 2.0
JOB_POLL_MAX = 15.0
JOB_POLL_BUDGET = 900.0


def _bounded_turn(message: str) -> str:
    """Wrap a person's instruction in the same bounded contract INSTRUCTION
    carries, so it goes out evaluated inside a boundary rather than as free
    text the model could read as licence to write prose."""
    return TURN_ENVELOPE.format(message=message)


@dataclass
class Allowance:
    """The operations balance, and whether anybody actually read it."""

    known: bool = False
    remaining: int = 0
    tier: str = ""


@dataclass
class Styling:
    """What the styling pass produced, and what to tell the person if nothing."""

    ok: bool = False
    output: bytes = b""
    #: One sentence, consumer-facing. Set on every path, success or not.
    note: str = ""
    stages: list = field(default_factory=list)
    #: Set when a styled file came back and was thrown away because its text or
    #: its pictures no longer matched. Kept apart from a transport failure: one
    #: is nobody's fault, the other is a claim we refused to pass on.
    rejected_for_content: bool = False
    ops_charged: int = 0
    allowance_known: bool = False
    allowance_remaining: int = 0
    #: The SuperDocs session this pass ran on. Minted the moment it exists,
    #: so a conversation can continue on the same document -- even on a
    #: later failure path, since the session itself may still be usable.
    session_id: str = ""


@dataclass
class Turn:
    """What one conversational instruction produced, and what to tell the
    person if nothing. Same discipline as `Styling`: every path, success or
    not, leaves `note` set to one consumer-facing sentence."""

    ok: bool = False
    output: bytes = b""              # the exported .docx after this turn
    note: str = ""                   # one consumer-facing sentence, set on EVERY path (B21)
    rejected_for_content: bool = False
    turn_index: int | None = None    # of the user message, for a later revert
    reconciled: bool = False         # True when recovered from a timeout rather than a clean reply
    stages: list = field(default_factory=list)
    #: Set when the job paused for review instead of applying. `pending` holds
    #: the proposed changes exactly as SuperDocs described them -- `old_html`,
    #: `new_html` and `ai_explanation` per entry -- and `job_id` is what
    #: `decide()` needs to answer. `ok` stays False: nothing has landed.
    proposed: bool = False
    pending: list = field(default_factory=list)
    job_id: str = ""
    # No operation count and no allowance here. The counter does not report
    # what it costs us: somebody whose file broke this morning did not arrive
    # with an account, and a number describing our metering is not something
    # they can act on. The allowance is still read before anything is sent
    # (B22) -- it decides whether to send, and says nothing further.


def pending_changes(job_body: dict) -> list[dict]:
    """Read the proposed changes off a paused job, whatever shape they arrive in.

    Two shapes exist and both are real, which is trap 1 on the brief's own
    list:

      * `GET /v1/jobs/{id}` returns `metadata.pending_changes` as a plain LIST
        of change dicts.
      * The same batch delivered as a `proposed_change_batch` event carries an
        envelope whose `content` is a JSON-encoded STRING needing a second
        parse. The docs name missing that second parse as the single most
        common reason integrators see empty diff cards -- the fields are all
        there and every one of them reads as undefined.

    Never raises: an unreadable batch is no batch, and the caller treats that
    the same as a job that proposed nothing.
    """
    meta = (job_body or {}).get("metadata") or {}
    pending = meta.get("pending_changes")
    if isinstance(pending, list):
        return list(pending)
    if pending is None:
        for event in meta.get("intermediate_responses") or []:
            if isinstance(event, dict) and event.get("type") == "proposed_change_batch":
                return parse_proposed_changes(event)
        return []
    if isinstance(pending, str):
        return parse_proposed_changes({"content": pending})
    return parse_proposed_changes(pending)


def parse_proposed_changes(envelope: dict) -> list[dict]:
    """The second parse. A one-change turn still arrives as a one-element
    `changes[]`, so this always returns a list and never special-cases the
    singular form."""
    import json as _json

    content = (envelope or {}).get("content")
    if isinstance(content, str):
        try:
            content = _json.loads(content)
        except (ValueError, TypeError):
            return []
    if not isinstance(content, dict):
        return []
    changes = content.get("changes")
    if isinstance(changes, list):
        return [c for c in changes if isinstance(c, dict)]
    return [content] if content.get("change_id") else []


@dataclass
class Reverted:
    """What a native revert produced."""

    ok: bool = False
    note: str = ""
    compose_text: str = ""           # the instruction that was undone, to refill the field
    reverted_to_turn: int = -1
    archived_turn_count: int = 0


class SuperDocsClient:
    """A thin wrapper that performs upload -> instruct -> export."""

    def __init__(self, api_key: str | None = None) -> None:
        self.api_key = api_key or os.environ.get("SUPERDOCS_API_KEY")

    def style(self, sent: bytes, filename: str = "recovered.docx",
              on_progress=None) -> "Styling":
        """Upload, instruct, export. Never raises to its caller.

        Takes the rebuilt bytes rather than a path: the engine already holds
        them, and re-reading a file it just wrote is a second chance to read
        something else.

        Every failure returns `ok=False` with a sentence a non-engineer can act
        on. The caller still has the local rebuild, so a failure here degrades
        to "you get the plain file, and here is why" rather than to "you get
        nothing".
        """
        import uuid

        r = Styling()

        def say(msg: str) -> None:
            r.stages.append(msg)
            if on_progress:
                on_progress("Styling", msg)

        if not self.api_key:
            r.note = (
                "This copy of the page has no SuperDocs key set, so the file "
                "below is the plain rebuild. It is complete and it is yours; "
                "it just has not been through the styling pass."
            )
            log.info("SUPERDOCS_API_KEY is not set; keeping the local rebuild.")
            return r

        try:
            # 0 -- the allowance, before a single billable call. Documented as
            # "useful before doing work (to confirm you have operations left)",
            # and reads are free. Starting a pass that cannot finish would leave
            # somebody watching a progress line for work refused at the far end.
            left = self.allowance()
            r.allowance_known, r.allowance_remaining = left.known, left.remaining
            if left.known and left.remaining < 1:
                r.note = (
                    "The SuperDocs styling allowance for this month is used up, "
                    "so nothing was sent and nothing was spent. The file below "
                    "is the plain rebuild and it is still yours."
                )
                say(r.note)
                return r

            session_id = f"salvage-{uuid.uuid4().hex}"
            r.session_id = session_id

            say("Sending the recovered document to SuperDocs…")
            self._upload_bytes(sent, filename, session_id)

            # The docs warn that a first request in a fresh session can take
            # from thirty seconds to several minutes with no visible progress on
            # a large document. That is still processing, not a crash, and the
            # line above is what the person watching it needs to see.
            say("Restoring heading styles, tables and spacing…")
            self._instruct(session_id)

            say("Exporting the styled file…")
            got = self.export(session_id)
            if not got:
                r.note = ("SuperDocs returned no file, so the plain rebuild "
                          "below is what you get. Nothing was lost.")
                say(r.note)
                return r

            refusal = _why_not_acceptable(sent, got)
            if refusal:
                # Checked rather than trusted, because the styled file is the
                # one offered as better and so is the one that must not quietly
                # be worse. A recovered document handed back with the pictures
                # missing, or with sentences its owner never wrote, is a
                # downgrade wearing better formatting.
                r.rejected_for_content = True
                r.note = (
                    "The styled version came back " + refusal + ", so it was "
                    "thrown away rather than handed over. Your document should "
                    "say what you wrote. The plain rebuild below is unchanged "
                    "and still yours."
                )
                say(r.note)
                log.warning("SuperDocs styling rejected (%s)", refusal)
                return r

            r.output = got
            r.ok = True
            r.ops_charged = 1        # one document-modifying chat turn
            r.note = "SuperDocs returned a styled file."
            say("Styled file ready.")
            return r

        except requests.Timeout:
            r.note = ("SuperDocs did not answer in time. The plain rebuild "
                      "below is unchanged and still yours — you can try the "
                      "styling again on a fresh run.")
        except Exception as exc:  # noqa: BLE001 -- never escape the styling pass
            # Deliberately no exception class name: this string reaches a person.
            log.warning("SuperDocs styling failed (%s)", exc)
            r.note = ("The styling pass did not work this time, so the file "
                      "below is the plain rebuild. It is complete and it is "
                      "yours.")
        say(r.note)
        return r

    def styled_export(self, filepath: str) -> str:
        """Path-in, path-out wrapper kept for callers that hold a file.

        Returns the styled file's path on success, or `filepath` unchanged on
        any failure, exactly as before.
        """
        path = Path(filepath)
        r = self.style(path.read_bytes(), path.name)
        if not r.ok:
            return str(path)
        out = path.with_name("final_recovered.docx")
        out.write_bytes(r.output)
        return str(out)

    def allowance(self) -> "Allowance":
        """What the platform says is left, before anything is spent.

        `known` is false when the balance could not be read. That is not the
        same as zero and is never reported as one: an unreadable balance lets
        the work proceed and says the number is unknown, because refusing on a
        number nobody read would be its own kind of bluff.
        """
        try:
            resp = requests.get(f"{BASE}/v1/agents/whoami",
                                headers=self._headers(), timeout=30)
            resp.raise_for_status()
            quota = (resp.json() or {}).get("quota") or {}
            if "remaining" not in quota:
                return Allowance()
            return Allowance(known=True, remaining=int(quota["remaining"]),
                             tier=str(quota.get("tier", "")))
        except Exception:  # noqa: BLE001 -- an unread balance is not a zero one
            return Allowance()

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"}

    def _upload_bytes(self, blob: bytes, filename: str, session_id: str) -> None:
        """Load the rebuilt document as the session's active editable document.

        Sent as a file, which is the documented contract: SuperDocs takes
        documents and HTML, and there is no endpoint for raw Word XML. Uploading
        the `.docx` is also what carries the pictures — the upload path extracts
        images to cloud storage and the export preserves them, so they survive
        without a separate image call.
        """
        import io

        resp = requests.post(
            f"{BASE}/v1/documents/upload",
            headers=self._headers(),
            files={"file": (filename, io.BytesIO(blob))},
            data={"session_id": session_id},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()

    def _upload(self, filepath: str, session_id: str) -> None:
        # Loads the local rebuild as the session's active, editable document.
        # Synchronous: the response returns the parsed HTML and the session_id.
        path = Path(filepath)
        with path.open("rb") as fh:
            resp = requests.post(
                f"{BASE}/v1/documents/upload",
                headers=self._headers(),
                files={"file": (path.name, fh)},
                data={"session_id": session_id},
                timeout=REQUEST_TIMEOUT,
            )
        resp.raise_for_status()

    def _instruct(self, session_id: str) -> None:
        # Synchronous chat: the AI normalizes/restyles the session's document and
        # applies the change immediately (auto-approve). No approval step required.
        requests.post(
            f"{BASE}/v1/chat",
            headers=self._headers(),
            json={"message": INSTRUCTION, "session_id": session_id},
            timeout=REQUEST_TIMEOUT,
        ).raise_for_status()

    def export(self, session_id: str) -> bytes | None:
        """The session's current document, as `.docx` bytes. Free (PRD §7)."""
        resp = requests.post(
            f"{BASE}/v1/documents/export",
            headers=self._headers(),
            json={"session_id": session_id, "format": "docx"},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.content or None

    def _export(self, session_id: str) -> bytes | None:
        # Kept as a thin alias: `export` is the public name now, but this
        # name stayed in case anything outside this file still reaches for
        # it directly.
        return self.export(session_id)

    # -- the conversation ------------------------------------------------

    def open_session(self, rebuild: bytes, filename: str = "recovered.docx") -> str:
        """Mint a session and load the rebuild into it as the active
        document, so a conversation can continue turn after turn on the
        same file.

        Never raises: a caller only ever gets back an id it can hold a
        conversation on. If the upload itself does not take, the first turn
        sent on this session will not find its edit landing, and
        reconciliation there is what surfaces it -- retrying the upload
        blindly here would carry the same double-billing risk `turn` is
        built to avoid, except spent on an upload rather than an edit.
        """
        import uuid

        session_id = f"salvage-{uuid.uuid4().hex}"
        try:
            self._upload_bytes(rebuild, filename, session_id)
        except Exception as exc:  # noqa: BLE001 -- never raise on session open
            log.warning("SuperDocs session open failed to upload (%s)", exc)
        return session_id

    def turn(self, session_id: str, message: str, *, sent: bytes,
             authorised, on_progress=None) -> "Turn":
        """One conversational instruction, applied and verified.

        Goes over the asynchronous chat endpoint (PRD §6): synchronous
        `/v1/chat` 504s past about 300 seconds, and there is no idempotency
        key on a billable write, so a turn that cannot be confirmed is
        reconciled against the session's own document version rather than
        ever resent.

        `approval_mode='ask_every_time'`, so this proposes and does not
        apply. A clean run ends at `awaiting_approval` with `r.proposed` set
        and the changes on `r.pending`; the person decides, and `decide()`
        finishes it. `r.ok` is False here on every path -- nothing has landed
        yet, and the counter must not say it has.

        `sent` is the version this turn started from (for the picture
        count) and `authorised` is the authorised word baseline (PRD §3) the
        guard checks the result against. Never raises -- every path returns
        a populated `Turn` with a note a non-engineer can act on.
        """
        r = Turn()

        def say(msg: str) -> None:
            r.stages.append(msg)
            if on_progress:
                on_progress("Styling", msg)

        # The free "did my edit land?" signal for reconciliation is the
        # session's document version id (`_version_id`, via the free
        # history read) -- it moves exactly when an edit lands and needs no
        # id resolution. `structure` is the documented "did my edit land"
        # read too, but it needs a durable document id resolved first
        # (`_durable_document_id`), so it stays a correct public helper
        # rather than the thing on this hot path.
        before_version = self._version_id(session_id)

        try:
            say("Sending your change to SuperDocs…")
            resp = requests.post(
                f"{BASE}/v1/chat/async",
                headers=self._headers(),
                json={"session_id": session_id, "message": _bounded_turn(message),
                      "response_mode": "compact",
                      # The person wrote this instruction, so the person
                      # decides whether the result is what they meant.
                      "approval_mode": "ask_every_time"},
                timeout=30,
            )
            resp.raise_for_status()
            job_id = (resp.json() or {}).get("job_id")
            if not job_id:
                raise ValueError("no job id in the response")

            say("Reading your document…")
            body = self._await_job(job_id, say)
            status = (body or {}).get("status")

            if status == "awaiting_approval":
                r.job_id = job_id
                r.pending = pending_changes(body)
                if not r.pending:
                    # Paused for a review with nothing to review. Nothing has
                    # been applied, so there is nothing to undo -- but the job
                    # would block the session until it is answered.
                    self._deny_quietly(session_id, job_id)
                    r.note = ("SuperDocs did not propose any change for that, "
                              "so your document is unchanged. Try saying it "
                              "another way.")
                    say(r.note)
                    return r
                r.proposed = True
                count = len(r.pending)
                noun = "change" if count == 1 else "changes"
                r.note = (f"SuperDocs proposes {count} {noun}. Nothing has "
                          f"changed yet — read it and decide.")
                say(r.note)
                return r

            if status == "completed":
                result = (body or {}).get("result") or {}
                turn_index = ((body or {}).get("metadata") or {}).get(
                    "user_turn_index_pre_inserted")
                self._finish_turn(r, session_id, sent, authorised, say,
                                  turn_index=turn_index,
                                  document_changes=result.get("document_changes"))
                return r

            # A definitive no from the platform -- failed or cancelled -- is
            # not ambiguous, so there is nothing to reconcile: the edit did
            # not land.
            r.note = ("SuperDocs could not apply that change, so your "
                      "document is unchanged. You can try again.")
            say(r.note)
            return r

        except Exception as exc:  # noqa: BLE001 -- never raise out of a turn
            # Timeout, 5xx, an unreadable job: never resend. Reconcile
            # against the session's own document version instead.
            log.warning("SuperDocs turn could not be confirmed (%s)", exc)
            self._reconcile_turn(r, session_id, before_version, sent,
                                 authorised, say)
            return r

    def decide(self, session_id: str, job_id: str, changes: list, approved: bool,
               *, sent: bytes, authorised, on_progress=None) -> "Turn":
        """Answer a review, and finish the turn if it was approved.

        `POST /v1/chat/{session_id}/approve` carries the decision. Top-level
        `approved` is required by the schema even on a batch -- omitting it is
        a bare 422, and the docs name it as a common trap -- so it is always
        sent, and every change carries its own copy.

        Approval is asynchronous: the call returns, then the job resumes and
        applies. Exporting before it settles exports the document without the
        change in it, so this polls to `completed` first.

        Never raises. Like `turn()`, every path leaves `note` set.
        """
        r = Turn()
        r.job_id = job_id

        def say(msg: str) -> None:
            r.stages.append(msg)
            if on_progress:
                on_progress("Styling", msg)

        before_version = self._version_id(session_id)

        try:
            say("Applying your change…" if approved else "Discarding that change…")
            self._answer_review(session_id, job_id, changes, approved)

            if not approved:
                # Denied changes are not billed, and there is nothing to
                # export: the document is exactly what it was.
                self._settle(session_id, job_id, say)
                r.note = ("Nothing was changed. That costs you nothing and "
                          "does not use one of your changes.")
                say(r.note)
                return r

            body = self._settle(session_id, job_id, say)
            status = (body or {}).get("status")

            if status == "completed":
                result = (body or {}).get("result") or {}
                turn_index = ((body or {}).get("metadata") or {}).get(
                    "user_turn_index_pre_inserted")
                self._finish_turn(r, session_id, sent, authorised, say,
                                  turn_index=turn_index,
                                  document_changes=result.get("document_changes"))
                return r

            r.note = ("SuperDocs could not apply that change, so your "
                      "document is unchanged. You can try again.")
            say(r.note)
            return r

        except Exception as exc:  # noqa: BLE001 -- never raise out of a decision
            log.warning("SuperDocs decision could not be confirmed (%s)", exc)
            if not approved:
                # A deny that could not be confirmed changed nothing either
                # way -- the only risk is a job still holding the session,
                # and that expires on its own.
                r.note = ("Nothing was changed. SuperDocs did not confirm "
                          "that, so give it a moment before sending another "
                          "change.")
                say(r.note)
                return r
            self._reconcile_turn(r, session_id, before_version, sent,
                                 authorised, say)
            return r

    def _answer_review(self, session_id: str, job_id: str, changes: list,
                       approved: bool) -> None:
        """The approve call itself. `changes` may be empty -- the top-level
        decision then stands for the whole batch."""
        payload: dict = {"job_id": job_id, "approved": bool(approved)}
        ids = [c.get("change_id") for c in (changes or [])
               if isinstance(c, dict) and c.get("change_id")]
        if ids:
            payload["changes"] = [{"change_id": cid, "approved": bool(approved)}
                                  for cid in ids]
        resp = requests.post(
            f"{BASE}/v1/chat/{session_id}/approve",
            headers=self._headers(), json=payload, timeout=30,
        )
        resp.raise_for_status()

    def _settle(self, session_id: str, job_id: str, say) -> dict | None:
        """Poll a decided job to a terminal state.

        A denial with no feedback should end the job, but the platform is
        allowed to come back with a revised proposal instead. Nothing is
        waiting to answer a second one, so it is denied too -- bounded, so an
        endlessly re-proposing job stops rather than spinning.
        """
        body = self._await_job(job_id, say)
        rounds = 0
        while (body or {}).get("status") == "awaiting_approval" and rounds < 2:
            rounds += 1
            self._answer_review(session_id, job_id,
                                pending_changes(body), False)
            body = self._await_job(job_id, say)
        return body

    def _deny_quietly(self, session_id: str, job_id: str) -> None:
        """Clear a review nobody can answer -- a pause that proposed nothing.

        Cancelling is what the docs point at for releasing a session held by a
        pending approval; already-applied work is kept and pending changes are
        discarded. Best effort: a job that cannot be cancelled expires on its
        own within the hour.
        """
        try:
            requests.post(f"{BASE}/v1/jobs/{job_id}/cancel",
                          headers=self._headers(), timeout=30)
        except Exception:  # noqa: BLE001
            pass

    def _await_job(self, job_id: str, say) -> dict | None:
        """Poll a chat job to a terminal state, with a small backoff. Emits a
        progress line while it runs (B5: stages are real, not simulated).
        Raises on a poll failure or a budget overrun -- caught by `turn`,
        which reconciles rather than resending."""
        import time

        delay = JOB_POLL_INITIAL
        deadline = time.monotonic() + JOB_POLL_BUDGET
        while True:
            resp = requests.get(f"{BASE}/v1/jobs/{job_id}",
                                headers=self._headers(), timeout=30)
            resp.raise_for_status()
            body = resp.json() or {}
            status = body.get("status")
            if status in ("completed", "failed", "cancelled"):
                return body
            if status == "awaiting_approval":
                # Two different pauses share this status, and answering the
                # wrong one is a 409. Branch on `awaiting_kind` first.
                kind = (body.get("metadata") or {}).get("awaiting_kind")
                if kind == "continue_prompt":
                    # A large edit applied what it could and is asking whether
                    # to keep going. It carries no proposed changes, and is
                    # resumed with `/v1/chat/{sid}/continue`, not `/approve`.
                    # This build has no continue flow, so the job is asked to
                    # stop rather than left running unattended -- and what it
                    # already applied is kept, which the reconcile path finds.
                    try:
                        requests.post(f"{BASE}/v1/jobs/{job_id}/cancel",
                                      headers=self._headers(), timeout=30)
                    except Exception:  # noqa: BLE001
                        pass
                    raise TimeoutError("job paused to ask about continuing")
                # A change review. This is the pause the counter asked for.
                return body
            if time.monotonic() >= deadline:
                raise TimeoutError("job poll exceeded its budget")
            progress = body.get("progress")
            say("Still applying your change…" +
                (f" {progress}%" if isinstance(progress, int) else ""))
            time.sleep(delay)
            delay = min(delay * 2, JOB_POLL_MAX)

    def _reconcile_turn(self, r: "Turn", session_id: str, before_version,
                        sent: bytes, authorised, say) -> None:
        """PRD §6: never blind-retry. Compare the session's document version
        id -- free, via `history` -- and only treat the edit as landed when
        it demonstrably moved. An unreadable id, before or after, is not
        evidence either way, so it is never read as "unchanged"."""
        after_version = self._version_id(session_id)
        if before_version is None or after_version is None:
            r.note = ("That change did not come back in time, and "
                      "SuperDocs cannot be reached to check whether it "
                      "landed. Nothing has been sent again; check back "
                      "before trying once more.")
            say(r.note)
            return
        if after_version == before_version:
            r.note = ("That change did not come back in time, and "
                      "checking your document shows nothing changed. You "
                      "can send it again.")
            say(r.note)
            return
        r.reconciled = True
        self._finish_turn(r, session_id, sent, authorised, say,
                          reconciled=True)

    def _finish_turn(self, r: "Turn", session_id: str, sent: bytes, authorised,
                     say, *, reconciled: bool = False,
                     turn_index: int | None = None,
                     document_changes=None) -> None:
        """Export, guard, and populate a landed edit's `Turn` -- shared by
        the clean-completion path and the reconcile path, since both end the
        same way once the edit is known to have landed."""
        from . import counter as _counter

        say("Exporting the changed file…")
        got = self.export(session_id)
        if not got:
            r.note = ("SuperDocs changed your document but did not send the "
                      "file back, so what you can download here is unchanged.")
            say(r.note)
            return

        # Nothing is refused here for changing the wording. At the counter
        # the person asked for it, and the owner's decision on 2026-08-26 is
        # that what they ask for is theirs to ask (B20''). The automatic pass
        # is unchanged and still refuses -- that is where the model acts with
        # nobody watching.
        #
        # It is still said. A turn that quietly removed seventy-three words
        # would be the silence this build has never allowed, whatever the
        # refusal rules are.
        changed = _counter.describe_change(sent, got, authorised)

        r.ok = True
        r.output = got
        # The job hands back the user message's own turn index; only the
        # reconcile path -- which never sees a job body -- falls back to
        # reading it from history.
        r.turn_index = (int(turn_index) if turn_index is not None
                        else self._last_turn_index(session_id))
        r.note = self._turn_receipt(document_changes, changed,
                                    reconciled=reconciled)
        say(r.note)

    def _turn_receipt(self, document_changes, changed: str, *,
                      reconciled: bool) -> str:
        """One consumer-facing sentence for a landed edit.

        Uses the compact per-section diff when SuperDocs sent one, so the
        receipt names that something specific changed rather than only that
        the turn landed -- and adds what it did to the words when that is
        not nothing. A person who asks for a formatting change and gets
        seventy-three words fewer should be told in the same breath as being
        told it worked.
        """
        prefix = ("That change took longer than expected, but it landed."
                 if reconciled else "SuperDocs applied your change.")
        if isinstance(document_changes, list) and document_changes:
            count = len(document_changes)
            noun = "section" if count == 1 else "sections"
            prefix = f"{prefix} {count} {noun} changed."
        if changed:
            prefix = f"{prefix} Also {changed}."
        return prefix

    def _history_body(self, session_id: str) -> dict | None:
        """Raw read of `GET /v1/sessions/{id}/history` --
        `{document_state, editor_action, messages, restore_error,
        session_id}`. Free. `None` when it could not be read."""
        try:
            resp = requests.get(
                f"{BASE}/v1/sessions/{session_id}/history",
                headers=self._headers(),
                timeout=30,
            )
            resp.raise_for_status()
            return resp.json() or {}
        except Exception as exc:  # noqa: BLE001
            log.warning("SuperDocs history read failed (%s)", exc)
            return None

    def _version_id(self, session_id: str) -> str | None:
        """The session's current document version id
        (`document_state.version_id`), which moves exactly when an edit
        lands. `None` when the history read failed -- not the same as
        "unchanged", and callers must not treat it as one."""
        body = self._history_body(session_id)
        if body is None:
            return None
        return (body.get("document_state") or {}).get("version_id")

    def document_html(self, session_id: str) -> str:
        """SuperDocs' own rendering of the session's document, with the
        formatting still on it.

        This is what the page shows after a turn, and it has to be, because
        the alternative does not work: rebuilding the preview from the
        exported `.docx` goes through `docx.blocks_to_html`, which carries
        structure and text and no formatting at all. Ask for smaller headings
        and that preview comes back byte-identical -- the change is real, in
        the file, and invisible on the screen. A page that cannot show the
        change it just made is worse than one that never offered to.

        Free (a history read). Empty string when it could not be read, which
        costs the preview and never the turn.
        """
        body = self._history_body(session_id)
        if body is None:
            return ""
        return (body.get("document_state") or {}).get("html_content") or ""

    def _last_turn_index(self, session_id: str) -> int | None:
        body = self._history_body(session_id)
        entries = (body or {}).get("messages") or []
        if not entries:
            return None
        idx = entries[-1].get("turn_index")
        return int(idx) if idx is not None else None

    def revert(self, session_id: str, turn_index: int) -> "Reverted":
        """Undo the given user turn and its edit together -- native revert,
        not a re-upload, because SuperDocs restores the conversation and the
        document as one unit (PRD §8). Free; the turn cap is the only limit
        on how often this can be pressed. Never raises.
        """
        r = Reverted()
        try:
            resp = requests.post(
                f"{BASE}/v1/sessions/{session_id}/revert",
                headers=self._headers(),
                json={"turn_index": turn_index},
                timeout=30,
            )
            if resp.status_code == 409:
                r.note = ("That change is still going through. You can put "
                          "it back once it has landed.")
                return r
            if resp.status_code == 422:
                r.note = ("That change is too old for SuperDocs to undo on "
                          "its own, so nothing was changed here.")
                return r
            resp.raise_for_status()
            body = resp.json() or {}
            r.ok = True
            r.compose_text = str(body.get("compose_text", "") or "")
            r.reverted_to_turn = int(body.get("reverted_to_turn", -1))
            r.archived_turn_count = int(body.get("archived_turn_count", 0) or 0)
            r.note = "That change has been put back."
            return r
        except Exception as exc:  # noqa: BLE001 -- never raise out of a revert
            log.warning("SuperDocs revert failed (%s)", exc)
            r.note = ("That change could not be put back just now. Your "
                      "document is unchanged.")
            return r

    def history(self, session_id: str) -> list[dict]:
        """The session's message history, each entry carrying `turn_index`
        and `checkpoint_id` (PRD §8). Free. Never raises -- an unreadable
        history comes back empty rather than failing its caller."""
        body = self._history_body(session_id)
        if body is None:
            return []
        entries = body.get("messages")
        if entries is None:
            entries = body.get("history") or []
        return list(entries)

    def _durable_document_id(self, session_id: str) -> str | None:
        """Resolve the session's focused document to the permanent id
        `structure` needs. `GET /v1/documents/{id}` 400s on the session id
        and on the session-local slot id (e.g. `doc_primary`) alike -- it
        wants `durable_document_id`, which only this free read
        (`GET /v1/sessions/{id}/documents`) carries."""
        try:
            resp = requests.get(
                f"{BASE}/v1/sessions/{session_id}/documents",
                headers=self._headers(),
                timeout=30,
            )
            resp.raise_for_status()
            body = resp.json() or {}
            docs = body.get("documents") or []
            focused = body.get("focused_document_id")
            for doc in docs:
                if doc.get("document_id") == focused:
                    return doc.get("durable_document_id")
            if docs:
                return docs[0].get("durable_document_id")
            return None
        except Exception as exc:  # noqa: BLE001
            log.warning("SuperDocs durable document id lookup failed (%s)", exc)
            return None

    def structure(self, session_id: str) -> dict | None:
        """The document's structure -- section_count, block_count, media --
        documented as the free "did my edit land?" read (PRD §6). Correct
        and kept public, but turn-level reconciliation uses the session's
        document version instead (`_version_id`), which needs no id
        resolution; this one does, via `_durable_document_id`, so it costs
        an extra free round trip. `None` when either read fails, which is
        not the same as "unchanged" and callers must not treat it as one."""
        try:
            durable_id = self._durable_document_id(session_id)
            if not durable_id:
                return None
            resp = requests.get(
                f"{BASE}/v1/documents/{durable_id}",
                headers=self._headers(),
                timeout=30,
            )
            resp.raise_for_status()
            body = resp.json() or {}
            return body.get("structure", body)
        except Exception as exc:  # noqa: BLE001
            log.warning("SuperDocs structure read failed (%s)", exc)
            return None


def _picture_count(blob: bytes) -> int:
    """How many readable pictures a `.docx` actually carries."""
    import io
    import zipfile

    from . import media

    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            total = 0
            for n in z.namelist():
                if not n.startswith(media.MEDIA_DIR):
                    continue
                blob = z.read(n)
                if media.sniff_ext(blob) and media.is_complete(blob):
                    total += 1
            return total
    except Exception:  # noqa: BLE001 -- an unreadable answer is not a count
        return 0


def _words(text: str) -> list[str]:
    """Text reduced to what a reader would call the words, so that a formatting
    pass -- which may re-wrap, re-space or re-escape -- reads as no change."""
    import html as _html
    import re

    plain = _html.unescape(re.sub(r"<[^>]+>", " ", text))
    return re.findall(r"[a-z0-9]+", plain.lower())


def docx_text(blob: bytes) -> str:
    """The text of a `.docx`, run by run. Deliberately not a full parse: this is
    asked only "what does it say"."""
    import io
    import re
    import zipfile

    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            body = z.read("word/document.xml").decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return ""
    return " ".join(re.findall(r"<w:t[^>]*>([^<]*)</w:t>", body))


def content_drift(sent: bytes, got: bytes) -> tuple[int, int]:
    """(words added, words removed) between what we sent and what came back.

    A multiset rather than a sequence: reordering a table's cells is not this
    guard's business, and inventing a paragraph is.
    """
    from collections import Counter

    before = Counter(_words(docx_text(sent)))
    after = Counter(_words(docx_text(got)))
    return sum((after - before).values()), sum((before - after).values())


def _why_not_acceptable(sent: bytes, got: bytes) -> str:
    """Empty when the styled file may be handed over, else the reason it may not.

    Two checks, both on the same principle: a formatting pass has no business
    changing what the document *is*.

    * **Pictures.** The styling path used to send HTML with the images stripped
      out of it, so every styled file came back with no pictures at all and
      nothing noticed. It now sends the rebuilt file, whose pictures travel with
      it -- and counts them on the way back, because "the docs say images are
      preserved" is a reason to expect it, not a reason to skip checking.
    * **Words.** A document-editing model handed a sparse recovered file will
      fill it out -- observed live on 2026-08-20, where a four-line report came
      back with invented paragraphs, a subtotal row, a disclaimer and a
      signature block. Handing that to somebody who came here to get their own
      words back is the worst output this product could produce: it opens
      cleanly, it looks better than the plain rebuild, and it is partly fiction.
    """
    before, after = _picture_count(sent), _picture_count(got)
    if after < before:
        return f"with {before - after} fewer pictures than it was sent"

    added, removed = content_drift(sent, got)
    if added + removed:
        return f"with the wording changed ({added} added, {removed} removed)"
    return ""
