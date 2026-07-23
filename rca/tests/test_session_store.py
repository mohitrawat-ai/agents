"""S3SessionStore against the SDK's own conformance suite (capture layer).

The SDK ships the behavioral contract as a test
(claude_agent_sdk.testing.run_session_store_conformance): append/load
round-trips, ordering, the never-written null case, uuid dedup on
redelivered batches. Optional-method tests skip because the adapter
deliberately omits them. FakeS3 stands in for boto3 — the adapter's
client is injectable for exactly this — so the suite exercises our part
numbering, pagination, and dedup logic, not AWS.
"""

import asyncio
import sys
from pathlib import Path

from claude_agent_sdk.testing import run_session_store_conformance

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "investigator"))

from session_store import S3SessionStore

PAGE = 2  # tiny page size so pagination is exercised, not just present


class FakeS3:
    """The three calls the adapter makes, dict-backed, with pagination."""

    def __init__(self):
        self.objects: dict[str, bytes] = {}

    def put_object(self, Bucket, Key, Body):
        self.objects[f"{Bucket}/{Key}"] = Body

    def list_objects_v2(self, Bucket, Prefix, ContinuationToken=None):
        matches = sorted(
            k.removeprefix(f"{Bucket}/")
            for k in self.objects
            if k.startswith(f"{Bucket}/{Prefix}")
        )
        start = int(ContinuationToken) if ContinuationToken else 0
        page = matches[start : start + PAGE]
        out = {"Contents": [{"Key": k} for k in page]}
        if start + PAGE < len(matches):
            out["NextContinuationToken"] = str(start + PAGE)
        return out

    def get_object(self, Bucket, Key):

        class Body:
            def __init__(self, data):
                self._data = data

            def read(self):
                return self._data

        return {"Body": Body(self.objects[f"{Bucket}/{Key}"])}


def test_s3_store_conformance():
    # the suite calls the factory once per case and expects an isolated
    # store each time, so every call gets its own fake bucket
    asyncio.run(
        run_session_store_conformance(
            lambda: S3SessionStore(bucket="rca-sessions", client=FakeS3())
        )
    )


def test_load_dedupes_redelivered_batch():
    """The SDK contract: a retried append can redeliver entries that
    already landed; load must treat uuid as the idempotency key."""

    async def scenario():
        store = S3SessionStore(bucket="b", client=FakeS3())
        key = {"project_key": "p", "session_id": "s"}
        batch = [{"uuid": "u1", "type": "assistant"}, {"uuid": "u2", "type": "user"}]
        await store.append(key, batch)
        await store.append(key, batch)  # the retry double-delivery
        await store.append(key, [{"type": "marker-no-uuid"}])
        return await store.load(key)

    loaded = asyncio.run(scenario())
    assert [e.get("uuid") for e in loaded] == ["u1", "u2", None]


def test_load_never_written_returns_none():
    store = S3SessionStore(bucket="b", client=FakeS3())
    out = asyncio.run(store.load({"project_key": "p", "session_id": "nope"}))
    assert out is None


def test_counter_resumes_after_process_restart():
    """A fresh adapter instance over the same bucket must continue part
    numbering, not overwrite part 00001 (mirror restarted mid-session)."""

    async def scenario():
        fake = FakeS3()
        key = {"project_key": "p", "session_id": "s"}
        a = S3SessionStore(bucket="b", client=fake)
        await a.append(key, [{"uuid": "u1"}])
        b = S3SessionStore(bucket="b", client=fake)  # new process, same store
        await b.append(key, [{"uuid": "u2"}])
        return fake, await b.load(key)

    fake, loaded = asyncio.run(scenario())
    assert len(fake.objects) == 2
    assert [e["uuid"] for e in loaded] == ["u1", "u2"]
