"""Shared repository helpers — pagination, filtering, ordering."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Generic, TypeVar

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

T = TypeVar("T")


@dataclass
class Page(Generic[T]):
    items: list[T]
    total: int
    page: int
    page_size: int

    @property
    def total_pages(self) -> int:
        if self.page_size <= 0:
            return 0
        return (self.total + self.page_size - 1) // self.page_size


SORT_DIRECTIONS = {"asc", "desc"}


def apply_filters(stmt: Select, model: Any, filters: dict[str, Any]) -> Select:
    """Only apply filters for columns that actually exist — never guess.

    A list/tuple/set value becomes ``IN (…)`` rather than an equality test, so
    a caller can express "any of these states" without hand-building SQL. One
    UI status bucket covers several domain states, and filtering on only the
    first would silently under-report.
    """
    for column, value in filters.items():
        if value is None:
            continue
        attr = getattr(model, column, None)
        if attr is None:
            continue
        if isinstance(value, list | tuple | set | frozenset):
            members = list(value)
            if not members:
                continue
            stmt = stmt.where(attr.in_(members))
        else:
            stmt = stmt.where(attr == value)
    return stmt


def apply_sorting(
    stmt: Select, model: Any, sort: str | None, default: str = "created_at"
) -> Select:
    column_name = default
    direction = "desc"
    if sort:
        raw = sort.lstrip("-")
        if getattr(model, raw, None) is not None:
            column_name = raw
            direction = "desc" if sort.startswith("-") else "asc"
    column = getattr(model, column_name)
    return stmt.order_by(column.desc() if direction == "desc" else column.asc())


async def paginate(
    session: AsyncSession,
    stmt: Select,
    *,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[Any], int]:
    """Return (items, total) where total honours the same filters as the query."""
    page = max(1, page)
    page_size = max(1, min(page_size, 200))
    count_stmt = select(func.count()).select_from(stmt.order_by(None).subquery())
    total = int(await session.scalar(count_stmt) or 0)
    rows = (
        await session.execute(stmt.offset((page - 1) * page_size).limit(page_size))
    ).scalars().all()
    return list(rows), total
