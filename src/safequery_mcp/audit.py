"""Append-only audit log with a hash chain.

Every tool call becomes one JSON line. Each line carries the SHA-256 of the
previous line, so editing or deleting an earlier entry breaks the chain and
`verify` reports where.

The chain makes tampering evident, not impossible: someone who can rewrite the
whole file can rebuild it. Ship the log to storage the gateway cannot modify if
that matters.
"""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

GENESIS = "0" * 64


def _digest(entry: dict[str, Any]) -> str:
    body = json.dumps(entry, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    entries: int
    bad_line: int | None = None
    reason: str | None = None


class AuditLog:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._last_hash = self._read_last_hash()

    def _read_last_hash(self) -> str:
        if not self.path.is_file():
            return GENESIS
        last = GENESIS
        with self.path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    last = json.loads(line)["hash"]
        return last

    def record(self, **fields: Any) -> dict[str, Any]:
        with self._lock:
            entry: dict[str, Any] = {
                "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                **fields,
                "prev": self._last_hash,
            }
            entry["hash"] = _digest(entry)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            self._last_hash = entry["hash"]
            return entry


def verify(path: Path) -> VerifyResult:
    path = Path(path)
    if not path.is_file():
        return VerifyResult(ok=True, entries=0)
    prev = GENESIS
    count = 0
    with path.open("r", encoding="utf-8") as f:
        for number, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                claimed = entry.pop("hash")
            except (ValueError, KeyError):
                return VerifyResult(False, count, number, "line is not a valid audit entry")
            if entry.get("prev") != prev:
                return VerifyResult(False, count, number, "does not link to the previous entry")
            if _digest(entry) != claimed:
                return VerifyResult(False, count, number, "content does not match its hash")
            prev = claimed
            count += 1
    return VerifyResult(ok=True, entries=count)
