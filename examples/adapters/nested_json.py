"""Adapter: a nested JSON/YAML-like config document -> legacy export JSON.

Typical input is a config dump where secrets are grouped by team, app and environment:

    {"payments": {"orders": {"prod": {"DB_PASSWORD": "...", "STRIPE_KEY": "..."}}}}

The nesting order is configurable with --levels (default: team,app,env). A leaf may be a plain
string, or an object with "value" and optional "kind", "last_rotated", "consumers".

    python examples/adapters/nested_json.py config.json --levels team,app,env --out legacy.json
    yq -o=json secrets.yml | python examples/adapters/nested_json.py - --out legacy.json
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from _common import record, write_export

LEVELS = ("team", "app", "env")
LEAF_FIELDS = ("value", "kind", "last_rotated", "consumers")


def _is_leaf(node: Any) -> bool:
    return isinstance(node, str) or (isinstance(node, dict) and isinstance(node.get("value"), str))


def convert(doc: dict[str, Any], levels: tuple[str, ...] = LEVELS) -> list[dict]:
    if set(levels) - set(LEVELS) or len(set(levels)) != len(levels):
        raise SystemExit(f"error: --levels must be a permutation of a subset of {','.join(LEVELS)}")
    records: list[dict] = []

    def walk(node: Any, path: tuple[str, ...]) -> None:
        if len(path) == len(levels):
            if not isinstance(node, dict):
                raise SystemExit(f"error: expected an object of secrets at {'/'.join(path)}")
            for name, leaf in sorted(node.items()):
                if not _is_leaf(leaf):
                    raise SystemExit(f"error: {'/'.join((*path, name))}: leaf must be a string or {{value}}")
                extra = leaf if isinstance(leaf, dict) else {"value": leaf}
                extra = {
                    k: v for k, v in extra.items() if k in ("value", "kind", "last_rotated", "consumers")
                }
                records.append(
                    record(
                        id=f"cfg:{'/'.join((*path, name))}",
                        name=name.lower(),
                        **dict(zip(levels, path, strict=True)),
                        **extra,
                    )
                )
            return
        if not isinstance(node, dict):
            raise SystemExit(f"error: expected an object at {'/'.join(path) or '<root>'}")
        for key, child in sorted(node.items()):
            walk(child, (*path, key))

    walk(doc, ())
    return records


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("input", help="JSON file, or - for stdin")
    p.add_argument("--levels", default=",".join(LEVELS), help="nesting order above the secret names")
    p.add_argument("--out", help="output JSON (default: stdout)")
    a = p.parse_args()
    fh = sys.stdin if a.input == "-" else open(a.input, encoding="utf-8")
    with fh:
        doc = json.load(fh)
    write_export(convert(doc, tuple(x.strip() for x in a.levels.split(","))), a.out)


if __name__ == "__main__":
    main()
