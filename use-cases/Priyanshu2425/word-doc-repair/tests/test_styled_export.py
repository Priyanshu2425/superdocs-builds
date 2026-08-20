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
                 never_settles=False):
        self.fail_at, self.no_job, self.no_file = fail_at, no_job, no_file
        self.quota_exhausted = quota_exhausted
        self.never_settles = never_settles
        self.calls, self.approved, self.uploaded_html = [], [], None
        self._approved = False

    def request(self, method, path, **kw):
        self.calls.append(path)
        if self.fail_at and self.fail_at in path:
            raise ConnectionError("network went away")

        usage = {"ops_charged": 1, "monthly_remaining": 10,
                 "quota_exhausted": self.quota_exhausted}

        if path == "/v1/documents/upload":
            self.uploaded_html = kw["files"]["file"][1]
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
            return Response(200, {"raw": b"" if self.no_file else b"STYLED-DOCX"}, {})
        return Response(404, {})


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
    assert r.ok and r.output == b"STYLED-DOCX"
    order = [c for c in fake.calls if not c.startswith("/v1/jobs/")]
    assert order == ["/v1/documents/upload", "/v1/chat/async",
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
