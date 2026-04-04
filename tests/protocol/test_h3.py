from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

from hypercorn.asyncio.worker_context import EventWrapper
from hypercorn.protocol.h3_send import H3SendScheduler


class DummyTaskGroup:
    def __init__(self) -> None:
        self.tasks: list[asyncio.Task] = []

    def spawn(self, func, *args) -> None:
        self.tasks.append(asyncio.create_task(func(*args)))

    async def aclose(self) -> None:
        for task in self.tasks:
            task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_send_scheduler_batches_flush_across_ready_events() -> None:
    connection = Mock()
    flush = AsyncMock()
    scheduler = H3SendScheduler(connection, EventWrapper, flush)
    task_group = DummyTaskGroup()
    closed = False
    task_group.spawn(scheduler.run, lambda: closed)

    tasks = [
        asyncio.create_task(scheduler.headers(1, [(b":status", b"200")])),
        asyncio.create_task(scheduler.data(1, b"hello")),
        asyncio.create_task(scheduler.data(1, b"", end_stream=True)),
    ]
    await asyncio.gather(*tasks)

    assert connection.send_headers.call_count == 1
    assert connection.send_data.call_count == 2
    flush.assert_awaited_once()

    closed = True
    await scheduler.wake()
    await task_group.aclose()


@pytest.mark.asyncio
async def test_send_scheduler_propagates_flush_errors() -> None:
    connection = Mock()
    flush = AsyncMock(side_effect=RuntimeError("boom"))
    scheduler = H3SendScheduler(connection, EventWrapper, flush)
    task_group = DummyTaskGroup()
    closed = False
    task_group.spawn(scheduler.run, lambda: closed)

    with pytest.raises(RuntimeError, match="boom"):
        await scheduler.data(1, b"hello")

    closed = True
    await scheduler.wake()
    await task_group.aclose()
