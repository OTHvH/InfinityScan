from __future__ import annotations

import importlib.util
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def load_script(name: str):
    path = ROOT / "scripts" / name
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_staging_workflow_is_dispatch_only_and_protected() -> None:
    checker = load_script("check-workflows.py")
    workflow = ROOT / ".github/workflows/deploy-staging.yml"
    checker.validate_workflow(workflow)
    data = checker.load_yaml(workflow)
    assert checker._trigger_names(data["on"], workflow) == {"workflow_dispatch"}
    assert data["concurrency"]["group"] == "staging-deployment"
    assert data["jobs"]["staging-preflight"]["environment"] == "staging"
    assert data["jobs"]["deploy-staging"]["needs"] == "staging-preflight"
    assert data["jobs"]["fetch-release-evidence"]["permissions"] == {
        "contents": "read",
        "actions": "read",
    }


def test_host_wrapper_rejects_arbitrary_actions() -> None:
    result = subprocess.run(
        [str(ROOT / "scripts/run-on-deployment-host.sh"), "arbitrary-command"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 2


def test_staging_fixture_refuses_production_environment() -> None:
    result = subprocess.run(
        [str(ROOT / "api/.venv/bin/python"), "-m", "tools.staging_fixture", "seed", "--confirm-staging"],
        cwd=ROOT / "api",
        env={**os.environ, "APP_ENV": "production", "DEPLOYMENT_ENVIRONMENT": "production"},
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode != 0
    assert "DEPLOYMENT_ENVIRONMENT=staging" in result.stderr


def test_fake_staging_rollback_rehearsal_passes() -> None:
    result = subprocess.run(
        [str(ROOT / "scripts/rehearse-staging-rollback.sh"), "--fake-host"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
        env={**os.environ, "PREFLIGHT_FAKE_HOST": "true"},
    )
    assert result.returncode == 0, result.stderr
    assert "auto-rolled-back" in result.stdout
