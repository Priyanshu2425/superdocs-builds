"""The styling counter's session endpoints -- `docs/PRD-SET-IT-YOUR-WAY.md`.

Offline throughout: a fake stands in for `SuperDocsClient`, so nothing here
touches the network or needs a key (B24) -- important beyond principle, since
this checkout's own `.env` sets a real `SUPERDOCS_API_KEY`. The fake is
installed *before* the recovery itself, so the automatic styling pass inside
`/api/recover` never reaches for the real client either; it is injected by
patching `docrepair.superdocs_client.SuperDocsClient`, which is what every new
route in `web.py` constructs via a lazy, in-function import -- the same house
style `_style` already uses.
"""

from __future__ import annotations

import io
import json
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from tests import fixtures

from docrepair import engine
from docrepair.web import app
import docrepair.web as web


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


# -- the fake platform ---------------------------------------------------------

@dataclass
class FakeTurn:
    ok: bool = True
    output: bytes = b""
    note: str = "SuperDocs applied the change."
    rejected_for_content: bool = False
    turn_index: int = 1
    reconciled: bool = False
    stages: list = field(default_factory=list)
    # Mirrors `superdocs_client.Turn`. A fake that accepts fewer fields than
    # the thing it stands in for is how BUG-015 happened: the tests stayed
    # green against a shape the real client had stopped producing.
    proposed: bool = False
    pending: list = field(default_factory=list)
    job_id: str = ""


@dataclass
class FakeReverted:
    ok: bool = True
    note: str = "Put back."
    compose_text: str = ""
    reverted_to_turn: int = -1
    archived_turn_count: int = 1


class FakeClient:
    """Stands in for `SuperDocsClient` everywhere `web.py` constructs one --
    including the automatic pass inside `/api/recover`, so a recovery run
    under this fake never touches the network either. Records what it was
    sent, so a pre-flight refusal can be proven to have sent nothing."""

    def __init__(self, *, allowance_known=True, allowance_remaining=99,
                turn_result=None, revert_result=None, document_html="",
                decide_result=None):
        self.allowance_known = allowance_known
        self.allowance_remaining = allowance_remaining
        self._turn_result = turn_result
        self._revert_result = revert_result
        self._decide_result = decide_result
        self._document_html = document_html
        self.allowance_calls = 0
        self.open_calls: list[tuple] = []
        self.turn_calls: list[str] = []
        self.revert_calls: list[int] = []
        self.decide_calls: list[tuple] = []

    # -- the automatic pass's interface (see superdocs_client.SuperDocsClient.style)
    def style(self, sent: bytes, filename: str = "recovered.docx", on_progress=None):
        from docrepair.superdocs_client import Styling
        return Styling(ok=False, note="Not exercised by the counter's own tests.")

    # -- the counter's interface --------------------------------------------
    def allowance(self):
        from docrepair.superdocs_client import Allowance
        self.allowance_calls += 1
        return Allowance(known=self.allowance_known, remaining=self.allowance_remaining)

    def open_session(self, rebuild: bytes, filename: str = "recovered.docx") -> str:
        self.open_calls.append((rebuild, filename))
        return "fake-session-1"

    def document_html(self, session_id: str) -> str:
        """SuperDocs' own rendering, which is what the page shows after a
        turn because it is the only one carrying formatting. Empty by
        default here so the tests that care about the fallback to a rebuilt
        preview still exercise it; set `document_html` to override."""
        return self._document_html

    def turn(self, session_id, message, *, sent, authorised, on_progress=None) -> FakeTurn:
        self.turn_calls.append(message)
        if on_progress:
            on_progress("Styling", "Sending your change to SuperDocs…")
            on_progress("Styling", "Applying it…")
        if callable(self._turn_result):
            return self._turn_result(message, sent, authorised)
        return self._turn_result or FakeTurn(output=sent + b"!" + message.encode())

    def decide(self, session_id, job_id, changes, approved, *, sent,
               authorised, on_progress=None) -> FakeTurn:
        self.decide_calls.append((job_id, [c.get("change_id") for c in changes],
                                  approved))
        if on_progress:
            on_progress("Styling",
                        "Applying your change…" if approved
                        else "Discarding that change…")
        if callable(self._decide_result):
            return self._decide_result(job_id, changes, approved)
        if self._decide_result is not None:
            return self._decide_result
        if not approved:
            return FakeTurn(ok=False, output=b"",
                            note="Nothing was changed. That costs you nothing "
                                 "and does not use one of your changes.")
        return FakeTurn(output=sent + b"!approved")

    def revert(self, session_id, turn_index) -> FakeReverted:
        self.revert_calls.append(turn_index)
        return self._revert_result or FakeReverted()


def _recover(client: TestClient, monkeypatch, **fake_kwargs) -> tuple[dict, FakeClient]:
    """Install a fake client, then run a real recovery under it -- offline
    end to end, including the automatic styling pass this build never skips
    (B3). Returns the manifest and the fake, so a test can assert on both."""
    fake = FakeClient(**fake_kwargs)
    monkeypatch.setattr(
        "docrepair.superdocs_client.SuperDocsClient", lambda *a, **k: fake)

    res = client.post(
        "/api/recover", files={"file": ("site.docx", io.BytesIO(fixtures.illustrated()))})
    assert res.status_code == 200
    last = [line for line in res.text.splitlines() if line.startswith("data: ")][-1]
    return json.loads(last[len("data: "):]), fake


def _sse_events(res) -> list[dict]:
    assert res.status_code == 200
    return [json.loads(line[len("data: "):])
            for line in res.text.splitlines() if line.startswith("data: ")]


# -- open -> turn -> download ---------------------------------------------------

def test_open_then_turn_repoints_download_and_plain_download_stays_the_rebuild(
        client, monkeypatch):
    payload, fake = _recover(client, monkeypatch)
    token, plain = payload["token"], payload["plain_download"]

    opened = client.post(f"/api/style/{token}/open").json()
    assert opened["turns_left"] == web.TURNS_CAP
    assert opened["receipts"] == []

    events = _sse_events(client.post(f"/api/style/{token}/turn",
                                     json={"message": "Make the headings bigger"}))
    final = events[-1]

    assert final["applied"] is True
    assert final["download"] not in (None, plain)
    assert final["plain_download"] == plain           # B2
    assert final["turns_left"] == web.TURNS_CAP - 1
    assert fake.turn_calls == ["Make the headings bigger"]

    got = client.get(final["download"])
    assert got.status_code == 200


# -- the free "no" ---------------------------------------------------------------

def test_a_refusal_made_before_sending_sends_nothing(client, monkeypatch):
    # Driven by a limit rather than by what was asked for. Since 2026-08-26
    # the counter refuses nothing on content -- the person is driving, and
    # what they ask SuperDocs for is theirs to ask.
    payload, fake = _recover(client, monkeypatch,
                             allowance_known=True, allowance_remaining=0)
    token = payload["token"]
    client.post(f"/api/style/{token}/open")

    events = _sse_events(client.post(
        f"/api/style/{token}/turn",
        json={"message": "Make the headings smaller"}))
    final = events[-1]

    assert final["applied"] is False
    assert final["turns_left"] == web.TURNS_CAP
    assert fake.turn_calls == [], "a pre-flight refusal must send nothing"


def test_the_turn_cap_refuses_the_eleventh_turn(client, monkeypatch):
    payload, fake = _recover(client, monkeypatch)
    token = payload["token"]
    client.post(f"/api/style/{token}/open")
    web._SESSIONS[token].turns_used = web.TURNS_CAP   # ten already spent

    events = _sse_events(client.post(f"/api/style/{token}/turn",
                                     json={"message": "widen the margins"}))
    final = events[-1]

    assert final["applied"] is False
    assert "tenth change" in final["note"]
    assert fake.turn_calls == []


def test_a_zero_allowance_refuses_before_sending(client, monkeypatch):
    payload, fake = _recover(client, monkeypatch,
                             allowance_known=True, allowance_remaining=0)
    token = payload["token"]
    client.post(f"/api/style/{token}/open")

    events = _sse_events(client.post(f"/api/style/{token}/turn",
                                     json={"message": "widen the margins"}))
    final = events[-1]

    assert final["applied"] is False
    assert fake.turn_calls == [], "nothing may be sent once the limit is reached"
    # The allowance still decides whether to send (B22). It is not what the
    # person is told about: they arrived with a broken file, not an account,
    # and a number describing our metering is not something they can act on.
    assert "allowance" not in final["note"].lower()
    assert "operation" not in final["note"].lower()
    assert final["note"], "a refusal with no reason is a bluff"


def test_an_unknown_allowance_does_not_refuse(client, monkeypatch):
    """`known=False` is "nobody read it", never treated as zero -- B22."""
    payload, fake = _recover(client, monkeypatch,
                             allowance_known=False, allowance_remaining=0)
    token = payload["token"]
    client.post(f"/api/style/{token}/open")

    events = _sse_events(client.post(f"/api/style/{token}/turn",
                                     json={"message": "widen the margins"}))
    final = events[-1]

    assert final["applied"] is True
    assert fake.turn_calls == ["widen the margins"]


# -- progress, in order ----------------------------------------------------------

def test_stages_arrive_before_the_final_manifest_in_order(client, monkeypatch):
    payload, fake = _recover(client, monkeypatch)
    token = payload["token"]
    client.post(f"/api/style/{token}/open")

    events = _sse_events(client.post(f"/api/style/{token}/turn",
                                     json={"message": "hairline table rules"}))

    assert len(events) >= 3
    for event in events[:-1]:
        assert "done" not in event
        assert "stage" in event and "message" in event
    assert events[-1]["done"] is True
    messages = [e["message"] for e in events[:-1]]
    assert messages == [
        "Sending your change to SuperDocs…",
        "Applying it…",
    ]


# -- revert -----------------------------------------------------------------------

def test_revert_restores_the_previous_version_and_returns_compose_text(client, monkeypatch):
    payload, fake = _recover(client, monkeypatch)
    token = payload["token"]
    client.post(f"/api/style/{token}/open")

    first = _sse_events(client.post(f"/api/style/{token}/turn",
                                    json={"message": "hairline table rules"}))[-1]
    fake._revert_result = FakeReverted(compose_text="hairline table rules")
    second = _sse_events(client.post(f"/api/style/{token}/turn",
                                     json={"message": "bigger headings"}))[-1]
    assert first["download"] != second["download"]

    reverted = client.post(f"/api/style/{token}/revert").json()

    assert reverted["ok"] is True
    assert reverted["compose_text"] == "hairline table rules"
    # A fresh token, but the same bytes the first turn produced -- the
    # version stack popped back to it rather than merely renaming a file.
    assert client.get(reverted["download"]).content == client.get(first["download"]).content
    assert reverted["plain_download"] == payload["plain_download"]
    assert fake.revert_calls == [1]


# -- expiry and disposal -----------------------------------------------------------

def test_an_expired_session_answers_with_the_not_held_sentence(client, monkeypatch):
    payload, fake = _recover(client, monkeypatch)
    token = payload["token"]
    client.post(f"/api/style/{token}/open")
    web._SESSIONS[token].last_used_at = time.time() - web.TTL_SECONDS - 1

    got = client.get(f"/api/style/{token}/session")

    assert got.status_code == 404
    assert "no longer being held" in got.json()["detail"]
    assert token not in web._SESSIONS


def test_delete_disposes_and_the_session_is_gone_afterwards(client, monkeypatch):
    payload, fake = _recover(client, monkeypatch)
    token = payload["token"]
    client.post(f"/api/style/{token}/open")
    assert token in web._SESSIONS

    deleted = client.delete(f"/api/style/{token}/session")
    assert deleted.status_code == 200
    assert deleted.json()["disposed"] is True
    assert token not in web._SESSIONS

    got = client.get(f"/api/style/{token}/session")
    assert got.status_code == 404


# -- O3: engine.repair(out_dir=...) -------------------------------------------

def test_repair_out_dir_writes_into_that_directory():
    with tempfile.TemporaryDirectory() as d:
        res = engine.repair(fixtures.illustrated(), "site.docx", out_dir=d)

        assert res.ok is True
        assert res.output_path == str(Path(d) / "local_rebuild.docx")
        assert Path(res.output_path).read_bytes() == res.output


def test_repair_with_no_out_dir_behaves_exactly_as_before():
    res = engine.repair(fixtures.illustrated(), "site.docx")

    assert res.output_path == "local_rebuild.docx"


# -- what the ledger records, and what it says a thing cost ---------------------
#
# Three properties found missing during review, each the same shape: the page
# tells the person something about cost or history that is not what happened.
# The receipts are where V5 says which of the two kinds of no this was, so a
# refusal that leaves no trace is a change somebody asked for and can never
# afterwards see they asked for.

def test_a_refusal_made_before_sending_still_goes_in_the_ledger(client, monkeypatch):
    payload, fake = _recover(client, monkeypatch,
                             allowance_known=True, allowance_remaining=0)
    token = payload["token"]
    client.post(f"/api/style/{token}/open")

    client.post(f"/api/style/{token}/turn",
                json={"message": "Make the headings smaller"})

    receipts = client.get(f"/api/style/{token}/session").json()["receipts"]
    assert len(receipts) == 1, "a refusal must leave a trace"
    assert receipts[0]["applied"] is False
    assert receipts[0]["turn_index"] is None      # never sent, nothing to revert
    assert fake.turn_calls == []


def test_a_turn_that_did_not_land_leaves_the_document_alone(client, monkeypatch):
    """Whatever went wrong, the previous version stands and the person is
    told in one sentence. What the attempt cost us is not on the page."""
    payload, fake = _recover(
        client, monkeypatch,
        turn_result=FakeTurn(ok=False,
                             note="SuperDocs could not apply that change, so "
                                  "your document is unchanged."))
    token = payload["token"]
    client.post(f"/api/style/{token}/open")

    final = _sse_events(client.post(f"/api/style/{token}/turn",
                                    json={"message": "Tighten the spacing"}))[-1]

    assert final["applied"] is False
    assert final["note"]
    assert "counted" not in final["note"].lower()


def test_a_refused_turn_does_not_leave_the_authorised_baseline_moved(
        client, monkeypatch):
    """B20'. The baseline moves only when the person's words actually landed.
    A turn that supplied text and was then refused must not go on authorising
    those words for every turn after it."""
    payload, fake = _recover(
        client, monkeypatch,
        turn_result=FakeTurn(ok=False, rejected_for_content=True,
                             note="That change did not land."))
    token = payload["token"]
    client.post(f"/api/style/{token}/open")

    client.post(f"/api/style/{token}/turn",
                json={"message": 'replace the last line with "and the crane is approved"'})

    state = client.get(f"/api/style/{token}/session").json()
    assert state["authored_words"] == 0, (
        "a refused turn must not leave its words authorised")


def test_an_applied_turn_does_move_the_baseline_and_counts_the_words(
        client, monkeypatch):
    """The other half of B27: words the person supplied and that did land are
    theirs, counted, and said out loud in the handover."""
    payload, fake = _recover(client, monkeypatch)
    token = payload["token"]
    client.post(f"/api/style/{token}/open")

    client.post(f"/api/style/{token}/turn",
                json={"message": 'replace the last line with "and the crane is approved"'})

    state = client.get(f"/api/style/{token}/session").json()
    assert state["authored_words"] == 5, "five words supplied, five counted as theirs"


# -- the sheet follows the file -------------------------------------------------
#
# V5's toggle asks "as it came back" against "with your changes". A toggle that
# cannot show the second half is decoration, and DESIGN.md is explicit that the
# verdict is a claim while the sheet is the thing itself.

def _a_changed_document() -> bytes:
    """A valid `.docx` that plainly differs from the recovery's own output, so
    a preview built from it cannot be mistaken for the one before it."""
    from docrepair import docx as _docx
    from docrepair.docx import Block

    return _docx.write_docx([
        Block("heading", "Site inspection", 2),
        Block("paragraph", "The east elevation, photographed on arrival:"),
        Block("paragraph", "No further defects were observed."),
    ])


def test_an_applied_turn_shows_the_changed_document_not_just_a_file(
        client, monkeypatch):
    changed = _a_changed_document()
    payload, fake = _recover(client, monkeypatch,
                             turn_result=FakeTurn(ok=True, output=changed))
    token = payload["token"]
    before = payload["preview_html"]
    client.post(f"/api/style/{token}/open")

    final = _sse_events(client.post(f"/api/style/{token}/turn",
                                    json={"message": "Plainer headings"}))[-1]

    assert final["applied"] is True
    assert final["preview_html"], "a turn that changed the document must show it"
    assert final["preview_html"] != before, (
        "the toggle needs two sides that actually differ")
    assert "Site inspection" in final["preview_html"]


def test_putting_a_change_back_puts_the_sheet_back_too(client, monkeypatch):
    changed = _a_changed_document()
    payload, fake = _recover(client, monkeypatch,
                             turn_result=FakeTurn(ok=True, output=changed))
    token = payload["token"]
    client.post(f"/api/style/{token}/open")
    client.post(f"/api/style/{token}/turn", json={"message": "Plainer headings"})
    assert client.get(f"/api/style/{token}/session").json()["preview_html"]

    client.post(f"/api/style/{token}/revert")

    state = client.get(f"/api/style/{token}/session").json()
    assert state["preview_html"] == "", (
        "a sheet still showing the change just put back is the page "
        "disagreeing with itself about what the document says")


def test_a_preview_that_cannot_be_read_costs_the_toggle_and_not_the_turn(
        client, monkeypatch):
    """An unreadable export must never fail a turn that otherwise landed."""
    payload, fake = _recover(client, monkeypatch,
                             turn_result=FakeTurn(ok=True, output=b"not a document"))
    token = payload["token"]
    client.post(f"/api/style/{token}/open")

    final = _sse_events(client.post(f"/api/style/{token}/turn",
                                    json={"message": "Tighter spacing"}))[-1]

    assert final["applied"] is True, "the turn still landed"
    assert final["preview_html"] == ""
    assert final["download"], "and the file is still there to download"


# -- the review gate -------------------------------------------------------------
#
# BUG-097. A turn proposes; the person decides; only then does anything land.


_PROPOSED = [
    {"change_id": "ch_1", "operation": "edit", "chunk_id": "c-1",
     "old_html": "<h1>Notes</h1>", "new_html": "<h2>Notes</h2>",
     "ai_explanation": "Made the heading one size smaller."},
]


def _proposing(message, sent, authorised):
    return FakeTurn(ok=False, output=b"", proposed=True, pending=_PROPOSED,
                    job_id="job-9",
                    note="SuperDocs proposes 1 change. Nothing has changed "
                         "yet — read it and decide.")


def _at_a_proposal(client, monkeypatch):
    """A counter open on a document with one change waiting to be decided."""
    payload, fake = _recover(client, monkeypatch, turn_result=_proposing)
    token = payload["token"]
    client.post(f"/api/style/{token}/open")
    events = _sse_events(client.post(f"/api/style/{token}/turn",
                                     json={"message": "Make the headings smaller"}))
    return token, fake, events[-1]


def test_a_proposed_turn_changes_nothing_and_costs_nothing(client, monkeypatch):
    token, fake, final = _at_a_proposal(client, monkeypatch)

    assert final["proposed"] is True
    assert final["applied"] is False
    # Not yet counted: reading a proposal and saying no must be free.
    assert final["turns_left"] == web.TURNS_CAP
    # No receipt yet either -- nothing has happened to record.
    assert final["receipts"] == []
    # The download still points at the untouched rebuild.
    assert final["download"] is None or final["download"] == final["plain_download"]


def test_the_page_is_told_what_would_change_and_why(client, monkeypatch):
    token, _, final = _at_a_proposal(client, monkeypatch)

    pending = final["pending"]
    assert pending["asked"] == "Make the headings smaller"
    change = pending["changes"][0]
    assert change["old_html"] == "<h1>Notes</h1>"
    assert change["new_html"] == "<h2>Notes</h2>"
    # The docs are explicit that this sentence is shown to the person.
    assert change["ai_explanation"]
    # The job id is ours, not theirs.
    assert "job_id" not in pending


def test_reopening_the_counter_finds_the_review_rather_than_losing_it(
        client, monkeypatch):
    token, _, _ = _at_a_proposal(client, monkeypatch)

    state = client.get(f"/api/style/{token}/session").json()

    assert state["pending"]["changes"][0]["change_id"] == "ch_1"
    assert state["turns_left"] == web.TURNS_CAP


def test_approving_applies_it_counts_it_and_repoints_the_download(
        client, monkeypatch):
    token, fake, final = _at_a_proposal(client, monkeypatch)
    plain = final["plain_download"]

    done = _sse_events(client.post(f"/api/style/{token}/approve",
                                   json={"approved": True}))[-1]

    assert fake.decide_calls == [("job-9", ["ch_1"], True)]
    assert done["applied"] is True
    assert done["turns_left"] == web.TURNS_CAP - 1
    assert done["download"] and done["download"] != plain
    assert done["pending"] is None
    assert [r["applied"] for r in done["receipts"]] == [True]


def test_discarding_leaves_the_document_alone_and_costs_no_turn(
        client, monkeypatch):
    token, fake, final = _at_a_proposal(client, monkeypatch)
    plain = final["plain_download"]

    done = _sse_events(client.post(f"/api/style/{token}/approve",
                                   json={"approved": False}))[-1]

    assert fake.decide_calls == [("job-9", ["ch_1"], False)]
    assert done["applied"] is False
    assert done["turns_left"] == web.TURNS_CAP, "a discarded change must be free"
    assert done["pending"] is None
    assert done["download"] is None or done["download"] == plain
    # It still leaves a trace: a decision the person made is theirs to see.
    assert [r["applied"] for r in done["receipts"]] == [False]


def test_deciding_when_nothing_is_waiting_is_refused(client, monkeypatch):
    payload, _ = _recover(client, monkeypatch)
    token = payload["token"]
    client.post(f"/api/style/{token}/open")

    res = client.post(f"/api/style/{token}/approve", json={"approved": True})

    assert res.status_code == 409


def test_a_review_in_the_way_is_said_rather_than_sent_and_failed(
        client, monkeypatch):
    """SuperDocs answers a revert with 409 while a review is open. The page
    says so instead of firing a call it knows will fail."""
    token, fake, _ = _at_a_proposal(client, monkeypatch)

    res = client.post(f"/api/style/{token}/revert").json()

    assert res["ok"] is False
    assert fake.revert_calls == []
    assert "decide" in res["note"].lower()


def test_proposed_markup_is_scrubbed_before_it_reaches_the_page(client, monkeypatch):
    """`old_html`/`new_html` are SuperDocs' markup and are rendered as markup,
    so they take the same scrubbing the sheet does rather than being trusted
    for coming from the API."""
    hostile = [{
        "change_id": "ch_x", "operation": "edit", "chunk_id": "c-1",
        "old_html": "<p>before</p><script>alert(1)</script>",
        "new_html": "<p onclick=\"steal()\">after</p>",
        "ai_explanation": "Tidied it.",
    }]

    def proposing(message, sent, authorised):
        return FakeTurn(ok=False, output=b"", proposed=True, pending=hostile,
                        job_id="job-x", note="SuperDocs proposes 1 change.")

    payload, _ = _recover(client, monkeypatch, turn_result=proposing)
    token = payload["token"]
    client.post(f"/api/style/{token}/open")
    final = _sse_events(client.post(f"/api/style/{token}/turn",
                                    json={"message": "Tidy it"}))[-1]

    change = final["pending"]["changes"][0]
    assert "<script" not in (change["old_html"] or "")
    assert "alert(1)" not in (change["old_html"] or "")
    assert "onclick" not in (change["new_html"] or "")
    # The words themselves survive -- scrubbing must not eat the diff.
    assert "before" in (change["old_html"] or "")
    assert "after" in (change["new_html"] or "")


# -- opening twice --------------------------------------------------------------
#
# Found live: the page opened the counter twice (React re-mounts the screen in
# development), the second open landed *after* a turn had already proposed a
# change, and `_open_session` replaced the session -- so the review the person
# was looking at no longer existed and deciding it answered 409.


def test_opening_a_standing_counter_resumes_it_rather_than_replacing_it(
        client, monkeypatch):
    token, fake, final = _at_a_proposal(client, monkeypatch)
    assert final["pending"] is not None

    again = client.post(f"/api/style/{token}/open").json()

    # Same conversation, not a fresh one: one upload, one session.
    assert len(fake.open_calls) == 1
    assert again["session"] == "fake-session-1"
    # And the change waiting to be decided is still waiting.
    state = client.get(f"/api/style/{token}/session").json()
    assert state["pending"]["changes"][0]["change_id"] == "ch_1"
    done = _sse_events(client.post(f"/api/style/{token}/approve",
                                   json={"approved": True}))[-1]
    assert done["applied"] is True


def test_a_resumed_counter_keeps_its_receipts_and_its_count(client, monkeypatch):
    payload, fake = _recover(client, monkeypatch)
    token = payload["token"]
    client.post(f"/api/style/{token}/open")
    _sse_events(client.post(f"/api/style/{token}/turn",
                            json={"message": "Make the headings bigger"}))

    again = client.post(f"/api/style/{token}/open").json()

    assert again["turns_left"] == web.TURNS_CAP - 1, "a resumed counter is not a fresh one"
    assert [r["asked"] for r in again["receipts"]] == ["Make the headings bigger"]


def test_two_opens_arriving_together_still_mint_one_session(client, monkeypatch):
    """The live failure was a race, not a sequence: both opens were in flight
    before either finished, so both found no session and both minted one."""
    import threading

    payload, fake = _recover(client, monkeypatch)
    token = payload["token"]

    started = threading.Barrier(2, timeout=5)
    slow = threading.Event()
    real_open = fake.open_session

    def open_session(rebuild, filename="recovered.docx"):
        # Hold the first opener inside the network call, which is where the
        # second one used to slip past it.
        slow.wait(timeout=2)
        return real_open(rebuild, filename)

    fake.open_session = open_session

    results: list = []

    def go():
        started.wait()
        results.append(client.post(f"/api/style/{token}/open").status_code)

    threads = [threading.Thread(target=go) for _ in range(2)]
    for t in threads:
        t.start()
    slow.set()
    for t in threads:
        t.join(timeout=10)

    assert results == [200, 200]
    assert len(fake.open_calls) == 1, "the same document was uploaded twice"
