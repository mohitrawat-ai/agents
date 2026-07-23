"""Tests for emit.py's doc_ready upload — DB-free (db and lf_mirror faked).

The DB layer itself is test_tools_db.py's job (needs .env DSNs); this file
proves emit's own logic: which files ride doc_ready into documents, and
that a missing rca.md fails where a missing feedback.md must not
(ruled 2026-07-23 — the feedback slot is optional, the document is not).

Run:  uv run pytest tests/test_emit.py
"""

import contextlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import emit


class FakeConn:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def transaction(self):
        return contextlib.nullcontext()


def wire(monkeypatch):
    """Fake the sinks; return the lists that record what emit inserted."""
    events: list[tuple[str, dict]] = []
    documents: list[tuple[str, str]] = []
    monkeypatch.setattr(emit.db, "connect", lambda: FakeConn())
    monkeypatch.setattr(
        emit.db,
        "insert_event",
        lambda _conn, event, fields: events.append((event, fields)),
    )
    monkeypatch.setattr(
        emit.db,
        "insert_document",
        lambda _conn, name, content: documents.append((name, content)),
    )
    monkeypatch.setattr(emit, "mirror_event", lambda *_a, **_k: None)
    return events, documents


def make_run_dir(tmp_path: Path, rca: bool = True, feedback: bool = False) -> Path:
    """The task layout: workdir/run is the run dir, feedback.md sits one up."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    if rca:
        (run_dir / "rca.md").write_text("# RCA\n\n**Verdict:** it broke.\n")
    if feedback:
        (tmp_path / "feedback.md").write_text("# Devops feedback — slug\n")
    return run_dir


def test_doc_ready_uploads_rca(tmp_path, monkeypatch):
    events, documents = wire(monkeypatch)
    run_dir = make_run_dir(tmp_path)
    assert emit.main(["--dir", str(run_dir), "doc_ready", '{"verdict": "x"}']) == 0
    assert [e for e, _ in events] == ["doc_ready"]
    assert [name for name, _ in documents] == ["rca.md"]


def test_doc_ready_uploads_feedback_when_present(tmp_path, monkeypatch):
    _events, documents = wire(monkeypatch)
    run_dir = make_run_dir(tmp_path, feedback=True)
    assert emit.main(["--dir", str(run_dir), "doc_ready", '{"verdict": "x"}']) == 0
    assert [name for name, _ in documents] == ["rca.md", "feedback.md"]
    assert documents[1][1].startswith("# Devops feedback")


def test_doc_ready_without_rca_fails_before_any_insert(tmp_path, monkeypatch):
    events, documents = wire(monkeypatch)
    run_dir = make_run_dir(tmp_path, rca=False, feedback=True)
    assert emit.main(["--dir", str(run_dir), "doc_ready", '{"verdict": "x"}']) == 2
    assert events == []
    assert documents == []


def test_other_events_upload_nothing(tmp_path, monkeypatch):
    events, documents = wire(monkeypatch)
    run_dir = make_run_dir(tmp_path, feedback=True)
    code = emit.main(
        ["--dir", str(run_dir), "hypothesis", '{"status": "formed", "claim": "c"}']
    )
    assert code == 0
    assert [e for e, _ in events] == ["hypothesis"]
    assert documents == []


def test_malformed_fields_is_exit_2(tmp_path, monkeypatch):
    events, _documents = wire(monkeypatch)
    run_dir = make_run_dir(tmp_path)
    assert emit.main(["--dir", str(run_dir), "doc_ready", "not json"]) == 2
    assert events == []
