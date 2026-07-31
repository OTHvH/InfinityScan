"""Tests for the repository workflow and container policy."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "check-workflows.py"
FIXTURES = Path(__file__).with_name("fixtures")
SPEC = importlib.util.spec_from_file_location("check_workflows", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
CHECKER = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = CHECKER
SPEC.loader.exec_module(CHECKER)


def test_valid_repository_and_yaml_12_on_key() -> None:
    root = FIXTURES / "valid-repository"
    workflow = root / ".github" / "workflows" / "ci.yml"

    assert CHECKER.load_yaml(workflow)["on"] == {"pull_request": None}
    assert CHECKER.validate_repository(root) == (1, 2)


@pytest.mark.parametrize(
    ("fixture", "message"),
    [
        ("workflow-pull-request-target.yml", "pull_request_target is forbidden"),
        ("workflow-write-all.yml", "write-all permissions are forbidden"),
        ("workflow-malformed-permissions.yml", "permissions must be a mapping"),
        ("workflow-job-escalation.yml", "job-level privilege escalation"),
        ("workflow-mutable-step.yml", "not pinned to an immutable SHA"),
        ("workflow-mutable-reusable.yml", "not pinned to an immutable SHA"),
        ("workflow-pr-secret.yml", "unsafe secret reference"),
        ("workflow-mutable-service.yml", "not digest-pinned"),
    ],
)
def test_invalid_workflow_fixtures(fixture: str, message: str) -> None:
    with pytest.raises(CHECKER.PolicyError, match=message):
        CHECKER.validate_workflow(FIXTURES / fixture)


def test_dependabot_requires_docker_infra_directory() -> None:
    with pytest.raises(CHECKER.PolicyError, match=r"\('docker', '/infra'\)"):
        CHECKER.validate_dependabot(FIXTURES / "dependabot-missing-infra.yml")


def test_mutable_dockerfile_base_is_rejected() -> None:
    with pytest.raises(CHECKER.PolicyError, match="not digest-pinned"):
        CHECKER.validate_dockerfile(FIXTURES / "mutable-dockerfile.fixture")


def test_mutable_compose_image_is_rejected() -> None:
    with pytest.raises(CHECKER.PolicyError, match="not digest-pinned"):
        CHECKER.validate_compose(FIXTURES / "mutable-compose.fixture")
