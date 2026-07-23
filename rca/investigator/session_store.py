"""S3SessionStore: mirror session transcripts to S3 (capture layer, §8g→§8a-B).

New code, 2026-07-23. Builds the deferred §8a-B capture layer — ruled in
chat 2026-07-23: capture now (audit, context inspection, raw material for
resume and steering), resume and steering stay behind their §8a-B
triggers. The storage shape is P2 §5's, unchanged since it was designed:
S3 has no append, so each `append()` call writes one new part object and
`load()` lists the prefix, sorts, and concatenates. Anthropic's reference
S3 adapter (TypeScript) uses the same shape.

    s3://<bucket>/<prefix>/<project_key>/<session_id>[/<subpath>]/00001.jsonl

Duck-typed against claude_agent_sdk.SessionStore: only `append` and
`load` exist here, on purpose. The optional methods (list, delete,
summaries) stay absent — the SDK probes with hasattr and treats absence
as "not supported"; deletion then is a no-op, which is right for an
append-only mirror. Retention belongs to the bucket's lifecycle rule,
never to code.

Mirroring is best-effort BY THE SDK's CONTRACT, not this class's choice:
the subprocess's local write is already durable before append() is
called; a failing batch is retried 3x then dropped with a
MirrorErrorMessage on the stream (run.py records those in the events
table — a hole in the mirror must be visible in the record, invariant 6).
A retried batch can redeliver entries, so load() dedupes on entry uuid.

One writer per prefix always: each attempt mints a new session id, so
two attempts never share a key (§8a-B's attempt discriminator, for free).

boto3 is sync; calls run via asyncio.to_thread so a slow S3 moment never
stalls the SDK's message loop.
"""

import asyncio
import json

import boto3


class S3SessionStore:
    """`append`/`load` against one bucket. The client is injectable so
    tests run against a fake; region comes from the ambient AWS config
    (task role / env), same as every other boto3 client in this tree."""

    def __init__(self, bucket: str, prefix: str = "sessions", client=None):
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self.s3 = client if client is not None else boto3.client("s3")
        self._counters: dict[str, int] = {}  # key prefix -> next part number

    def _key_prefix(self, key: dict) -> str:
        parts = [self.prefix, key["project_key"], key["session_id"]]
        if key.get("subpath"):
            parts.append(key["subpath"])
        return "/".join(parts) + "/"

    def _list(self, key_prefix: str) -> list[str]:
        """Direct-child object keys under the prefix, sorted (part order =
        name order). Children only: the main transcript's prefix is a parent
        of every subpath prefix (`…/<session>/` vs `…/<session>/subagents/…`),
        so a plain prefix list would sweep subagent parts into a main load."""
        keys: list[str] = []
        token = None
        while True:
            kwargs = {"Bucket": self.bucket, "Prefix": key_prefix}
            if token:
                kwargs["ContinuationToken"] = token
            page = self.s3.list_objects_v2(**kwargs)
            keys += [
                o["Key"]
                for o in page.get("Contents", [])
                if "/" not in o["Key"][len(key_prefix):]
            ]
            token = page.get("NextContinuationToken")
            if not token:
                return sorted(keys)

    def _append_sync(self, key: dict, entries: list) -> None:
        key_prefix = self._key_prefix(key)
        n = self._counters.get(key_prefix)
        if n is None:
            # first append for this key in this process: resume numbering
            # after whatever already exists (same shape as a restarted
            # mirror; harmless extra LIST once per session)
            n = len(self._list(key_prefix)) + 1
        body = "\n".join(json.dumps(e) for e in entries) + "\n"
        self.s3.put_object(
            Bucket=self.bucket,
            Key=f"{key_prefix}{n:05d}.jsonl",
            Body=body.encode(),
        )
        self._counters[key_prefix] = n + 1

    def _load_sync(self, key: dict) -> list | None:
        key_prefix = self._key_prefix(key)
        object_keys = self._list(key_prefix)
        if not object_keys:
            return None  # never written (the contract's null case)
        entries: list = []
        seen: set[str] = set()
        for object_key in object_keys:
            raw = self.s3.get_object(Bucket=self.bucket, Key=object_key)
            for line in raw["Body"].read().decode().splitlines():
                if not line.strip():
                    continue
                entry = json.loads(line)
                # a retried batch can redeliver: uuid is the idempotency
                # key; entries without one (titles, markers) always keep
                uuid = entry.get("uuid") if isinstance(entry, dict) else None
                if uuid is not None:
                    if uuid in seen:
                        continue
                    seen.add(uuid)
                entries.append(entry)
        return entries

    async def append(self, key: dict, entries: list) -> None:
        await asyncio.to_thread(self._append_sync, key, list(entries))

    async def load(self, key: dict) -> list | None:
        return await asyncio.to_thread(self._load_sync, key)
