"""The SuperDocs path, keyless. The rule under test is that it can fail in any
way at all and the user still ends up with the file the local rebuild made."""

import json

import pytest

from docrepair import repair
from docrepair.styled_export import styled_export
from docrepair.superdocs_client import Response, SuperDocsClient
from tests import broken


class FakeSuperDocs:
    def __init__(self, fail_at=None, quota_exhausted=False, no_job=False, no_file=False,
                 never_settles=False, quota_remaining=None):
        self.fail_at, self.no_job, self.no_file = fail_at, no_job, no_file
        self.quota_exhausted = quota_exhausted
        self.never_settles = never_settles
        # None means the balance cannot be read -- which is the ordinary case
        # for a personal key, and is not the same as a balance of zero.
        self.quota_remaining = quota_remaining
        # What the export hands back. A real .docx by default, built from the
        # HTML this fake was given, so the content guard has something honest
        # to check. Tests that want a rewrite pass their own.
        self.exported = None
        self.calls, self.approved, self.uploaded_html = [], [], None
        self._approved = False

    def request(self, method, path, **kw):
        self.calls.append(path)
        if self.fail_at and self.fail_at in path:
            raise ConnectionError("network went away")

        usage = {"ops_charged": 1, "monthly_remaining": 10,
                 "quota_exhausted": self.quota_exhausted}

        if path == "/v1/agents/whoami":
            if self.quota_remaining is None:
                return Response(401, {"detail": "not an agent key"})
            return Response(200, {"quota": {"tier": "free", "monthly_limit": 500,
                                            "used": 500 - self.quota_remaining,
                                            "remaining": self.quota_remaining}})
        if path == "/v1/documents/upload":
            self.uploaded_html = kw["files"]["file"][1]
            if self.exported is None:
                self.exported = docx_of(self.uploaded_html)
            return Response(200, {"ok": True})
        if path == "/v1/chat/async":
            if self.no_job:
                return Response(200, {"usage": usage})
            return Response(200, {"job_id": "j1", "status": "pending", "usage": usage})
        if path.startswith("/v1/jobs/"):
            if self._approved:
                # Approval is asynchronous on the real API: the job resumes and
                # only then reaches completed. Exporting before that returns the
                # pre-edit document with a 200. Verified live 2026-08-19.
                if self.never_settles:
                    return Response(200, {"status": "in_progress", "usage": usage})
                return Response(200, {"status": "completed", "usage": usage})
            batch = json.dumps({"changes": [{"change_id": "ch_1"}]})
            return Response(200, {"status": "awaiting_approval",
                                  "metadata": {"pending_changes": {"content": batch}},
                                  "usage": usage})
        if path.endswith("/approve"):
            self.approved.extend(kw["json"]["changes"])
            self._approved = True
            return Response(200, {"status": "ok", "usage": usage})
        if path == "/v1/documents/export":
            if self.no_file:
                return Response(200, {"raw": b""}, {})
            return Response(200, {"raw": self.exported}, {})
        return Response(404, {})


def docx_of(html) -> bytes:
    """A .docx saying exactly what the HTML says -- a styling pass that changed
    nothing but the styling. The honest answer, which the guard must accept."""
    import re

    from docrepair.docx import Block, write_docx

    text = html.decode("utf-8") if isinstance(html, bytes) else html
    words = re.sub(r"<[^>]+>", " ", text)
    import html as _html

    return write_docx([Block("paragraph", _html.unescape(words))])


def blocks_of(data=None):
    r = repair(data or broken.healthy(), "d.docx")
    from docrepair.docx import read_blocks
    import io, zipfile
    with zipfile.ZipFile(io.BytesIO(r.output)) as z:
        return read_blocks(z.read("word/document.xml"))


def run(fake, blocks=None):
    return styled_export(SuperDocsClient(fake, sleep=lambda s: None),
                         "sess", blocks or blocks_of())


def test_the_four_calls_happen_in_the_required_order():
    fake = FakeSuperDocs()
    r = run(fake)
    assert r.ok and r.output
    order = [c for c in fake.calls if not c.startswith("/v1/jobs/")]
    # The balance read comes first and is not part of the contract -- it is
    # trap 3, asked before the work rather than discovered inside it.
    assert order == ["/v1/agents/whoami",
                     "/v1/documents/upload", "/v1/chat/async",
                     "/v1/chat/sess/approve", "/v1/documents/export"]
    # and it waited for the job to settle before exporting
    approve_at = fake.calls.index("/v1/chat/sess/approve")
    export_at = fake.calls.index("/v1/documents/export")
    assert any(c.startswith("/v1/jobs/") for c in fake.calls[approve_at:export_at]), \
        "exported without waiting for the approved change to be applied"


def test_it_sends_html_never_raw_word_xml():
    """The docs are explicit that there is no endpoint for raw Word XML."""
    fake = FakeSuperDocs()
    run(fake)
    sent = fake.uploaded_html.decode()
    assert "<h1>" in sent and "<table>" in sent
    assert "w:document" not in sent and "PK" not in sent[:4]


def test_the_structure_survives_into_the_html():
    fake = FakeSuperDocs()
    run(fake)
    sent = fake.uploaded_html.decode()
    assert "Quarterly Report" in sent
    assert "<h2>Regional breakdown</h2>" in sent
    assert "<td>EMEA</td>" in sent


def test_the_proposed_change_is_double_parsed_and_actually_approved():
    fake = FakeSuperDocs()
    run(fake)
    assert fake.approved == [{"change_id": "ch_1", "approved": True}]


@pytest.mark.parametrize("kwargs,expect", [
    ({"fail_at": "/v1/chat/async"}, "did not work"),
    ({"fail_at": "/v1/documents/export"}, "did not work"),
    ({"quota_exhausted": True}, "allowance is exhausted"),
    ({"no_job": True}, "did not start a job"),
    ({"no_file": True}, "returned no file"),
])
def test_every_failure_degrades_to_the_local_rebuild_and_says_why(kwargs, expect):
    """The engine has already produced a valid file before this runs. Nothing
    here may take that away from the user."""
    r = run(FakeSuperDocs(**kwargs))
    assert not r.ok
    assert r.output == b""
    assert any(expect in n for n in r.notes), r.notes


def test_it_never_raises_whatever_happens():
    class Exploding:
        def request(self, *a, **k):
            raise RuntimeError("boom")

    r = styled_export(SuperDocsClient(Exploding(), sleep=lambda s: None), "s", blocks_of())
    assert not r.ok and r.notes


def test_it_keeps_the_plain_rebuild_when_the_job_never_finishes_applying():
    """If the job never reaches completed, the styled export would be the
    document as it was before the edit — so the local rebuild is kept instead."""
    r = run(FakeSuperDocs(never_settles=True))
    assert not r.ok
    assert any("did not finish applying" in n for n in r.notes)


def test_the_vendored_client_has_not_drifted():
    """Build B vendors Build A's client so each stands alone in the builds
    repository. The multipart fix for BUG-015 landed in the original and not
    here, so this build kept sending an empty body and getting a 422 long after
    the other one worked. Everything below the docstring must stay identical."""
    import pathlib

    here = (pathlib.Path(__file__).resolve().parents[1]
            / "backend/docrepair/superdocs_client.py")
    there = (pathlib.Path(__file__).resolve().parents[2]
             / "quota-aware-agent/backend/quota_aware_agent/client.py")
    origin_build = there.parents[2]
    if not origin_build.exists():    # published alone, without its sibling
        import pytest
        pytest.skip(f"{origin_build.name} is not in this checkout")
    assert there.exists(), (
        f"{origin_build.name} is here but {there.name} is not where this guard "
        "looks. A guard that skips when its target moves is a guard that has "
        "stopped guarding, and nothing fails."
    )

    def body(p):
        return p.read_text().split('"""', 2)[2]

    assert body(here) == body(there), (
        "the vendored client has drifted from its origin -- re-copy it, "
        "keeping only the docstring different"
    )


def test_it_never_reports_zero_operations_for_a_billable_call():
    """The async endpoints return no usage block, so a zero means "not
    reported", not "free". Printing "0 operations" about a request that was
    billed is the same bluff BUG-017 was about, in a new place."""
    r = run(FakeSuperDocs())
    assert r.ok
    assert r.ops_charged >= 1


def test_it_does_not_spend_an_allowance_it_has_already_been_told_is_gone():
    """Trap 3. A styling pass that cannot finish should never be started.

    The failure this prevents is not a wasted call -- it is a person watching a
    progress line for work that was refused at the far end, which is exactly the
    shape of "it ran for a bit and then wanted money" the README quotes.
    """
    fake = FakeSuperDocs(quota_remaining=0)
    r = run(fake)
    assert not r.ok
    assert fake.calls == ["/v1/agents/whoami"], "it sent something anyway"
    assert r.ops_charged == 0
    assert r.allowance_known and r.allowance_remaining == 0
    assert any("nothing was spent" in n for n in r.notes)
    assert any("still yours" in n for n in r.notes)


def test_a_balance_it_cannot_read_is_not_treated_as_a_balance_of_zero():
    """A personal key is not an agent key, and `whoami` answers only the latter.

    Refusing on a number nobody managed to read would be its own bluff, so the
    work proceeds and the report says the balance was unknown.
    """
    fake = FakeSuperDocs()          # whoami answers 401
    r = run(fake)
    assert r.ok, "an unreadable balance stopped work it had no business stopping"
    assert not r.allowance_known


def test_a_balance_that_is_there_is_read_and_reported():
    fake = FakeSuperDocs(quota_remaining=7)
    r = run(fake)
    assert r.ok
    assert r.allowance_known and r.allowance_remaining == 7


def test_a_balance_read_that_blows_up_never_costs_the_caller_the_styling():
    """The preflight is a courtesy, not a gate. If it cannot answer, it gets out
    of the way."""
    class Exploding(FakeSuperDocs):
        def request(self, method, path, **kw):
            if path == "/v1/agents/whoami":
                raise ConnectionError("no network for this one call")
            return super().request(method, path, **kw)

    r = run(Exploding())
    assert r.ok and not r.allowance_known


def test_a_styled_file_that_says_something_else_is_thrown_away():
    """The defect this guard exists for, seen live on 2026-08-20: a four-line
    recovered report came back with three invented paragraphs, a subtotal row,
    a disclaimer and a signature block. It opens cleanly and it reads better
    than the plain rebuild, and it is partly fiction. Handing that to somebody
    who came here to get their own words back is the worst thing this product
    could do -- worse than returning nothing, because they would not notice.
    """
    fake = FakeSuperDocs()
    fake.exported = _rewritten()
    r = run(fake)
    assert not r.ok, "a rewritten document was handed over as a repair"
    assert r.rejected_for_content
    assert r.output == b""
    assert any("wording changed" in n for n in r.notes)
    assert any("still yours" in n for n in r.notes)


def _rewritten() -> bytes:
    from docrepair.docx import Block, write_docx

    return write_docx([
        Block("heading", "Quarterly Report", level=1),
        Block("paragraph", "Revenue rose in Q3."),
        Block("paragraph", "This growth reflects a sustained commitment to "
                           "client retention and successful expansion."),
    ])


def test_a_styled_file_that_says_the_same_thing_is_accepted():
    """The guard has to let the good case through, or it is just an off switch."""
    r = run(FakeSuperDocs())
    assert r.ok and not r.rejected_for_content


def test_the_drift_check_reads_re_wrapping_and_re_escaping_as_no_change():
    from docrepair.docx import Block, write_docx
    from docrepair.styled_export import content_drift

    html = "<h1>Q3 &amp; Q4</h1><p>Revenue rose\n  \u2014 driven by renewals.</p>"
    same = write_docx([Block("heading", "Q3 & Q4", level=1),
                       Block("paragraph", "Revenue rose \u2014 driven by renewals.")])
    assert content_drift(html, same) == (0, 0)


def test_a_styled_file_that_drops_content_is_thrown_away_too():
    from docrepair.docx import Block, write_docx

    fake = FakeSuperDocs()
    fake.exported = write_docx([Block("paragraph", "Quarterly Report")])
    r = run(fake)
    assert not r.ok and r.rejected_for_content
