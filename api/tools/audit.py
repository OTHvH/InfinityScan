"""Inspect and prune audit events without exposing event payloads."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from audit_events import record_event_safe, prune_events
from database import _get_engine
from models import AuditEvent, AuditEventOutcome
from settings import get_settings


def status(db: Session) -> dict[str, object]:
    cfg = get_settings()
    cutoff = datetime.now(timezone.utc) - timedelta(days=cfg.audit_retention_days)
    total = db.scalar(select(func.count()).select_from(AuditEvent)) or 0
    eligible = db.scalar(
        select(func.count()).select_from(AuditEvent).where(AuditEvent.created_at < cutoff)
    ) or 0
    oldest = db.scalar(select(func.min(AuditEvent.created_at)))
    return {
        "total": total,
        "eligible_for_prune": eligible,
        "oldest_created_at": oldest.isoformat() if oldest else None,
        "retention_days": cfg.audit_retention_days,
        "batch_size": cfg.audit_prune_batch_size,
    }


def prune(db: Session, *, dry_run: bool) -> dict[str, object]:
    cfg = get_settings()
    removed = prune_events(
        db,
        retention_days=cfg.audit_retention_days,
        batch_size=cfg.audit_prune_batch_size,
        dry_run=dry_run,
    )
    if not dry_run:
        event = record_event_safe(
            db,
            event_type="audit.pruned",
            outcome=AuditEventOutcome.success,
            metadata={
                "retention_days": cfg.audit_retention_days,
                "removed_count": removed,
                "batch_size": cfg.audit_prune_batch_size,
                "dry_run": False,
            },
        )
        if event is not None:
            db.commit()
    return {"dry_run": dry_run, "removed": removed, "batch_size": cfg.audit_prune_batch_size}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("status")
    prune_parser = subparsers.add_parser("prune")
    prune_parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    engine = _get_engine()
    if engine is None:
        parser.error("DATABASE_URL is not configured")
    with sessionmaker(bind=engine, autoflush=False, autocommit=False)() as db:
        result = status(db) if args.command == "status" else prune(db, dry_run=args.dry_run)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
