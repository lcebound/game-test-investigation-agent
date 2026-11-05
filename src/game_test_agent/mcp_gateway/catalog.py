"""Optional, platform-neutral test catalog for interactive clients."""

from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from game_test_agent.contracts.models import TestRef


class TestCatalogItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    test_ref: TestRef
    name: str
    description: str = ""
    target_kinds: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)


class TestCatalogPage(BaseModel):
    items: list[TestCatalogItem]
    total: int = Field(ge=0)
    page: int = Field(ge=1)
    page_size: int = Field(ge=1, le=100)


@runtime_checkable
class TestCatalogProvider(Protocol):
    @property
    def catalog_supported(self) -> bool: ...

    async def list_tests(
        self, query: str = "", *, page: int = 1, page_size: int = 50
    ) -> TestCatalogPage: ...
