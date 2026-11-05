from __future__ import annotations

from datetime import datetime, timezone

from game_test_agent.contracts.models import ProjectPack
from game_test_agent.storage import ConflictError, NotFoundError, Repository, payload_hash


class ProjectPackRegistry:
    """Immutable version registry for reviewed, declarative project knowledge."""

    def __init__(self, repository: Repository | None = None) -> None:
        self.repository = repository
        self._packs: dict[tuple[str, str, str], tuple[ProjectPack, str]] = {}

    def register(self, pack: ProjectPack) -> str:
        if self.repository is not None:
            try:
                return self.repository.register_project_pack(pack)
            except ConflictError as error:
                raise ValueError(str(error)) from error
        key = (pack.tenant_id, pack.project_id, pack.version)
        digest = payload_hash(pack)
        existing = self._packs.get(key)
        if existing is not None and existing[1] != digest:
            raise ValueError("project pack version is immutable")
        self._packs[key] = (pack, digest)
        return digest

    def resolve(self, tenant_id: str, project_id: str, version: str) -> ProjectPack:
        if self.repository is not None:
            try:
                pack = self.repository.get_project_pack(tenant_id, project_id, version)
            except NotFoundError as error:
                raise LookupError(str(error)) from error
        else:
            item = self._packs.get((tenant_id, project_id, version))
            if item is None:
                raise LookupError(f"project pack {project_id!r}/{version!r} is unavailable")
            pack = item[0]
        if pack.valid_until is not None and pack.valid_until <= datetime.now(timezone.utc):
            raise LookupError("project pack has expired")
        return pack
