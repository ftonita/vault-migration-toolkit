"""Adapter: a tree of dotenv files -> legacy export JSON.

Expected layout (one file per app and environment):

    <root>/<team>/<app>/<env>.env       e.g. secrets/payments/orders/prod.env

Every KEY=VALUE line becomes one record: name = key in lower case, team/app/env from the path.
The id is derived from the path and key, so it is stable when the export is regenerated.

    python examples/adapters/env_files.py secrets/ --exclude '*_HOST' --exclude '*_PORT' --out legacy.json
"""

from __future__ import annotations

import argparse
import fnmatch
from datetime import date
from pathlib import Path

from _common import record, write_export


def parse_env(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.removeprefix("export ").partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        out[key.strip()] = value
    return out


def convert(root: Path, rotated_from_mtime: bool = False, exclude: tuple[str, ...] = ()) -> list[dict]:
    records = []
    for f in sorted(root.glob("*/*/*.env")):
        team, app, env = f.parent.parent.name, f.parent.name, f.stem
        rotated = date.fromtimestamp(f.stat().st_mtime).isoformat() if rotated_from_mtime else ""
        for key, value in parse_env(f.read_text(encoding="utf-8")).items():
            if not value or any(fnmatch.fnmatchcase(key, pat) for pat in exclude):
                continue
            records.append(
                record(
                    id=f"env:{team}/{app}/{env}/{key}",
                    name=key.lower(),
                    value=value,
                    app=app,
                    env=env,
                    team=team,
                    last_rotated=rotated,
                    consumers=[app],
                )
            )
    return records


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("root", type=Path, help="directory with <team>/<app>/<env>.env files")
    p.add_argument("--out", help="output JSON (default: stdout)")
    p.add_argument(
        "--rotated-from-mtime", action="store_true", help="use file mtime as last_rotated (best effort)"
    )
    p.add_argument(
        "--exclude", action="append", default=[], metavar="GLOB", help="skip keys that are not secrets"
    )
    a = p.parse_args()
    write_export(convert(a.root, a.rotated_from_mtime, tuple(a.exclude)), a.out)


if __name__ == "__main__":
    main()
