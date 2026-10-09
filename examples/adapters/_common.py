"""Shared helpers for the example adapters (stdlib only, never print values)."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

FIELDS = ("id", "name", "value", "app", "env", "team", "kind", "last_rotated", "consumers")


def record(**kw: Any) -> dict[str, Any]:
    """One export record with every column present, in a stable order."""
    unknown = set(kw) - set(FIELDS)
    if unknown:
        raise ValueError(f"unknown fields: {sorted(unknown)}")
    return {f: kw.get(f, [] if f == "consumers" else "") for f in FIELDS}


def write_export(records: list[dict[str, Any]], out: str | None) -> None:
    """Write the export as JSON. A file is created 0600 because it holds plaintext values."""
    ids = [r["id"] for r in records]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        raise SystemExit(f"error: duplicate ids: {', '.join(dupes)}")
    text = json.dumps(records, indent=2, ensure_ascii=False) + "\n"
    if out is None:
        sys.stdout.write(text)
    else:
        path = Path(out)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
    print(f"{len(records)} records exported", file=sys.stderr)
