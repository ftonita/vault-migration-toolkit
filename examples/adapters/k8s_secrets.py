"""Adapter: Kubernetes Secrets (kubectl JSON) -> legacy export JSON.

    kubectl get secrets -A -o json > k8s-secrets.json
    python examples/adapters/k8s_secrets.py k8s-secrets.json --env-map payments-prod=prod --out legacy.json

Mapping (each data key of each Secret becomes one record):
  * team  <- label --team-label (default "team")
  * app   <- label "app.kubernetes.io/name", else label "app", else the Secret name
  * env   <- --env-map NAMESPACE=ENV, else label --env-label (default "env"), else the namespace
  * name  <- the data key (lower case, "." and "-" kept; the toolkit slugs the rest)
Service-account tokens, Helm release secrets and docker registry configs are skipped.
Binary values that are not UTF-8 are skipped (reported by id on stderr, never by value).
"""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import sys

from _common import record, write_export

SKIP_TYPES = {"kubernetes.io/service-account-token", "helm.sh/release.v1", "kubernetes.io/dockerconfigjson"}


def convert(doc: dict, *, team_label: str = "team", env_label: str = "env", env_map=None) -> list[dict]:
    env_map = env_map or {}
    items = doc.get("items", [doc]) if doc.get("kind") != "Secret" else [doc]
    records = []
    for s in items:
        if s.get("kind", "Secret") != "Secret" or s.get("type") in SKIP_TYPES:
            continue
        meta = s.get("metadata", {})
        ns, secret = meta.get("namespace", "default"), meta["name"]
        labels = meta.get("labels") or {}
        app = labels.get("app.kubernetes.io/name") or labels.get("app") or secret
        env = env_map.get(ns) or labels.get(env_label) or ns
        for key, b64 in sorted((s.get("data") or {}).items()):
            rid = f"k8s:{ns}/{secret}/{key}"
            try:
                value = base64.b64decode(b64, validate=True).decode("utf-8")
            except (binascii.Error, UnicodeDecodeError):
                print(f"skipped {rid}: not base64-encoded UTF-8", file=sys.stderr)
                continue
            if not value:
                continue
            records.append(
                record(
                    id=rid,
                    name=key.lower(),
                    value=value,
                    app=app,
                    env=env,
                    team=labels.get(team_label, ""),
                    consumers=[f"{ns}/{app}"],
                )
            )
    return records


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("input", help="output of 'kubectl get secrets -o json', or - for stdin")
    p.add_argument("--team-label", default="team")
    p.add_argument("--env-label", default="env")
    p.add_argument("--env-map", action="append", default=[], metavar="NAMESPACE=ENV")
    p.add_argument("--out", help="output JSON (default: stdout)")
    a = p.parse_args()
    env_map = dict(item.split("=", 1) for item in a.env_map)
    fh = sys.stdin if a.input == "-" else open(a.input, encoding="utf-8")
    with fh:
        doc = json.load(fh)
    write_export(convert(doc, team_label=a.team_label, env_label=a.env_label, env_map=env_map), a.out)


if __name__ == "__main__":
    main()
