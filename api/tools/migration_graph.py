"""Deterministic Alembic revision graph validation."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .migration_manifest import MigrationRecord, discover_migrations


@dataclass(frozen=True)
class GraphFinding:
    code: str
    detail: str
    revision: str | None = None

    def to_dict(self) -> dict[str, str | None]:
        return {"code": self.code, "detail": self.detail, "revision": self.revision}


@dataclass(frozen=True)
class GraphReport:
    bases: tuple[str, ...]
    heads: tuple[str, ...]
    findings: tuple[GraphFinding, ...]

    @property
    def ok(self) -> bool:
        return not self.findings and len(self.heads) == 1

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "bases": list(self.bases),
            "heads": list(self.heads),
            "findings": [finding.to_dict() for finding in self.findings],
        }


def validate_revision_graph(records: tuple[MigrationRecord, ...]) -> GraphReport:
    findings: list[GraphFinding] = []
    by_revision: dict[str, MigrationRecord] = {}
    for record in records:
        if record.revision in by_revision:
            findings.append(GraphFinding("duplicate_revision", "revision ID is declared more than once", record.revision))
        by_revision[record.revision] = record

    children: dict[str, set[str]] = {record.revision: set() for record in records}
    for record in records:
        if record.down_revision is None:
            continue
        if record.down_revision == record.revision:
            findings.append(GraphFinding("self_reference", "revision points to itself", record.revision))
        elif record.down_revision not in by_revision:
            findings.append(GraphFinding("missing_parent", "down_revision target does not exist", record.revision))
        else:
            children[record.down_revision].add(record.revision)

    bases = tuple(sorted(record.revision for record in records if record.down_revision is None))
    heads = tuple(sorted(revision for revision, descendants in children.items() if not descendants))
    if len(heads) == 0:
        findings.append(GraphFinding("no_head", "revision graph has no head"))
    elif len(heads) > 1:
        findings.append(GraphFinding("multiple_heads", "revision graph has multiple heads"))

    colors: dict[str, int] = {}

    def visit(revision: str) -> None:
        colors[revision] = 1
        parent = by_revision[revision].down_revision
        if parent in by_revision:
            if colors.get(parent) == 1:
                findings.append(GraphFinding("cycle", "revision graph contains a cycle", revision))
            elif colors.get(parent, 0) == 0:
                visit(parent)
        colors[revision] = 2

    for revision in sorted(by_revision):
        if colors.get(revision, 0) == 0:
            visit(revision)

    reachable: set[str] = set()
    for base in bases:
        stack = [base]
        while stack:
            current = stack.pop()
            if current in reachable:
                continue
            reachable.add(current)
            stack.extend(sorted(children.get(current, ())))
    for revision in sorted(set(by_revision) - reachable):
        findings.append(GraphFinding("disconnected_revision", "revision is not reachable from a base", revision))

    return GraphReport(
        bases=bases,
        heads=heads,
        findings=tuple(sorted(findings, key=lambda finding: (finding.code, finding.revision or ""))),
    )


def inspect_graph(versions_dir: Path) -> GraphReport:
    try:
        return validate_revision_graph(discover_migrations(versions_dir))
    except (OSError, SyntaxError, ValueError) as exc:
        return GraphReport((), (), (GraphFinding("graph_unreadable", str(exc)),))
