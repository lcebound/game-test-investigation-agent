"""Budget-aware waiting shared by execution backends."""

from datetime import datetime, timedelta


class ExternalWaitStopped(RuntimeError):
    def __init__(self, stop_reason: str) -> None:
        self.stop_reason = stop_reason
        super().__init__(stop_reason)


def poll_delay(remaining_seconds: float, available_reads: int, suggested: float) -> float:
    """Spread status reads across the remaining deadline, keeping one final check."""
    if available_reads <= 1:
        return max(remaining_seconds, 0)
    return min(max(suggested, remaining_seconds / available_reads, 0.1), remaining_seconds)


def investigation_deadline(created_at: datetime, wall_seconds: int) -> datetime:
    return created_at + timedelta(seconds=wall_seconds)
