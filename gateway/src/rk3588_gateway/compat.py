from __future__ import annotations

import asyncio
from functools import partial
from pathlib import Path
from typing import Any, Callable, TypeVar

T = TypeVar("T")


async def to_thread(func: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, partial(func, *args, **kwargs))


def unlink_missing_ok(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
