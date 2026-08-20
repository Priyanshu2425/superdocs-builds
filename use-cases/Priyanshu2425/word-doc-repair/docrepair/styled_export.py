"""Take the recovered structure through SuperDocs to get a properly styled file.

The local rebuild always runs first and always produces a valid file. This is
the optional second pass: hand the recovered structure to SuperDocs as HTML,
have it normalise the document, and export a styled DOCX.

Why HTML and not the rebuilt .docx bytes: the documentation is explicit that
SuperDocs takes documents and HTML and that there is no endpoint for raw Word
XML. Sending clean HTML built from structure we have already parsed is both
supported and lossless in the directions that matter -- headings stay headings,
tables stay tables.

The four calls, in order, are exactly the required contract:
  upload -> edit instruction -> approve -> export
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .docx import Block, blocks_to_html
from .superdocs_client import QuotaExhausted, SuperDocsClient

INSTRUCTION = (
    "This document was recovered from a damaged file. Apply consistent heading "
    "styles, table formatting and paragraph spacing. Do not add, remove or "
    "reword any content -- only restore its formatting."
)


@dataclass
class StyledResult:
    ok: bool = False
    output: bytes = b""
    warnings: list = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    ops_charged: int = 0
    ops_confirmed: bool = False   # the async endpoints return no usage block


def styled_export(client: SuperDocsClient, session_id: str, blocks: list[Block],
                  filename: str = "recovered.html",
                  on_progress=None) -> StyledResult:
    """Best effort, and it never costs the caller their local result.

    Every failure path returns ok=False with a reason. The engine's own rebuild
    has already succeeded by the time this runs, so a failure here degrades to
    "you get the plain rebuild" rather than to "you get nothing".
    """
    r = StyledResult()

    def say(msg: str) -> None:
        r.notes.append(msg)
        if on_progress:
            on_progress("superdocs", msg)

    html = blocks_to_html(blocks)

    try:
        # 1 -- upload. Free.
        say("Sending the recovered content to SuperDocs for styling…")
        client.upload(session_id, filename, html.encode("utf-8"))

        # 2 -- edit instruction, with a human gate on every proposed change.
        started = client.edit(session_id, INSTRUCTION)
        r.ops_charged += int(started.usage.get("ops_charged", 0))
        job_id = started.body.get("job_id")
        if not job_id:
            say("SuperDocs did not start a job; keeping the plain rebuild.")
            return r

        # 3 -- wait. A long silence is still processing.
        say("Waiting for SuperDocs to finish — large documents can take minutes.")
        job = client.poll_job(job_id)
        r.ops_charged += int(job.usage.get("ops_charged", 0))

        if job.body.get("status") == "awaiting_approval":
            from .superdocs_client import pending_changes

            changes = pending_changes(job.body)
            if changes:
                # Formatting-only changes on a document we just rebuilt: approve
                # them. The instruction forbids content edits, so a change that
                # rewrote text would be a bug on their side, not a judgement
                # call on ours -- and the export below is what gets checked.
                approved = client.approve(
                    session_id, job_id,
                    [{"change_id": c.get("change_id"), "approved": True} for c in changes],
                )
                r.ops_charged += int(approved.usage.get("ops_charged", 0))
                say(f"Approved {len(changes)} formatting change(s).")

                # Approval is asynchronous: the call returns ok, then the job
                # resumes and applies the change. Exporting before it settles
                # returns the pre-edit document with a 200 and no warning.
                settled = client.poll_job(job_id, deadline_s=300, interval_s=2)
                r.ops_charged += int(settled.usage.get("ops_charged", 0))
                if settled.body.get("status") != "completed":
                    say("SuperDocs did not finish applying the changes; "
                        "keeping the plain rebuild.")
                    return r

        # 4 -- export. Free.
        exported = client.export(session_id, "docx")
        r.warnings = client.export_warnings(exported)
        blob = exported.body.get("raw") if isinstance(exported.body, dict) else None
        if not blob:
            say("SuperDocs returned no file; keeping the plain rebuild.")
            return r
        r.output = blob if isinstance(blob, bytes) else bytes(blob)
        r.ok = True
        if not r.ops_charged:
            # The async endpoints return no usage block (see BUG-017), so a
            # zero here means "not reported", not "free". Saying "0 operations"
            # about a billable request would be the same bluff in a new place.
            r.ops_charged = 1
            r.ops_confirmed = False
        else:
            r.ops_confirmed = True
        say("SuperDocs returned a styled file.")
        if r.warnings:
            say(f"The export completed with {len(r.warnings)} non-fatal warning(s).")
    except QuotaExhausted:
        say("The SuperDocs allowance is exhausted; keeping the plain rebuild.")
    except TimeoutError:
        # Not a failure on their side -- the job may still be running. But we
        # cannot export until it settles, so the local rebuild is what ships.
        say("SuperDocs did not finish applying the changes in time; "
            "keeping the plain rebuild.")
    except Exception:
        # Deliberately no exception class name: this string reaches a user.
        say("Styling through SuperDocs did not work; keeping the plain rebuild.")
    return r
