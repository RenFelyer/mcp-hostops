"""Running uniform work in parallel while preserving result order.

`fan_out` is synchronous work in threads (probes, `ssh -G`, deep checks);
`gather` is coroutines in a single task group (llms HTTP requests). Any
element's error propagates; a caller that needs a partial result catches it
itself.
"""

from collections.abc import Awaitable, Callable, Iterable
from concurrent.futures import ThreadPoolExecutor

import anyio

from ..config.constants import MAX_WORKERS


def fan_out[T, R](fn: Callable[[T], R], items: Iterable[T]) -> list[R]:
    """Apply `fn` to each item in threads, at most `MAX_WORKERS` at a time."""
    todo = list(items)
    if not todo:
        return []
    with ThreadPoolExecutor(max_workers=min(len(todo), MAX_WORKERS)) as pool:
        return list(pool.map(fn, todo))


async def gather[T, R](fn: Callable[[T], Awaitable[R]], items: Iterable[T]) -> list[R]:
    """Run `fn` for all items in parallel within a single task group."""
    todo = list(items)
    results: dict[int, R] = {}

    async def one(i: int, item: T) -> None:
        results[i] = await fn(item)

    async with anyio.create_task_group() as tg:
        for i, item in enumerate(todo):
            tg.start_soon(one, i, item)
    return [results[i] for i in range(len(todo))]
