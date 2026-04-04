from __future__ import annotations

from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from ..typing import Event as IOEvent

MAX_BATCHED_SENDS = 32


class H3SendClosedError(RuntimeError):
    pass


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
        self._closed = False
        self._queue: deque[_QueuedOperation] = deque()

    async def run(self, should_stop: Callable[[], bool]) -> None:
        while True:
            if self._closed or should_stop():
                await self._fail_pending(H3SendClosedError("H3 send scheduler is closed"))
                break
            if not self._queue:
                await self.has_data.wait()
                await self.has_data.clear()
                if self._closed or should_stop():
                    await self._fail_pending(H3SendClosedError("H3 send scheduler is closed"))
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

    async def close(self) -> None:
        self._closed = True
        await self._fail_pending(H3SendClosedError("H3 send scheduler is closed"))
        await self.has_data.set()

    async def _enqueue(self, apply: Callable[[], bool]) -> None:
        if self._closed:
            raise H3SendClosedError("H3 send scheduler is closed")
        operation = _QueuedOperation(apply=apply, complete=self._event_class())
        self._queue.append(operation)
        await self.has_data.set()
        await operation.complete.wait()
        if operation.error is not None:
            raise operation.error

    async def _fail_pending(self, error: Exception) -> None:
        while self._queue:
            operation = self._queue.popleft()
            operation.error = error
            await operation.complete.set()

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
