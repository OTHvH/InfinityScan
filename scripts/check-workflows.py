#!/usr/bin/env python3
"""Enforce workflow, dependency-update, and container pinning policy."""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import yaml
from yaml.constructor import ConstructorError
from yaml.nodes import MappingNode

ROOT = Path(__file__).resolve().parents[1]
SHA_REF = re.compile(r"^[0-9a-f]{40}$")
SHA256_REF = re.compile(r"^[0-9a-f]{64}$")
COMPOSE_IMAGE_VAR = re.compile(r"^\$\{(?:API|WEB|CADDY)_IMAGE(?::\?[^}]*)?\}$")
YAML12_BOOL = re.compile(r"^(?:true|false)$", re.IGNORECASE)
COMPOSE_NAME = re.compile(r"^(?:docker-)?compose(?:\.[^.]+)?\.ya?ml$")
DOCKER_FROM = re.compile(
    r"^\s*FROM(?:\s+--platform=\S+)?\s+(\S+)(?:\s+AS\s+(\S+))?\s*$",
    re.IGNORECASE,
)
SAFE_PR_SECRET = re.compile(r"\$\{\{\s*secrets\.GITHUB_TOKEN\s*\}\}")
REQUIRED_DEPENDABOT = {
    ("pip", "/api"),
    ("npm", "/web"),
    ("github-actions", "/"),
    ("docker", "/api"),
    ("docker", "/web"),
    ("docker", "/infra"),
}
PUBLISH_WORKFLOW = "publish-images.yml"
PUBLISH_JOB_PERMISSIONS = {
    "contents": "read",
    "packages": "write",
    "id-token": "write",
    "attestations": "write",
}
PUBLISH_VERIFY_PERMISSIONS = {"contents": "read", "actions": "read"}
STAGING_FETCH_PERMISSIONS = {"contents": "read", "actions": "read"}


class PolicyError(ValueError):
    """A repository policy violation."""


class Yaml12SafeLoader(yaml.SafeLoader):
    """Safe loader with YAML 1.2 booleans and duplicate-key rejection."""


Yaml12SafeLoader.yaml_implicit_resolvers = {
    key: [
        (tag, pattern)
        for tag, pattern in resolvers
        if tag != "tag:yaml.org,2002:bool"
    ]
    for key, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
Yaml12SafeLoader.add_implicit_resolver(
    "tag:yaml.org,2002:bool",
    YAML12_BOOL,
    list("tTfF"),
)


def _construct_unique_mapping(
    loader: Yaml12SafeLoader,
    node: MappingNode,
    deep: bool = False,
) -> dict[Any, Any]:
    loader.flatten_mapping(node)
    result: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in result
        except TypeError as exc:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "found an unhashable key",
                key_node.start_mark,
            ) from exc
        if duplicate:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"found duplicate key {key!r}",
                key_node.start_mark,
            )
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


Yaml12SafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def load_yaml(path: Path) -> Any:
    try:
        return yaml.load(path.read_text(encoding="utf-8"), Loader=Yaml12SafeLoader)
    except (OSError, yaml.YAMLError) as exc:
        raise PolicyError(f"{path}: invalid YAML: {exc}") from exc


def _trigger_names(value: Any, path: Path) -> set[str]:
    if isinstance(value, str):
        return {value}
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return set(value)
    if isinstance(value, Mapping) and all(isinstance(item, str) for item in value):
        return set(value)
    raise PolicyError(f"{path}: on must be an event name, list, or mapping")


def _iter_strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for key, item in value.items():
            yield from _iter_strings(key)
            yield from _iter_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _iter_strings(item)


def _validate_action_use(value: Any, location: str) -> None:
    if not isinstance(value, str) or not value:
        raise PolicyError(f"{location}: uses must be a non-empty string")
    if value.startswith("./"):
        return
    if value.startswith("docker://"):
        _validate_image(value.removeprefix("docker://"), location)
        return
    if value.count("@") != 1:
        raise PolicyError(f"{location}: {value!r} is not pinned to an immutable SHA")
    action, reference = value.rsplit("@", 1)
    if not action or not SHA_REF.fullmatch(reference):
        raise PolicyError(f"{location}: {value!r} is not pinned to an immutable SHA")


def _validate_image(value: Any, location: str) -> None:
    if not isinstance(value, str) or any(character.isspace() for character in value):
        raise PolicyError(f"{location}: container image must be a static string")
    if "${" in value or value.count("@sha256:") != 1:
        raise PolicyError(f"{location}: container image {value!r} is not digest-pinned")
    name, digest = value.rsplit("@sha256:", 1)
    if not name or not SHA256_REF.fullmatch(digest):
        raise PolicyError(f"{location}: container image {value!r} is not digest-pinned")


def _validate_top_permissions(value: Any, path: Path) -> None:
    if value == "write-all":
        raise PolicyError(f"{path}: write-all permissions are forbidden")
    if not isinstance(value, Mapping):
        raise PolicyError(f"{path}: top-level permissions must be a mapping")
    if path.name == PUBLISH_WORKFLOW:
        if dict(value) != {"contents": "read"}:
            raise PolicyError(
                f"{path}: publication workflow top-level permissions must be exactly contents: read"
            )
        return
    if dict(value) != {"contents": "read"}:
        raise PolicyError(f"{path}: top-level permissions must be exactly contents: read")


def _validate_job_permissions(
    value: Any,
    location: str,
    *,
    codeql: bool,
    publication_job: bool = False,
    publication_verify: bool = False,
    staging_fetch: bool = False,
    staging_artifact_read: bool = False,
) -> None:
    if value is None:
        return
    if value == "write-all":
        raise PolicyError(f"{location}: write-all permissions are forbidden")
    if not isinstance(value, Mapping):
        raise PolicyError(f"{location}: job permissions must be a mapping")
    if publication_job and dict(value) != PUBLISH_JOB_PERMISSIONS:
        raise PolicyError(f"{location}: publication permissions are broader or narrower than policy")
    if publication_verify and dict(value) != PUBLISH_VERIFY_PERMISSIONS:
        raise PolicyError(f"{location}: verification permissions are broader or narrower than policy")
    if staging_fetch and dict(value) != STAGING_FETCH_PERMISSIONS:
        raise PolicyError(f"{location}: staging evidence fetch permissions are broader or narrower than policy")
    for scope, level in value.items():
        if (
            not isinstance(scope, str)
            or not isinstance(level, str)
            or level not in {"read", "write", "none"}
        ):
            raise PolicyError(f"{location}: malformed permission {scope!r}: {level!r}")
        if level == "none" or (scope == "contents" and level == "read"):
            continue
        if (staging_fetch or staging_artifact_read) and scope == "actions" and level == "read":
            continue
        if codeql and scope == "security-events" and level == "write":
            continue
        if publication_job or publication_verify:
            continue
        raise PolicyError(f"{location}: job-level privilege escalation {scope}: {level}")


def _validate_workflow_containers(job: Mapping[Any, Any], location: str) -> None:
    services = job.get("services", {})
    if not isinstance(services, Mapping):
        raise PolicyError(f"{location}: services must be a mapping")
    for service_name, service in services.items():
        if not isinstance(service, Mapping) or "image" not in service:
            raise PolicyError(f"{location}: service {service_name!r} must define an image")
        _validate_image(service["image"], f"{location}: service {service_name}")

    container = job.get("container")
    if container is None:
        return
    image = container.get("image") if isinstance(container, Mapping) else container
    _validate_image(image, f"{location}: job container")


def validate_workflow(path: Path) -> None:
    data = load_yaml(path)
    if not isinstance(data, Mapping):
        raise PolicyError(f"{path}: workflow must be a mapping")
    if "on" not in data:
        raise PolicyError(f"{path}: workflow must define on")
    triggers = _trigger_names(data["on"], path)
    if "pull_request_target" in triggers:
        raise PolicyError(f"{path}: pull_request_target is forbidden")
    _validate_top_permissions(data.get("permissions"), path)
    publication_workflow = path.name == PUBLISH_WORKFLOW
    staging_workflow = path.name == "deploy-staging.yml"
    if publication_workflow and "workflow_dispatch" not in triggers:
        raise PolicyError(f"{path}: publication workflow must support workflow_dispatch")
    if publication_workflow and ({"pull_request", "pull_request_target"} & triggers):
        raise PolicyError(f"{path}: publication workflow must not run for pull requests")
    if staging_workflow and triggers != {"workflow_dispatch"}:
        raise PolicyError(f"{path}: staging deployment must use workflow_dispatch only")
    if staging_workflow and "staging" not in data.get("concurrency", {}).get("group", ""):
        raise PolicyError(f"{path}: staging concurrency group is required")

    jobs = data.get("jobs")
    if not isinstance(jobs, Mapping) or not jobs:
        raise PolicyError(f"{path}: workflow must contain a jobs mapping")

    pull_request = "pull_request" in triggers
    if pull_request:
        for string in _iter_strings(data):
            remaining = SAFE_PR_SECRET.sub("", string)
            if re.search(r"\bsecrets\s*(?:\.|\[)", remaining):
                raise PolicyError(f"{path}: unsafe secret reference in pull_request workflow")

    for job_name, job in jobs.items():
        location = f"{path}: job {job_name}"
        if not isinstance(job, Mapping):
            raise PolicyError(f"{location} is not a mapping")
        if publication_workflow and job_name == "publish":
            if job.get("environment") != "image-publication":
                raise PolicyError(f"{location}: protected publication environment is required")
            if job.get("needs") != "verify":
                raise PolicyError(f"{location}: publication must depend on the verification job")
        if staging_workflow and job_name in {
            "authorize", "fetch-release-evidence", "verify-release-evidence", "staging-preflight",
            "deploy-staging", "smoke-staging", "rollback-rehearsal", "publish-staging-evidence",
        } and job.get("environment") not in (None, "staging"):
            raise PolicyError(f"{location}: staging jobs may only use the staging environment")
        if pull_request and job.get("secrets") == "inherit":
            raise PolicyError(f"{location}: secrets: inherit is unsafe for pull requests")

        reusable_use = job.get("uses")
        if reusable_use is not None:
            if "steps" in job:
                raise PolicyError(f"{location}: reusable workflow job cannot define steps")
            _validate_action_use(reusable_use, f"{location}: reusable workflow")

        steps = job.get("steps", [])
        if not isinstance(steps, list):
            raise PolicyError(f"{location}: steps must be a list")
        for index, step in enumerate(steps, start=1):
            if not isinstance(step, Mapping):
                raise PolicyError(f"{location}: step {index} is not a mapping")
            if "uses" in step:
                _validate_action_use(step["uses"], f"{location}: step {index}")

        codeql = any(
            isinstance(step, Mapping)
            and str(step.get("uses", "")).startswith("github/codeql-action/analyze@")
            for step in steps
        )
        _validate_job_permissions(
            job.get("permissions"),
            location,
            codeql=codeql,
            publication_job=publication_workflow and job_name == "publish",
            publication_verify=publication_workflow and job_name == "verify",
            staging_fetch=staging_workflow and job_name == "fetch-release-evidence",
            staging_artifact_read=staging_workflow and job_name == "verify-release-evidence",
        )
        _validate_workflow_containers(job, location)


def validate_dependabot(path: Path) -> None:
    data = load_yaml(path)
    if not isinstance(data, Mapping) or data.get("version") != 2:
        raise PolicyError(f"{path}: Dependabot configuration must use version 2")
    updates = data.get("updates")
    if not isinstance(updates, list):
        raise PolicyError(f"{path}: Dependabot updates must be a list")

    configured: set[tuple[str, str]] = set()
    for index, update in enumerate(updates, start=1):
        if not isinstance(update, Mapping):
            raise PolicyError(f"{path}: Dependabot update {index} is not a mapping")
        ecosystem = update.get("package-ecosystem")
        directory = update.get("directory")
        directories = update.get("directories")
        if not isinstance(ecosystem, str):
            raise PolicyError(f"{path}: Dependabot update {index} has no ecosystem")
        if directory is not None and directories is not None:
            raise PolicyError(f"{path}: Dependabot update {index} mixes directory forms")
        if isinstance(directory, str):
            configured.add((ecosystem, directory))
        elif isinstance(directories, list) and all(
            isinstance(item, str) for item in directories
        ):
            configured.update((ecosystem, item) for item in directories)
        else:
            raise PolicyError(f"{path}: Dependabot update {index} has no directory")

    missing = REQUIRED_DEPENDABOT - configured
    if missing:
        raise PolicyError(f"{path}: required Dependabot entries missing: {sorted(missing)}")


def validate_dockerfile(path: Path) -> None:
    aliases: set[str] = set()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise PolicyError(f"{path}: cannot read Dockerfile: {exc}") from exc
    for line_number, line in enumerate(lines, start=1):
        if not line.lstrip().upper().startswith("FROM "):
            continue
        match = DOCKER_FROM.fullmatch(line)
        if match is None:
            raise PolicyError(f"{path}:{line_number}: malformed FROM instruction")
        image, alias = match.groups()
        if image.lower() != "scratch" and image.lower() not in aliases:
            _validate_image(image, f"{path}:{line_number}")
        if alias:
            aliases.add(alias.lower())


def validate_compose(path: Path) -> None:
    data = load_yaml(path)
    if not isinstance(data, Mapping) or not isinstance(data.get("services"), Mapping):
        raise PolicyError(f"{path}: Compose file must contain a services mapping")
    for service_name, service in data["services"].items():
        if not isinstance(service, Mapping):
            raise PolicyError(f"{path}: Compose service {service_name!r} is invalid")
        if "image" in service:
            image = service["image"]
            if (
                path.name == "docker-compose.production.yml"
                and isinstance(image, str)
                and COMPOSE_IMAGE_VAR.fullmatch(image)
            ):
                continue
            _validate_image(image, f"{path}: service {service_name}")


def validate_repository(root: Path = ROOT) -> tuple[int, int]:
    workflows = root / ".github" / "workflows"
    workflow_paths = sorted(workflows.glob("*.yml")) + sorted(workflows.glob("*.yaml"))
    if not workflow_paths:
        raise PolicyError(f"{workflows}: no GitHub workflows found")
    for path in workflow_paths:
        validate_workflow(path)

    validate_dependabot(root / ".github" / "dependabot.yml")

    def is_policy_fixture(path: Path) -> bool:
        return path.relative_to(root).parts[:3] == ("scripts", "tests", "fixtures")

    dockerfiles = sorted(
        path
        for path in root.rglob("Dockerfile*")
        if path.is_file()
        and not is_policy_fixture(path)
        and (path.name == "Dockerfile" or path.name.startswith("Dockerfile."))
    )
    compose_files = sorted(
        path
        for path in root.rglob("*.y*ml")
        if not is_policy_fixture(path) and COMPOSE_NAME.fullmatch(path.name)
    )
    for path in dockerfiles:
        validate_dockerfile(path)
    for path in compose_files:
        validate_compose(path)
    return len(workflow_paths), len(dockerfiles) + len(compose_files)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", nargs="?", type=Path, default=ROOT)
    args = parser.parse_args(argv)
    try:
        workflow_count, container_file_count = validate_repository(args.root.resolve())
    except PolicyError as exc:
        print(exc, file=sys.stderr)
        return 1
    print(
        f"validated {workflow_count} workflows, Dependabot configuration, "
        f"and {container_file_count} container files"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
