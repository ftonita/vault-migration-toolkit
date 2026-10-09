"""The files in examples/ are documentation: keep them loadable and consistent with the docs."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from vault_migration.cli import main
from vault_migration.rules import Rules, plan
from vault_migration.sources import load_legacy

EX = Path(__file__).resolve().parent.parent / "examples"
ADAPTERS = EX / "adapters"
SAMPLES = ADAPTERS / "samples"


def test_json_and_csv_samples_are_the_same_export():
    assert load_legacy(EX / "legacy.json") == load_legacy(EX / "legacy.csv")


def test_sample_plan_matches_the_docs():
    items, unmapped = plan(load_legacy(EX / "legacy.json"), Rules.from_file(str(EX / "rules.yml")))
    targets = {i.secret_id: str(i.target) for i in items}
    assert len(items) == 8 and {u.secret_id for u in unmapped} == {"LEG-0009", "LEG-0010"}
    assert targets["LEG-0004"] == "kv-payments/prod/ledger/db_password"  # team + env aliases
    assert targets["LEG-0006"] == "kv-platform/stage/reports/s3_secret_key"  # app_owners fallback


def test_sample_values_are_synthetic():
    values = [r["value"] for r in json.loads((EX / "legacy.json").read_text(encoding="utf-8"))]
    assert all(v.startswith("SYNTHETIC-") or v == "changeme" for v in values)


def _run(script: str, *args: str, tmp_path: Path) -> Path:
    out = tmp_path / f"{script}.json"
    subprocess.run([sys.executable, str(ADAPTERS / script), *args, "--out", str(out)], check=True)
    assert out.stat().st_mode & 0o077 == 0  # plaintext export is created 0600
    return out


@pytest.mark.parametrize(
    ("script", "args", "expected"),
    [
        (
            "env_files.py",
            [str(SAMPLES / "env-tree"), "--exclude", "*_HOST"],
            {
                "kv-payments/prod/orders/db_password",
                "kv-payments/prod/orders/stripe_api_key",
                "kv-payments/stage/orders/db_password",
                "kv-platform/prod/gateway/oauth_client_secret",
            },
        ),
        (
            "nested_json.py",
            [str(SAMPLES / "nested.json")],
            {
                "kv-payments/prod/orders/db_password",
                "kv-payments/prod/orders/stripe_api_key",
                "kv-payments/stage/orders/db_password",
                "kv-scoring/prod/scorer/jwt_secret",
            },
        ),
        (
            "k8s_secrets.py",
            [str(SAMPLES / "k8s-secrets.json"), "--env-map", "payments-prod=prod"],
            {
                "kv-payments/prod/orders/password",
                "kv-payments/prod/orders/username",
                "kv-scoring/stage/scorer/jwt_secret",
            },
        ),
    ],
)
def test_adapters_produce_a_loadable_export(script, args, expected, tmp_path):
    out = _run(script, *args, tmp_path=tmp_path)
    items, unmapped = plan(load_legacy(out), Rules.from_file(str(EX / "rules.yml")))
    assert not unmapped and {str(i.target) for i in items} == expected


def test_adapter_output_runs_through_the_cli(tmp_path, capsys):
    out = _run("nested_json.py", str(SAMPLES / "nested.json"), tmp_path=tmp_path)
    common = ["--source", str(out), "--rules", str(EX / "rules.yml"), "--state", str(tmp_path / "st")]
    backend = ["--backend", f"fake:{tmp_path / 'fake.json'}"]
    assert main(["apply", *common, *backend, "--execute", "--create-mounts"]) == 0
    assert main(["verify", *common, *backend]) == 0
    assert "verified=4 mismatched=0 missing=0" in capsys.readouterr().out
