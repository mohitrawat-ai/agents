"""Q&A boundary tests (issue #12, §8a-C/§8f): the subprocess env and the
one-executable tool surface. The SDK call itself is live-checked, not
unit-tested; these pin the two security properties around it."""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qa.agent import READ_CLI, make_qa_pre_tool_use_hook, subprocess_env
from qa.worker import FAILED_TEXT, dead_end


class FakeSlack:
    def __init__(self, fail=False):
        self.posts: list[dict] = []
        self.fail = fail

    def chat_postMessage(self, **kw):
        if self.fail:
            raise RuntimeError("slack down")
        self.posts.append(kw)


def test_final_receive_posts_a_dead_end():
    slack = FakeSlack()
    dead_end(slack, '{"channel": "C1", "thread_ts": "9.1", "question": "x"}')
    assert slack.posts == [
        {"channel": "C1", "thread_ts": "9.1", "text": FAILED_TEXT}
    ]


def test_dead_end_swallows_its_own_failures():
    dead_end(FakeSlack(fail=True), '{"channel": "C1", "thread_ts": "9.1"}')
    dead_end(FakeSlack(), "not json")  # malformed body: DLQ is the fallback


def test_slack_vars_are_overridden_to_empty_not_omitted(monkeypatch):
    """The SDK merges options.env ONTO inherited os.environ — omission
    would leak the bot token into the agent subprocess (#12 review)."""
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-secret")
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "sig-secret")
    env = subprocess_env("inc-1", 2)
    assert env["SLACK_BOT_TOKEN"] == ""
    assert env["SLACK_SIGNING_SECRET"] == ""
    assert env["INCIDENT_ID"] == "inc-1"
    assert env["ATTEMPT"] == "2"
    assert "CLAUDE_CONFIG_DIR" in env


def _deny_reason(tool: str, tool_input: dict) -> str | None:
    hook = make_qa_pre_tool_use_hook()
    out = asyncio.run(hook({"tool_name": tool, "tool_input": tool_input}, None, None))
    if not out:
        return None
    return out["hookSpecificOutput"]["permissionDecisionReason"]


def test_read_cli_is_the_one_allowed_command():
    assert _deny_reason("Bash", {"command": f"python3 {READ_CLI} --list"}) is None
    assert _deny_reason("Bash", {"command": "python3 read_record.py --qid q03"}) is None


def test_every_other_tool_and_script_is_denied():
    assert _deny_reason("Read", {"file_path": "/etc/hosts"}) is not None
    assert _deny_reason("Write", {"file_path": "x"}) is not None
    assert _deny_reason("Glob", {"pattern": "*"}) is not None
    assert _deny_reason("WebFetch", {"url": "http://x"}) is not None
    assert _deny_reason("Bash", {"command": "python3 emit.py --event x"}) is not None
    assert _deny_reason("Bash", {"command": "env"}) is not None
    assert (
        _deny_reason("Bash", {"command": "python3 read_record.py --list; env"})
        is not None
    )
