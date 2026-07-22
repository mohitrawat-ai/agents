"""Poller unit tests (issue #10): the two pure mappings.

format_event and terminal_text decide every word the poller posts; both are
pure functions, so they test without Slack, AWS, or Postgres. The loop
mechanics (cursor, grace window, terminal ordering) are proven live per
#10's acceptance list, not mocked here.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "poller"))

from main import EXIT_LABELS, format_event, terminal_text

# --- format_event: milestone -> line, or None ---------------------------------


def test_ack_on_run_started():
    assert format_event("run_started", 1, {}) == (
        "Investigating — I'll post here as I go."
    )


def test_restart_ack_names_the_attempt():
    text = format_event("run_started", 2, {})
    assert "attempt 2" in text
    assert "Restarting" in text


def test_hypothesis_verbs_and_qids():
    formed = format_event(
        "hypothesis", 1, {"status": "formed", "claim": "ALB-side", "qids": ["q03"]}
    )
    assert formed == "Hypothesis: ALB-side (q03)"
    killed = format_event("hypothesis", 1, {"status": "killed", "claim": "DB lock"})
    assert killed == "Ruled out: DB lock"


def test_doc_ready_posts_verdict():
    assert format_event("doc_ready", 1, {"verdict": "ALB 5XX"}) == "*Verdict:* ALB 5XX"


def test_run_failed_posts_reason():
    text = format_event("run_failed", 1, {"reason": "budget cap $5.00 hit"})
    assert text == "Run failed: budget cap $5.00 hit"


def test_unnarrated_kinds_return_none():
    for event in ("tool_call", "instrument_note", "run_finished"):
        assert format_event(event, 1, {}) is None


def test_format_event_is_total_on_malformed_payloads():
    """emit.py validates payload shape as 'an object', nothing deeper. A
    raise here would wedge the cursor on the same row forever (review
    2026-07-23), so garbage must render, not throw."""
    for payload in ({"qids": 5}, {"qids": True}, {"qids": [1, None]}, {"qids": {}}):
        assert isinstance(format_event("hypothesis", 1, payload), str)
    for event in ("timeline_settled", "self_check", "doc_ready", "run_failed"):
        assert isinstance(format_event(event, 1, {}), str)


# --- terminal_text: execution status + last lifecycle row -> closing line -----


def test_succeeded_with_stats():
    text = terminal_text(
        "SUCCEEDED", "s1", {"elapsed_s": 641, "turns": 67, "cost_usd": 2.02}, None
    )
    assert text == (
        "Investigation finished for `s1` in 641s, 67 turns, $2.02 "
        "— document is ready."
    )


def test_succeeded_without_stats_row():
    text = terminal_text("SUCCEEDED", "s1", None, None)
    assert text == "Investigation finished for `s1` — document is ready."


def test_failed_renders_exit_code_label():
    for code, label in EXIT_LABELS.items():
        text = terminal_text("FAILED", "s1", None, {"exit_code": code, "reason": "r"})
        assert label in text
        assert "FAILED" in text


def test_failed_without_row_names_both_causes():
    """No row on the last attempt is either a hard death or a startup
    failure (exit 2 writes no row); the message must not pick one."""
    text = terminal_text("FAILED", "s1", None, None)
    assert "wrote no failure row" in text
    assert "infrastructure death" in text
    assert "startup failure" in text


def test_terminal_text_is_total_on_malformed_payloads():
    text = terminal_text("SUCCEEDED", "s1", {"cost_usd": "NaN?"}, None)
    assert "document is ready" in text
    text = terminal_text("FAILED", "s1", None, {"exit_code": "weird"})
    assert "FAILED" in text


def test_timed_out_and_aborted():
    assert "timeout" in terminal_text("TIMED_OUT", "s1", None, None)
    assert "aborted" in terminal_text("ABORTED", "s1", None, None)
