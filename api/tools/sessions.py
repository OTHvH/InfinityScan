"""Bounded refresh-session inspection and maintenance.

This tool never prints token material. Mutating commands refuse to run when
the replacement graph or foreign-key enforcement is unsafe.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.orm import Session, aliased

from database import _get_engine
from models import RefreshSession, User
from settings import Settings, get_settings


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class SessionReport:
    checked_at: str
    cutoff: str
    retention_days: int
    batch_size: int
    inspected: int = 0
    active: int = 0
    expired: int = 0
    revoked: int = 0
    eligible_for_cleanup: int = 0
    missing_users: int = 0
    missing_replacements: int = 0
    impossible_replacements: int = 0
    replacement_cycles: int = 0
    sessions_in_cycles: int = 0
    foreign_keys_enforced: bool = True
    blocked: bool = False
    details: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass
class MaintenanceResult:
    operation: str
    dry_run: bool
    inspected: int
    active: int
    expired: int
    revoked: int
    eligible_for_cleanup: int
    removed: int = 0
    revoked_now: int = 0
    batches: int = 0
    blocked: bool = False
    remaining: int = 0
    details: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _foreign_keys_enforced(db: Session) -> bool:
    dialect = db.get_bind().dialect.name
    if dialect == "sqlite":
        return bool(db.execute(text("PRAGMA foreign_keys")).scalar())
    return dialect == "postgresql"


def _replacement_graph(db: Session) -> dict[uuid.UUID, uuid.UUID]:
    rows = db.execute(
        select(RefreshSession.id, RefreshSession.replaced_by_session_id).where(
            RefreshSession.replaced_by_session_id.is_not(None)
        )
    ).all()
    return {source: target for source, target in rows}


def _cycles(graph: dict[uuid.UUID, uuid.UUID]) -> tuple[int, int]:
    colors: dict[uuid.UUID, int] = {}
    cycle_sets: set[frozenset[uuid.UUID]] = set()

    def visit(node: uuid.UUID, path: list[uuid.UUID], positions: dict[uuid.UUID, int]) -> None:
        colors[node] = 1
        positions[node] = len(path)
        path.append(node)
        target = graph.get(node)
        if target is not None:
            if colors.get(target) == 1 and target in positions:
                cycle_sets.add(frozenset(path[positions[target] :]))
            elif colors.get(target, 0) == 0:
                visit(target, path, positions)
        path.pop()
        positions.pop(node, None)
        colors[node] = 2

    for node in graph:
        if colors.get(node, 0) == 0:
            visit(node, [], {})
    participants = set().union(*cycle_sets) if cycle_sets else set()
    return len(cycle_sets), len(participants)


def inspect_sessions(
    db: Session,
    *,
    settings: Settings | None = None,
    checked_at: datetime | None = None,
) -> SessionReport:
    cfg = settings or get_settings()
    snapshot = _utc(checked_at) or _now()
    cutoff = snapshot - timedelta(days=cfg.session_retention_days)
    report = SessionReport(
        checked_at=snapshot.isoformat(),
        cutoff=cutoff.isoformat(),
        retention_days=cfg.session_retention_days,
        batch_size=cfg.session_maintenance_batch_size,
        foreign_keys_enforced=_foreign_keys_enforced(db),
    )

    report.inspected = db.scalar(select(func.count()).select_from(RefreshSession)) or 0
    report.active = db.scalar(
        select(func.count()).select_from(RefreshSession).where(
            RefreshSession.revoked_at.is_(None), RefreshSession.expires_at >= snapshot
        )
    ) or 0
    report.expired = db.scalar(
        select(func.count()).select_from(RefreshSession).where(
            RefreshSession.revoked_at.is_(None), RefreshSession.expires_at < snapshot
        )
    ) or 0
    report.revoked = db.scalar(
        select(func.count()).select_from(RefreshSession).where(
            RefreshSession.revoked_at.is_not(None)
        )
    ) or 0
    report.eligible_for_cleanup = db.scalar(
        select(func.count()).select_from(RefreshSession).where(
            (RefreshSession.revoked_at.is_not(None) & (RefreshSession.revoked_at < cutoff))
            | (RefreshSession.expires_at < cutoff)
        )
    ) or 0

    missing_user = aliased(User)
    report.missing_users = db.scalar(
        select(func.count()).select_from(RefreshSession).outerjoin(
            missing_user, RefreshSession.user_id == missing_user.id
        ).where(missing_user.id.is_(None))
    ) or 0

    target = aliased(RefreshSession)
    report.missing_replacements = db.scalar(
        select(func.count()).select_from(RefreshSession).outerjoin(
            target, RefreshSession.replaced_by_session_id == target.id
        ).where(
            RefreshSession.replaced_by_session_id.is_not(None), target.id.is_(None)
        )
    ) or 0

    report.impossible_replacements = db.scalar(
        select(func.count()).select_from(RefreshSession).join(
            target, RefreshSession.replaced_by_session_id == target.id
        ).where(
            RefreshSession.replaced_by_session_id.is_not(None),
            (
                (RefreshSession.revoked_at.is_(None))
                | (RefreshSession.last_used_at.is_(None))
                | (RefreshSession.user_id != target.user_id)
                | (RefreshSession.family_id != target.family_id)
                | (target.created_at <= RefreshSession.created_at)
                | (RefreshSession.id == target.id)
            ),
        )
    ) or 0
    report.replacement_cycles, report.sessions_in_cycles = _cycles(_replacement_graph(db))
    if not report.foreign_keys_enforced:
        report.blocked = True
        report.details.append("foreign-key enforcement is disabled")
    if report.missing_users or report.missing_replacements or report.impossible_replacements:
        report.blocked = True
        report.details.append("replacement or ownership relationships are inconsistent")
    if report.replacement_cycles:
        report.blocked = True
        report.details.append("replacement graph contains a cycle")
    return report


def _candidate_ids(db: Session, cutoff: datetime) -> list[uuid.UUID]:
    rows = db.execute(
        select(RefreshSession.id)
        .where(
            (RefreshSession.revoked_at.is_not(None) & (RefreshSession.revoked_at < cutoff))
            | (RefreshSession.expires_at < cutoff)
        )
        .order_by(
            func.coalesce(RefreshSession.revoked_at, RefreshSession.expires_at),
            RefreshSession.created_at,
            RefreshSession.id,
        )
    ).all()
    return [row[0] for row in rows]


def _safe_batch_ids(db: Session, candidate_ids: list[uuid.UUID], limit: int) -> list[uuid.UUID]:
    """Return a source-before-target batch with no surviving inbound references."""
    if not candidate_ids:
        return []
    candidates = set(candidate_ids)
    predecessors: dict[uuid.UUID, set[uuid.UUID]] = {sid: set() for sid in candidates}
    rows = db.execute(
        select(RefreshSession.id, RefreshSession.replaced_by_session_id).where(
            RefreshSession.replaced_by_session_id.is_not(None)
        )
    ).all()
    for source, target in rows:
        if target in predecessors:
            predecessors[target].add(source)
    surviving_inbound = {
        target for target, sources in predecessors.items() if any(source not in candidates for source in sources)
    }
    selected: list[uuid.UUID] = []
    remaining = set(candidates)
    while remaining and len(selected) < limit:
        ready = sorted(
            sid
            for sid in remaining
            if sid not in surviving_inbound
            and not any(source in remaining for source in predecessors.get(sid, set()))
        )
        if not ready:
            break
        for sid in ready:
            if len(selected) >= limit:
                break
            selected.append(sid)
            remaining.remove(sid)
    return selected


def revoke_expired(
    db: Session,
    *,
    settings: Settings | None = None,
    dry_run: bool = False,
    checked_at: datetime | None = None,
) -> MaintenanceResult:
    cfg = settings or get_settings()
    snapshot = _utc(checked_at) or _now()
    report = inspect_sessions(db, settings=cfg, checked_at=snapshot)
    candidates = db.scalar(
        select(func.count()).select_from(RefreshSession).where(
            RefreshSession.revoked_at.is_(None), RefreshSession.expires_at < snapshot
        )
    ) or 0
    result = MaintenanceResult(
        operation="revoke-expired",
        dry_run=dry_run,
        inspected=report.inspected,
        active=report.active,
        expired=report.expired,
        revoked=report.revoked,
        eligible_for_cleanup=report.eligible_for_cleanup,
        blocked=report.blocked,
        details=list(report.details),
    )
    if dry_run or report.blocked:
        result.remaining = candidates
        result.revoked_now = 0
        result.eligible_for_cleanup = candidates
        return result
    remaining = candidates
    while remaining:
        ids = list(
            db.scalars(
                select(RefreshSession.id)
                .where(
                    RefreshSession.revoked_at.is_(None),
                    RefreshSession.expires_at < snapshot,
                )
                .order_by(RefreshSession.expires_at, RefreshSession.created_at, RefreshSession.id)
                .limit(cfg.session_maintenance_batch_size)
                .with_for_update(skip_locked=True)
            ).all()
        )
        if not ids:
            break
        changed = db.execute(
            update(RefreshSession)
            .where(RefreshSession.id.in_(ids), RefreshSession.revoked_at.is_(None))
            .values(revoked_at=snapshot)
        ).rowcount or 0
        db.commit()
        result.revoked_now += changed
        result.batches += 1
        remaining -= len(ids)
    result.remaining = max(0, candidates - result.revoked_now)
    result.eligible_for_cleanup = result.revoked_now
    return result


def cleanup_sessions(
    db: Session,
    *,
    settings: Settings | None = None,
    dry_run: bool = False,
    checked_at: datetime | None = None,
) -> MaintenanceResult:
    cfg = settings or get_settings()
    snapshot = _utc(checked_at) or _now()
    report = inspect_sessions(db, settings=cfg, checked_at=snapshot)
    candidates = _candidate_ids(db, snapshot - timedelta(days=cfg.session_retention_days))
    result = MaintenanceResult(
        operation="cleanup",
        dry_run=dry_run,
        inspected=report.inspected,
        active=report.active,
        expired=report.expired,
        revoked=report.revoked,
        eligible_for_cleanup=len(candidates),
        blocked=report.blocked,
        details=list(report.details),
    )
    if dry_run or report.blocked:
        result.remaining = len(candidates)
        result.eligible_for_cleanup = 0 if report.blocked else len(candidates)
        return result

    remaining = list(candidates)
    while remaining:
        batch = _safe_batch_ids(db, remaining, cfg.session_maintenance_batch_size)
        if not batch:
            result.details.append("eligible rows are retained because their replacement evidence is still referenced")
            break
        locked = list(
            db.scalars(
                select(RefreshSession.id)
                .where(RefreshSession.id.in_(batch))
                .with_for_update(skip_locked=True)
            ).all()
        )
        if not locked:
            break
        deleted_count = db.execute(delete(RefreshSession).where(RefreshSession.id.in_(locked))).rowcount or 0
        db.commit()
        deleted_set = set(locked)
        remaining = [sid for sid in remaining if sid not in deleted_set]
        result.removed += deleted_count
        result.batches += 1
    result.remaining = len(remaining)
    return result


def _open_session() -> tuple[Session, Settings]:
    from sqlalchemy.orm import sessionmaker

    engine = _get_engine()
    if engine is None:
        raise RuntimeError("DATABASE_URL is not configured")
    settings = get_settings()
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)(), settings


def _print(value: dict[str, object]) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Refresh-session maintenance")
    sub = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (
        ("status", "Inspect session lifecycle and replacement integrity"),
        ("cleanup", "Remove safely retained terminal sessions"),
        ("revoke-expired", "Mark expired sessions revoked"),
    ):
        command = sub.add_parser(name, help=help_text)
        command.add_argument("--dry-run", action="store_true", help="Report without writes")
        command.add_argument("--json", action="store_true", help="Emit JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        db, settings = _open_session()
    except (RuntimeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    try:
        if args.command == "status":
            report = inspect_sessions(db, settings=settings)
            value = report.to_dict()
            if args.json:
                _print(value)
            else:
                print(
                    f"inspected={report.inspected} active={report.active} "
                    f"expired={report.expired} revoked={report.revoked} "
                    f"eligible={report.eligible_for_cleanup}"
                )
            return int(report.blocked)
        if args.command == "cleanup":
            result = cleanup_sessions(db, settings=settings, dry_run=args.dry_run)
        else:
            result = revoke_expired(db, settings=settings, dry_run=args.dry_run)
        if args.json:
            _print(result.to_dict())
        else:
            print(
                f"inspected={result.inspected} active={result.active} "
                f"expired={result.expired} revoked={result.revoked} "
                f"eligible={result.eligible_for_cleanup} removed={result.removed} "
                f"revoked_now={result.revoked_now}"
            )
        return int(result.blocked)
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
