"""Valid state transitions for the durable import state machine."""

from __future__ import annotations

from models import ImportJobItemStatus, ImportJobStatus


ACTIVE_JOB_STATES = frozenset(
    {
        ImportJobStatus.pending,
        ImportJobStatus.scanning,
        ImportJobStatus.uploading,
        ImportJobStatus.verifying,
    }
)
TERMINAL_JOB_STATES = frozenset(
    {
        ImportJobStatus.succeeded,
        ImportJobStatus.partial,
        ImportJobStatus.failed,
        ImportJobStatus.cancelled,
    }
)

JOB_TRANSITIONS: dict[ImportJobStatus, frozenset[ImportJobStatus]] = {
    ImportJobStatus.pending: frozenset(
        {ImportJobStatus.scanning, ImportJobStatus.failed, ImportJobStatus.cancelled}
    ),
    ImportJobStatus.scanning: frozenset(
        {
            ImportJobStatus.uploading,
            ImportJobStatus.verifying,
            ImportJobStatus.failed,
            ImportJobStatus.cancelled,
        }
    ),
    ImportJobStatus.uploading: frozenset(
        {
            ImportJobStatus.uploading,
            ImportJobStatus.verifying,
            ImportJobStatus.failed,
            ImportJobStatus.cancelled,
        }
    ),
    ImportJobStatus.verifying: frozenset(
        {
            ImportJobStatus.uploading,
            ImportJobStatus.succeeded,
            ImportJobStatus.partial,
            ImportJobStatus.failed,
            ImportJobStatus.cancelled,
        }
    ),
    ImportJobStatus.succeeded: frozenset(),
    ImportJobStatus.partial: frozenset(),
    ImportJobStatus.failed: frozenset(),
    ImportJobStatus.cancelled: frozenset(),
}

# A successful or reused item can return to uploading only while its active job
# is repairing storage drift. Quarantined items are immutable evidence.
ITEM_TRANSITIONS: dict[ImportJobItemStatus, frozenset[ImportJobItemStatus]] = {
    ImportJobItemStatus.pending: frozenset(
        {
            ImportJobItemStatus.uploading,
            ImportJobItemStatus.skipped,
            ImportJobItemStatus.failed,
            ImportJobItemStatus.quarantined,
        }
    ),
    ImportJobItemStatus.uploading: frozenset(
        {
            ImportJobItemStatus.uploading,
            ImportJobItemStatus.verifying,
            ImportJobItemStatus.failed,
            ImportJobItemStatus.quarantined,
        }
    ),
    ImportJobItemStatus.verifying: frozenset(
        {
            ImportJobItemStatus.uploading,
            ImportJobItemStatus.verifying,
            ImportJobItemStatus.succeeded,
            ImportJobItemStatus.skipped,
            ImportJobItemStatus.failed,
            ImportJobItemStatus.quarantined,
        }
    ),
    ImportJobItemStatus.succeeded: frozenset(
        {
            ImportJobItemStatus.uploading,
            ImportJobItemStatus.failed,
            ImportJobItemStatus.quarantined,
        }
    ),
    ImportJobItemStatus.skipped: frozenset(
        {
            ImportJobItemStatus.uploading,
            ImportJobItemStatus.failed,
            ImportJobItemStatus.quarantined,
        }
    ),
    ImportJobItemStatus.failed: frozenset(),
    ImportJobItemStatus.quarantined: frozenset(),
}


def require_job_transition(current: ImportJobStatus, target: ImportJobStatus) -> None:
    if current == target:
        return
    if target not in JOB_TRANSITIONS[current]:
        raise ValueError(f"invalid import job transition: {current.value} -> {target.value}")


def require_item_transition(
    current: ImportJobItemStatus, target: ImportJobItemStatus
) -> None:
    if current == target:
        return
    if target not in ITEM_TRANSITIONS[current]:
        raise ValueError(f"invalid import item transition: {current.value} -> {target.value}")
