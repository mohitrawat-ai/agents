"""Mirror receipts and milestones into Langfuse as spans in the run's trace.

Adapted from ingren-rca-bench/tools/bench_query.py::mirror_to_langfuse on
2026-07-18 (the house pattern): one run dir = one trace (id seeded
deterministically from its resolved path) = one session, so an investigation
reads as a single timeline even though every query is a separate process.
No-op unless LANGFUSE_PUBLIC_KEY/SECRET_KEY are set (process env or
rca/.env); never raises — tracing must not break a run (design-v2 D9).

Shared by nrql_log.py, aws_log.py, emit.py, and run.py — the third-consumer
threshold that justified extracting a module.
"""

import json
import os
import sys
from pathlib import Path

RCA_ROOT = Path(__file__).resolve().parents[1]
TAG = "rca-agent"


def _load_env() -> None:
    env_path = RCA_ROOT / ".env"
    if env_path.exists():
        for raw in env_path.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    if os.environ.get("LANGFUSE_BASE_URL") and not os.environ.get("LANGFUSE_HOST"):
        os.environ["LANGFUSE_HOST"] = os.environ["LANGFUSE_BASE_URL"]


def mirror_span(
    run_dir: Path,
    name: str,
    input: dict,
    output: object = None,
    error: str | None = None,
) -> None:
    """Add one span to run_dir's trace. Truncates nothing — the Langfuse UI
    handles large payloads better than a lossy log would."""
    _load_env()
    if not (
        os.environ.get("LANGFUSE_PUBLIC_KEY") and os.environ.get("LANGFUSE_SECRET_KEY")
    ):
        return
    try:
        # import after env is loaded so the client picks up the credentials
        from langfuse import get_client, propagate_attributes

        lf = get_client()
        run = run_dir.resolve()
        run_name = "/".join(run.parts[-2:])  # <slug>/<variant>
        with (
            propagate_attributes(
                trace_name=run_name,
                session_id=run_name,
                tags=[TAG],
                metadata={"run_dir": str(run)},
            ),
            lf.start_as_current_observation(
                as_type="span",
                name=name[:80],
                trace_context={"trace_id": lf.create_trace_id(seed=str(run))},
                input=input,
            ) as span,
        ):
            if error:
                span.update(output=error, level="ERROR", status_message="failed")
            elif output is not None:
                span.update(output=output)
        lf.flush()
    except Exception as exc:  # noqa: BLE001 — tracing must never break a run
        print(f"[langfuse] mirroring failed: {exc}", file=sys.stderr)


def mirror_query(run_dir: Path, entry: dict) -> None:
    """Mirror one queries.jsonl entry (NRQL or aws cmd shape)."""
    query_text = entry.get("nrql") or entry.get("cmd") or ""
    err = entry.get("errors")
    mirror_span(
        run_dir,
        name=f"{entry.get('id', '?')} {entry.get('purpose', '')}",
        input={"purpose": entry.get("purpose"), "query": query_text},
        output={"rows": entry.get("rows"), "results": entry.get("results")}
        if not err
        else None,
        error=json.dumps(err, default=str) if err else None,
    )


def mirror_event(run_dir: Path, event: str, fields: dict) -> None:
    """Mirror one semantic milestone (emit.py) into the same trace."""
    mirror_span(
        run_dir,
        name=f"[{event}] "
        + str(
            fields.get("claim") or fields.get("verdict") or fields.get("summary") or ""
        ),
        input={"event": event, **fields},
    )
