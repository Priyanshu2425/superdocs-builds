"""The two endpoints the page actually talks to, exercised as the page does.

`fastapi` is an optional extra — the engine and its suite need nothing
installed — so these skip rather than fail on a bare checkout. They are the
tests that catch the class of defect the module tests cannot: a capability that
exists in the code and is unreachable from the surface a person uses. Styling
was in `styled_export.py` with tests around it for a day before anything on the
web page could start it.
"""

from __future__ import annotations

import json

import pytest

fastapi = pytest.importorskip("fastapi", reason="the web extra is not installed")
pytest.importorskip("multipart", reason="the web extra is not installed")
try:
    from fastapi.testclient import TestClient
except RuntimeError as missing:      # starlette raises this, it does not ImportError
    # Skip, do not blow up collection. `importorskip` cannot help here: the
    # import succeeds far enough to raise from inside starlette, and an
    # uncaught raise at module scope interrupts the *whole* run rather than
    # this file. That is BUG-057, and it is why this is a try rather than one
    # more importorskip line.
    pytest.skip(f"the web extra needs an HTTP client: {missing}",
                allow_module_level=True)

from docrepair import web  # noqa: E402
from tests import broken  # noqa: E402


@pytest.fixture
def client(monkeypatch):
    monkeypatch.delenv("SUPERDOCS_API_KEY", raising=False)
    web._READY.clear()
    return TestClient(web.app)


def stream(response) -> tuple[list[dict], dict]:
    """The page's own read of the wire: stage lines, then the final line."""
    lines = [json.loads(l) for l in response.text.splitlines() if l.strip()]
    final = [l for l in lines if l.get("done")]
    assert len(final) == 1, "a stream must end with exactly one final line"
    return [l for l in lines if not l.get("done")], final[0]


def repair_one(client, data=None, name="broken.docx"):
    r = client.post("/api/repair", files={"file": (name, data or broken.truncated_container())})
    assert r.status_code == 200
    return stream(r)


def test_a_repair_streams_its_real_stages_and_then_the_report(client):
    stages, report = repair_one(client)
    assert stages, "the page was given no stages to show"
    assert report["ok"] and report["download"] and report["style"]
    assert report["preview_html"]


def test_the_repaired_file_can_be_downloaded_more_than_once(client):
    _, report = repair_one(client)
    first = client.get(report["download"])
    second = client.get(report["download"])
    assert first.status_code == second.status_code == 200
    assert first.content == second.content
    assert first.content[:2] == b"PK"
    assert "attachment" in first.headers["content-disposition"]


def test_a_file_that_cannot_be_repaired_is_offered_neither_download_nor_styling(client):
    _, report = repair_one(client, broken.missing_document_part(), "gone.docx")
    assert not report["ok"]
    assert report["download"] is None and report["style"] is None


def test_an_empty_upload_is_refused_in_the_reader_s_words(client):
    r = client.post("/api/repair", files={"file": ("empty.docx", b"")})
    assert r.status_code == 400
    assert "empty" in r.json()["detail"].lower()


def test_a_file_over_the_ceiling_is_refused_before_it_is_read(client):
    r = client.post("/api/repair",
                    files={"file": ("big.docx", b"x" * (web.MAX_BYTES + 1))})
    assert r.status_code == 413


def test_the_page_is_told_styling_is_off_when_no_key_is_configured(client):
    body = client.get("/api/capabilities").json()
    assert body["styling"] is False
    assert "switched off" in body["note"]
    # And it says what is *not* affected, because a person reading a greyed-out
    # step needs to know their file is not the thing that went wrong.
    assert "Nothing else is affected" in body["note"]


def test_styling_is_refused_plainly_rather_than_failing_when_it_is_off(client):
    _, report = repair_one(client)
    r = client.post(report["style"])
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert "switched off" in detail and "still yours" in detail


def test_a_stale_styling_token_is_refused_the_same_way_a_download_is(client):
    assert client.post("/api/style/nosuchtoken").status_code == 404
    assert client.get("/api/download/nosuchtoken").status_code == 404


def test_the_page_is_told_styling_is_on_when_a_key_is_configured(client, monkeypatch):
    monkeypatch.setenv("SUPERDOCS_API_KEY", "sk_test")
    assert client.get("/api/capabilities").json()["styling"] is True


def test_styling_streams_its_own_stages_and_hands_back_a_second_file(client, monkeypatch):
    """The whole point of the endpoint: a *second* file, and the first one still
    there afterwards. A styling pass that replaced the plain rebuild would take
    away the only thing this product promised."""
    monkeypatch.setenv("SUPERDOCS_API_KEY", "sk_test")
    from tests.test_styled_export import FakeSuperDocs

    monkeypatch.setattr(web, "_styling_key", lambda: "sk_test")
    import docrepair.superdocs_client as sc

    # The fake answers every call at once, so nothing here waits on a clock.
    monkeypatch.setattr(sc, "HttpTransport", lambda key: FakeSuperDocs())

    _, report = repair_one(client)
    stages, styled = stream(client.post(report["style"]))
    assert styled["ok"], styled
    assert stages, "nothing was shown while it worked"
    assert styled["download"] and styled["download"] != report["download"]
    assert styled["filename"].endswith("-styled.docx")
    assert client.get(styled["download"]).content[:2] == b"PK"
    # the plain rebuild is untouched and still collectable
    assert client.get(report["download"]).status_code == 200


def test_a_styling_failure_leaves_the_plain_rebuild_exactly_where_it_was(client,
                                                                        monkeypatch):
    monkeypatch.setattr(web, "_styling_key", lambda: "sk_test")
    import docrepair.superdocs_client as sc

    def explode(key):
        raise RuntimeError("no transport today")

    monkeypatch.setattr(sc, "HttpTransport", explode)

    _, report = repair_one(client)
    _, styled = stream(client.post(report["style"]))
    assert styled["ok"] is False
    assert styled["download"] is None
    assert styled["notes"], "it failed without saying anything"
    assert client.get(report["download"]).status_code == 200


def test_no_engine_vocabulary_or_class_name_reaches_the_page_from_either_endpoint(
        client, monkeypatch):
    """BUG-020, guarded on the second endpoint before it can happen there."""
    monkeypatch.setattr(web, "_styling_key", lambda: "sk_test")
    import docrepair.superdocs_client as sc

    def explode(key):
        raise ValueError("ValueError: something internal")

    monkeypatch.setattr(sc, "HttpTransport", explode)

    _, report = repair_one(client)
    stages, styled = stream(client.post(report["style"]))
    text = " ".join([s["message"] for s in stages] + styled["notes"])
    for word in ("ValueError", "RuntimeError", "Traceback", "ZIP", "XML"):
        assert word not in text, f"{word!r} reached a worried person"


def test_a_styled_file_that_was_rewritten_never_reaches_the_download(client, monkeypatch):
    """The guard, at the surface. A rewritten document that got as far as the
    page would be handed over with a download button and a cheerful line about
    styling -- which is how somebody ends up circulating three paragraphs they
    never wrote."""
    monkeypatch.setattr(web, "_styling_key", lambda: "sk_test")
    from tests.test_styled_export import FakeSuperDocs, _rewritten
    import docrepair.superdocs_client as sc

    def transport(key):
        fake = FakeSuperDocs()
        fake.exported = _rewritten()
        return fake

    monkeypatch.setattr(sc, "HttpTransport", transport)

    _, report = repair_one(client)
    _, styled = stream(client.post(report["style"]))
    assert styled["ok"] is False
    assert styled["rejected_for_content"] is True
    assert styled["download"] is None
    assert any("wording changed" in n for n in styled["notes"])
    assert client.get(report["download"]).status_code == 200
