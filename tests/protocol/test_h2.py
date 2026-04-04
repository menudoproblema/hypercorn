from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, call, Mock

import pytest
from h2.connection import H2Connection
from h2.events import ConnectionTerminated, DataReceived

from hypercorn.asyncio.worker_context import EventWrapper, WorkerContext
from hypercorn.config import Config
from hypercorn.events import Closed, RawData
from hypercorn.protocol.events import Body, StreamClosed
from hypercorn.protocol.h2 import BUFFER_HIGH_WATER, BufferCompleteError, H2Protocol, StreamBuffer
from hypercorn.protocol.h2_send import H2SendScheduler
from hypercorn.protocol.queued_stream import QueuedStream
from hypercorn.typing import ConnectionState


class DummyTaskGroup:
    def __init__(self) -> None:
        self.tasks: list[asyncio.Task] = []

    def spawn(self, func, *args) -> None:
        self.tasks.append(asyncio.create_task(func(*args)))

    async def spawn_app(self, *args, **kwargs) -> AsyncMock:
        return AsyncMock()

    async def aclose(self) -> None:
        for task in self.tasks:
            task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_stream_buffer_push_and_pop() -> None:
    event_loop: asyncio.AbstractEventLoop = asyncio.get_running_loop()

    stream_buffer = StreamBuffer(EventWrapper)

    async def _push_over_limit() -> bool:
        await stream_buffer.push(b"a" * (BUFFER_HIGH_WATER + 1))
        return True

    task = event_loop.create_task(_push_over_limit())
    await asyncio.sleep(0)
    assert not task.done()  # Blocked as over high water
    await stream_buffer.pop(BUFFER_HIGH_WATER // 4)
    assert not task.done()  # Blocked as over low water
    await stream_buffer.pop((BUFFER_HIGH_WATER // 4) + 1)
    assert (await task) is True


@pytest.mark.asyncio
async def test_stream_buffer_drain() -> None:
    event_loop: asyncio.AbstractEventLoop = asyncio.get_running_loop()

    stream_buffer = StreamBuffer(EventWrapper)
    await stream_buffer.push(b"a" * 10)

    async def _drain() -> bool:
        await stream_buffer.drain()
        return True

    task = event_loop.create_task(_drain())
    assert not task.done()  # Blocked
    await stream_buffer.pop(20)
    assert (await task) is True


@pytest.mark.asyncio
async def test_stream_buffer_closed() -> None:
    stream_buffer = StreamBuffer(EventWrapper)
    await stream_buffer.close()
    await stream_buffer._is_empty.wait()
    await stream_buffer._paused.wait()
    assert True
    with pytest.raises(BufferCompleteError):
        await stream_buffer.push(b"a")


@pytest.mark.asyncio
async def test_stream_buffer_complete() -> None:
    stream_buffer = StreamBuffer(EventWrapper)
    await stream_buffer.push(b"a" * 10)
    assert not stream_buffer.complete
    stream_buffer.set_complete()
    assert not stream_buffer.complete
    await stream_buffer.pop(20)
    assert stream_buffer.complete


@pytest.mark.asyncio
async def test_stream_buffer_pop_across_chunks() -> None:
    stream_buffer = StreamBuffer(EventWrapper)
    await stream_buffer.push(b"abcd")
    await stream_buffer.push(b"efgh")

    assert bytes(await stream_buffer.pop(6)) == b"abcdef"
    assert bytes(await stream_buffer.pop(6)) == b"gh"


@pytest.mark.asyncio
async def test_send_scheduler_sends_data_and_closes_stream() -> None:
    connection = Mock()
    connection.local_flow_control_window.return_value = 5
    connection.max_outbound_frame_size = 5
    flush = AsyncMock()
    scheduler = H2SendScheduler(connection, EventWrapper, flush)

    scheduler.register_stream(1)
    await scheduler.buffer(1, b"hello")
    scheduler.stream_buffers[1].set_complete()
    await scheduler._send_ready_batch(1)

    connection.send_data.assert_called_once()
    assert bytes(connection.send_data.call_args.args[1]) == b"hello"
    connection.end_stream.assert_called_once_with(1)
    flush.assert_awaited_once()
    assert 1 not in scheduler.stream_buffers


@pytest.mark.asyncio
async def test_send_scheduler_batches_flush_across_ready_streams() -> None:
    connection = Mock()
    connection.local_flow_control_window.return_value = 5
    connection.max_outbound_frame_size = 5
    flush = AsyncMock()
    scheduler = H2SendScheduler(connection, EventWrapper, flush)

    scheduler.register_stream(1)
    scheduler.register_stream(3)
    await scheduler.buffer(1, b"hello")
    await scheduler.buffer(3, b"world")
    scheduler.stream_buffers[1].set_complete()
    scheduler.stream_buffers[3].set_complete()

    await scheduler._send_ready_batch(1)

    assert connection.send_data.call_count == 2
    assert connection.end_stream.call_count == 2
    flush.assert_awaited_once()


@pytest.mark.asyncio
async def test_protocol_handle_protocol_error() -> None:
    task_group = DummyTaskGroup()
    protocol = H2Protocol(
        Mock(),
        Config(),
        WorkerContext(None),
        task_group,  # type: ignore[arg-type]
        ConnectionState({}),
        False,
        None,
        None,
        AsyncMock(),
    )
    await protocol.handle(RawData(data=b"broken nonsense\r\n\r\n"))
    protocol.send.assert_awaited()  # type: ignore
    assert protocol.send.call_args_list == [call(Closed())]  # type: ignore
    await task_group.aclose()


@pytest.mark.asyncio
async def test_protocol_keep_alive_max_requests() -> None:
    task_group = DummyTaskGroup()
    protocol = H2Protocol(
        Mock(),
        Config(),
        WorkerContext(None),
        task_group,  # type: ignore[arg-type]
        ConnectionState({}),
        False,
        None,
        None,
        AsyncMock(),
    )
    protocol.config.keep_alive_max_requests = 0
    client = H2Connection()
    client.initiate_connection()
    headers = [
        (":method", "GET"),
        (":path", "/reqinfo"),
        (":authority", "hypercorn"),
        (":scheme", "https"),
    ]
    client.send_headers(1, headers, end_stream=True)
    await protocol.handle(RawData(data=client.data_to_send()))
    protocol.send.assert_awaited()  # type: ignore
    events = client.receive_data(protocol.send.call_args_list[1].args[0].data)  # type: ignore
    assert isinstance(events[-1], ConnectionTerminated)
    await task_group.aclose()


@pytest.mark.asyncio
async def test_protocol_ignores_data_received_for_closed_stream() -> None:
    task_group = DummyTaskGroup()
    protocol = H2Protocol(
        Mock(),
        Config(),
        WorkerContext(None),
        task_group,  # type: ignore[arg-type]
        ConnectionState({}),
        False,
        None,
        None,
        AsyncMock(),
    )
    protocol.connection.acknowledge_received_data = Mock()  # type: ignore[method-assign]
    protocol._flush = AsyncMock()  # type: ignore[method-assign]

    await protocol._handle_events(
        [DataReceived(stream_id=1, data=b"body", flow_controlled_length=4)]
    )

    protocol.connection.acknowledge_received_data.assert_not_called()
    protocol._flush.assert_awaited_once()
    await task_group.aclose()


@pytest.mark.asyncio
async def test_protocol_idle_requires_no_registered_streams() -> None:
    task_group = DummyTaskGroup()
    protocol = H2Protocol(
        Mock(),
        Config(),
        WorkerContext(None),
        task_group,  # type: ignore[arg-type]
        ConnectionState({}),
        False,
        None,
        None,
        AsyncMock(),
    )
    stream = Mock(spec=QueuedStream)
    stream.idle = True

    assert protocol.idle is True
    protocol.streams[1] = stream
    assert protocol.idle is False

    del protocol.streams[1]
    assert protocol.idle is True
    await task_group.aclose()


@pytest.mark.asyncio
async def test_protocol_does_not_block_other_streams_on_slow_stream() -> None:
    class BlockingStream:
        idle = False

        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def handle(self, event: Body | StreamClosed) -> None:
            if isinstance(event, Body):
                self.started.set()
                await self.release.wait()

    class RecordingStream:
        idle = False

        def __init__(self) -> None:
            self.body_received = asyncio.Event()

        async def handle(self, event: Body | StreamClosed) -> None:
            if isinstance(event, Body):
                self.body_received.set()

    task_group = DummyTaskGroup()
    protocol = H2Protocol(
        Mock(),
        Config(),
        WorkerContext(None),
        task_group,  # type: ignore[arg-type]
        ConnectionState({}),
        False,
        None,
        None,
        AsyncMock(),
    )
    protocol.connection.acknowledge_received_data = Mock()  # type: ignore[method-assign]
    protocol._flush = AsyncMock()  # type: ignore[method-assign]

    slow_stream = BlockingStream()
    fast_stream = RecordingStream()
    protocol.streams[1] = QueuedStream(slow_stream, task_group, protocol.context)  # type: ignore[arg-type]
    protocol.streams[3] = QueuedStream(fast_stream, task_group, protocol.context)  # type: ignore[arg-type]

    await protocol._handle_events(
        [
            DataReceived(stream_id=1, data=b"a", flow_controlled_length=1),
            DataReceived(stream_id=3, data=b"b", flow_controlled_length=1),
        ]
    )

    await asyncio.wait_for(slow_stream.started.wait(), timeout=0.1)
    await asyncio.wait_for(fast_stream.body_received.wait(), timeout=0.1)
    protocol.connection.acknowledge_received_data.assert_called_once_with(1, 3)

    slow_stream.release.set()
    await asyncio.wait_for(asyncio.sleep(0), timeout=0.1)
    await asyncio.wait_for(asyncio.sleep(0), timeout=0.1)
    assert protocol.connection.acknowledge_received_data.call_args_list == [call(1, 3), call(1, 1)]

    await protocol.streams[1].handle(StreamClosed(stream_id=1))
    await protocol.streams[3].handle(StreamClosed(stream_id=3))
    await asyncio.gather(*task_group.tasks)
