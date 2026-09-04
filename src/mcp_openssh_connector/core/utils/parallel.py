"""Параллельный запуск однотипной работы с сохранением порядка результатов.

`fan_out` — синхронная работа в потоках (пробы, `ssh -G`, глубокие проверки),
`gather` — сопрограммы в одной группе задач (HTTP-запросы llms). Ошибка любого
элемента — наружу: обёртка, которой нужен частичный результат, ловит её сама.
"""

from collections.abc import Awaitable, Callable, Iterable
from concurrent.futures import ThreadPoolExecutor

import anyio

from ..config.constants import MAX_WORKERS


def fan_out[T, R](fn: Callable[[T], R], items: Iterable[T]) -> list[R]:
    """Применить `fn` к каждому элементу в потоках, не больше `MAX_WORKERS` разом."""
    todo = list(items)
    if not todo:
        return []
    with ThreadPoolExecutor(max_workers=min(len(todo), MAX_WORKERS)) as pool:
        return list(pool.map(fn, todo))


async def gather[T, R](fn: Callable[[T], Awaitable[R]], items: Iterable[T]) -> list[R]:
    """Выполнить `fn` для всех элементов параллельно в одной группе задач."""
    todo = list(items)
    results: dict[int, R] = {}

    async def one(i: int, item: T) -> None:
        results[i] = await fn(item)

    async with anyio.create_task_group() as tg:
        for i, item in enumerate(todo):
            tg.start_soon(one, i, item)
    return [results[i] for i in range(len(todo))]
