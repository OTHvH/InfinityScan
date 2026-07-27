#!/usr/bin/env python3
"""Static workflow security and Dependabot policy checks."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
SHA_REF = re.compile(r"^[0-9a-f]{40}$")


def fail(message: str) -> None:
    raise SystemExit(message)


def main() -> int:
    workflow_paths = sorted(WORKFLOWS.glob("*.yml")) + sorted(WORKFLOWS.glob("*.yaml"))
    if not workflow_paths:
        fail("no GitHub workflows found")
    for path in workflow_paths:
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            fail(f"{path}: invalid YAML: {exc}")
        if not isinstance(data, dict) or "jobs" not in data:
            fail(f"{path}: workflow must contain jobs")
        if "pull_request_target" in data:
            fail(f"{path}: pull_request_target is forbidden")
        permissions = data.get("permissions", {})
        if permissions != "read-all" and permissions.get("contents") != "read":
            fail(f"{path}: top-level permissions must grant contents: read")
        raw = path.read_text(encoding="utf-8")
        if re.search(r"\b(secrets\.[A-Za-z0-9_]+)\b", raw) and "pull_request:" in raw:
            # Secrets are permitted only in workflows that do not execute
            # untrusted fork code. This repository's secret scan uses the
            # read-only GitHub token, not repository secrets.
            if "GITHUB_TOKEN" not in raw or "pull_request_target" in raw:
                fail(f"{path}: unsafe secret reference")
        for job_name, job in data["jobs"].items():
            if not isinstance(job, dict):
                fail(f"{path}: job {job_name} is invalid")
            for step in job.get("steps", []):
                if not isinstance(step, dict) or "uses" not in step:
                    continue
                reference = str(step["uses"]).rsplit("@", 1)[-1]
                if not SHA_REF.fullmatch(reference):
                    fail(f"{path}: {step['uses']} is not pinned to an immutable SHA")
    dependabot = ROOT / ".github" / "dependabot.yml"
    try:
        config = yaml.safe_load(dependabot.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        fail(f"dependabot configuration invalid: {exc}")
    ecosystems = {item.get("package-ecosystem") for item in config.get("updates", [])}
    required = {"npm", "pip", "github-actions", "docker"}
    if not required <= ecosystems:
        fail(f"dependabot ecosystems missing: {sorted(required - ecosystems)}")
    print(f"validated {len(workflow_paths)} workflows and Dependabot configuration")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
