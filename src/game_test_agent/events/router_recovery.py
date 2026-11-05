"""Lifecycle-managed bounded recovery of already-authorized routing requests."""
from __future__ import annotations

import asyncio
import logging


class RouterRecovery:
    def __init__(self, service, *, enabled, poll_seconds=1):
        self.service = service
        self.enabled = enabled
        self.poll_seconds = max(1, poll_seconds)
        self.task = None
        self.status = "disabled" if not enabled else "stopped"
        self.last_pass = None

    def start(self):
        if not self.enabled or (self.task is not None and not self.task.done()):
            return
        self.status = "running"
        self.task = asyncio.create_task(self._run(), name="router-evaluation-recovery")

    async def _run(self):
        try:
            while True:
                try:
                    self.last_pass = await self.service.recover_due(limit=4)
                    returned = getattr(self.service, "returned_inputs", None)
                    if returned is not None:
                        self.last_pass = {**self.last_pass, "input_returns": await returned.recover_due(limit=4)}
                    observations = getattr(self.service, "observations", None)
                    if observations is not None:
                        self.last_pass = {**self.last_pass, "observations": await observations.recover_due(limit=4)}
                    self.status = "running"
                except Exception:
                    self.status = "unavailable"
                    logging.getLogger(__name__).warning("route recovery pass failed; retrying")
                await asyncio.sleep(self.poll_seconds)
        finally:
            self.status = "stopped"

    async def close(self):
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None
