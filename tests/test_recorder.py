"""The stream recorder against a local HTTP server that drops, stalls and misbehaves."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from custom_components.radio_recorder.recorder import Recording, StreamRecorder, UnsupportedStream

CHUNK = b"\xff\xfb" + b"\x00" * 1022


class Station:
    """Streams CHUNK every 20 ms. Set `drop_after` to close the first N connections early."""

    def __init__(self) -> None:
        self.connections = 0
        self.drop_after: int | None = None
        self.drops_left = 0
        self.content_type = "audio/mpeg"
        self.status = 200
        self.stall = False
        self.headers: list[dict[str, str]] = []

    async def handle(self, request: web.Request) -> web.StreamResponse:
        self.connections += 1
        self.headers.append(dict(request.headers))
        if self.status != 200:
            return web.Response(status=self.status)
        resp = web.StreamResponse(headers={"Content-Type": self.content_type})
        await resp.prepare(request)
        sent = 0
        while True:
            if self.stall:
                await asyncio.sleep(3600)
            await resp.write(CHUNK)
            sent += 1
            if self.drop_after is not None and self.drops_left and sent >= self.drop_after:
                self.drops_left -= 1
                return resp
            await asyncio.sleep(0.02)


@pytest.fixture
async def station(socket_enabled):
    """A local stream. HA's test harness blocks sockets unless a test asks for them."""
    station = Station()
    app = web.Application()
    app.router.add_get("/stream", station.handle)
    server = TestServer(app, host="127.0.0.1")
    await server.start_server()
    station.url = str(server.make_url("/stream"))
    yield station
    await server.close()


@pytest.fixture
async def session():
    async with aiohttp.ClientSession() as session:
        yield session


def make_recording(tmp_path: Path, url: str, seconds: float) -> Recording:
    now = datetime.now(UTC)
    return Recording(
        id="abc",
        station="KALX",
        show="Madame X",
        url=url,
        start=now,
        end=now + timedelta(seconds=seconds),
        incoming=tmp_path / ".incoming" / "abc.part",
    )


async def test_records_until_the_end_time(station, session, tmp_path) -> None:
    rec = make_recording(tmp_path, station.url, 0.5)
    await StreamRecorder(session, rec).run()
    data = rec.incoming.read_bytes()
    assert len(data) == rec.bytes > 0
    assert data.startswith(CHUNK)
    assert rec.content_type == "audio/mpeg"
    assert rec.reconnects == 0
    assert datetime.now(UTC) >= rec.end
    assert "Icy-MetaData" not in station.headers[0]


async def test_reconnects_and_appends_after_a_drop(station, session, tmp_path) -> None:
    station.drop_after = 3
    station.drops_left = 1
    rec = make_recording(tmp_path, station.url, 1.8)
    await StreamRecorder(session, rec).run()
    assert station.connections == 2
    assert rec.reconnects == 1
    assert rec.incoming.stat().st_size == rec.bytes > 3 * len(CHUNK)


async def test_stalled_stream_is_retried(station, session, tmp_path) -> None:
    station.stall = True
    rec = make_recording(tmp_path, station.url, 1.6)
    await StreamRecorder(session, rec, stall_timeout=0.3).run()
    assert station.connections >= 2


async def test_server_errors_are_retried_until_the_end(station, session, tmp_path) -> None:
    station.status = 503
    rec = make_recording(tmp_path, station.url, 1.6)
    await StreamRecorder(session, rec).run()
    assert station.connections >= 2
    assert rec.bytes == 0


async def test_stop_ends_early_and_keeps_the_audio(station, session, tmp_path) -> None:
    rec = make_recording(tmp_path, station.url, 60)
    recorder = StreamRecorder(session, rec)
    task = asyncio.create_task(recorder.run())
    await asyncio.sleep(0.3)
    recorder.stop()
    await asyncio.wait_for(task, 2)
    assert rec.incoming.stat().st_size == rec.bytes > 0


async def test_cancel_writes_what_was_buffered(station, session, tmp_path) -> None:
    rec = make_recording(tmp_path, station.url, 60)
    task = asyncio.create_task(StreamRecorder(session, rec).run())
    await asyncio.sleep(0.3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert rec.incoming.stat().st_size == rec.bytes > 0


async def test_appends_to_an_existing_partial_file(station, session, tmp_path) -> None:
    rec = make_recording(tmp_path, station.url, 0.3)
    rec.incoming.parent.mkdir(parents=True)
    rec.incoming.write_bytes(b"before restart")
    await StreamRecorder(session, rec).run()
    assert rec.incoming.read_bytes().startswith(b"before restart" + CHUNK)


async def test_web_page_is_not_recorded(station, session, tmp_path) -> None:
    station.content_type = "text/html"
    rec = make_recording(tmp_path, station.url, 60)
    with pytest.raises(UnsupportedStream):
        await asyncio.wait_for(StreamRecorder(session, rec).run(), 2)
    assert station.connections == 1


def test_recording_round_trips_through_a_dict(tmp_path) -> None:
    rec = make_recording(tmp_path, "http://x/stream", 10)
    rec.event_key = "k"
    rec.bytes = 5
    assert Recording.from_dict(rec.as_dict()) == rec
