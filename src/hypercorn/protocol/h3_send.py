from __future__ import annotations

from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from ..typing import Event as IOEvent

MAX_BATCHED_SENDS = 32


@dataclass
class _QueuedOperation:
    apply: Callable[[], bool]
    complete: IOEvent
    error: Exception | None = None


class H3SendScheduler:
    def __init__(
        self,
        connection: object,
        event_class: type[IOEvent],
        flush: Callable[[], Awaitable[None]],
    ) -> None:
        self.connection = connection
        self.flush = flush
        self.has_data = event_class()
        self._event_class = event_class
        self._queue: deque[_QueuedOperation] = deque()

    async def run(self, should_stop: Callable[[], bool]) -> None:
        while not should_stop():
            if not self._queue:
                await self.has_data.wait()
                await self.has_data.clear()
                if should_stop():
                    break

            await self._send_ready_batch()

    async def headers(
        self, stream_id: int, headers: list[tuple[bytes, bytes]], end_stream: bool = False
    ) -> None:
        await self._enqueue(
            lambda: self._send_headers(stream_id, headers, end_stream=end_stream)
        )

    async def data(self, stream_id: int, data: bytes, end_stream: bool = False) -> None:
        await self._enqueue(lambda: self._send_data(stream_id, data, end_stream=end_stream))

    async def wake(self) -> None:
        await self.has_data.set()

    async def _enqueue(self, apply: Callable[[], bool]) -> None:
        operation = _QueuedOperation(apply=apply, complete=self._event_class())
        self._queue.append(operation)
        await self.has_data.set()
        await operation.complete.wait()
        if operation.error is not None:
            raise operation.error

    async def _send_ready_batch(self) -> None:
        needs_flush = False
        processed: list[_QueuedOperation] = []

        try:
            while self._queue and len(processed) < MAX_BATCHED_SENDS:
                operation = self._queue.popleft()
                processed.append(operation)
                needs_flush |= operation.apply()

            if needs_flush:
                await self.flush()
        except Exception as error:
            for operation in processed:
                operation.error = error
        finally:
            for operation in processed:
                await operation.complete.set()

        if self._queue:
            await self.has_data.set()

    def _send_headers(
        self, stream_id: int, headers: list[tuple[bytes, bytes]], end_stream: bool = False
    ) -> bool:
        self.connection.send_headers(stream_id, headers, end_stream=end_stream)
        return True

    def _send_data(self, stream_id: int, data: bytes, end_stream: bool = False) -> bool:
        self.connection.send_data(stream_id, data, end_stream)
        return True
