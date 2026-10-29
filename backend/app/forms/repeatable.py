from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List


@dataclass
class RepeatableResult:
    completed_rows: int
    verified: bool
    errors: List[str]


class RepeatableGroupExecutor:
    """Generic row-by-row algorithm; callers supply live rescan/fill/add operations."""

    async def execute(
        self,
        rows: List[Dict[str, Any]],
        scan_rows: Callable[[], Awaitable[List[Dict[str, Any]]]],
        fill_row: Callable[[int, Dict[str, Any]], Awaitable[bool]],
        add_row: Callable[[], Awaitable[bool]],
    ) -> RepeatableResult:
        errors = []
        for index, data in enumerate(rows):
            live = await scan_rows()
            if index >= len(live):
                if not await add_row():
                    errors.append("could not add row %d" % (index + 1))
                    break
                live = await scan_rows()
            if index >= len(live):
                errors.append("row %d did not appear" % (index + 1))
                break
            if not await fill_row(index, data):
                errors.append("row %d could not be filled or verified" % (index + 1))
                break
            if index + 1 < len(rows) and len(live) <= index + 1:
                if not await add_row():
                    errors.append("add action failed after row %d" % (index + 1))
                    break
        final = await scan_rows()
        completed = min(len(rows), len(final)) if not errors else max(0, len(rows) - len(errors))
        return RepeatableResult(completed, not errors and len(final) >= len(rows), errors)

