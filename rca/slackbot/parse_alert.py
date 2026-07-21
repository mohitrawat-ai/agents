"""Turn a tagged Slack alert message into alert.json fields + a dedup key.

New code, 2026-07-18 (unit 7). Deliberately shallow: the daemon only needs a
slug, a dedup key, and the verbatim text — semantic parsing (condition,
entity, timezone care) is the investigator's job, per its procedure. The raw
text is stored untouched so nothing the daemon fails to parse is ever lost.
"""

import hashlib
import re
from datetime import UTC, datetime

# common shapes: CloudWatch alarm names, NR condition titles — best-effort only
_CONDITION_PATTERNS = [
    re.compile(r'"([^"]+)"'),  # quoted alarm/condition name
    re.compile(r"alarm[:\s]+([\w\-.]+)", re.I),  # 'ALARM: name' / 'alarm name'
    re.compile(r"^\*?([\w\-.]{8,})\*?\s*$", re.M),  # a lone bold-ish token line
]


def parse_alert(text: str, received_utc: datetime | None = None) -> dict:
    """Return {slug, dedup_key, condition_guess, received_utc, raw}."""
    received = received_utc or datetime.now(UTC)
    condition = None
    for pat in _CONDITION_PATTERNS:
        m = pat.search(text)
        if m:
            condition = m.group(1).strip()
            break

    # dedup key: condition if we found one, else normalized text prefix
    basis = condition or " ".join(text.split())[:120]
    dedup_key = hashlib.sha1(basis.lower().encode()).hexdigest()[:12]

    return {
        "slug": received.strftime("%Y-%m-%dT%H-%MZ"),
        "dedup_key": dedup_key,
        "condition_guess": condition,
        "received_utc": received.isoformat(timespec="seconds"),
        "raw": text,
    }
