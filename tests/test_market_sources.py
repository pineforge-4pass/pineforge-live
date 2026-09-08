import asyncio
import io
import json
from dataclasses import asdict

import pytest

from pineforge_live import types as T
from pineforge_live.sources import SourceConfig, SourceError, create_source, load_history, parse_event, parse_frame
from pineforge_live.sources.base import MAX_FRAME_BYTES, SequenceTracker, validate_url
from pineforge_live.sources.http import HttpSource
from pineforge_live.sources.jsonl import JsonlSource
from pineforge_live.sources.websocket import WebSocketSource


def tick(seq=1, ts=1):
    return {'type': 'tick', 'ts': ts, 'seq': seq, 'price': 100, 'qty': 0.1}


def bar(stamp=0):
    return {'type': 'bar', 'bar': {'ts_open': stamp, 'o': 100, 'h': 102, 'l': 99, 'c': 101, 'v': 10}}


def test_generic_tick_confirmed_forming_and_arrays():
    frame = json.dumps([tick(), bar(), {'type': 'forming', 'bar': bar()['bar']}])
    a, b, c = parse_frame(frame, '1')
    assert isinstance(a, T.Tick) and isinstance(b, T.Confirmed) and isinstance(c, T.Forming)
    assert not b.bar.is_forming and c.bar.is_forming
    assert b.bar.trade_count == 0


@pytest.mark.parametrize('field,value', [('ts', True), ('seq', -1), ('seq', 1.2), ('price', float('nan')),
                                        ('price', float('inf')), ('qty', -0.1), ('qty', '2')])
def test_tick_numeric_boundaries(field, value):
    row = tick()
    row[field] = value
    with pytest.raises(SourceError):
        parse_event(row, '1')


@pytest.mark.parametrize('field,value', [('ts_open', 1), ('h', 99), ('l', 101), ('c', 110), ('v', -1),
                                        ('o', float('nan')), ('trade_count', True), ('is_forming', True)])
def test_bar_alignment_and_ohlcv_boundaries(field, value):
    row = bar()
    row['bar'][field] = value
    with pytest.raises(SourceError):
        parse_event(row, '1')


def test_weekly_alignment_is_monday_anchored():
    row = bar(4 * 86_400_000)
    assert parse_event(row, 'W').bar.ts_open == 4 * 86_400_000
    with pytest.raises(SourceError):
        parse_event(bar(0), 'W')


@pytest.mark.parametrize('frame', ['{"type":"tick","type":"bar"}', '{', 'null', '[1]',
                                   '{"type":"unknown"}', '{"type":"tick","price":NaN}'])
def test_malformed_unknown_nonfinite_json_refused(frame):
    with pytest.raises(SourceError):
        parse_frame(frame, '1')


def test_frame_size_bound():
    with pytest.raises(SourceError, match='one MiB'):
        parse_frame('x' * (MAX_FRAME_BYTES + 1), '1')


def test_jsonl_finite_file_and_stdin_filter_and_gap(tmp_path):
    rows = [tick(1), tick(2), tick(2), tick(4), bar()]
    path = tmp_path / 'events.jsonl'
    path.write_text('\n'.join(json.dumps(row) for row in rows) + '\n')
    async def collect(source):
        return [event async for event in source.events(from_seq=1)]
    first = asyncio.run(collect(create_source(SourceConfig('jsonl', path=path), '1')))
    second = asyncio.run(collect(JsonlSource(script_tf='1', stream=io.StringIO(path.read_text()))))
    assert first == second
    assert len(first) == 4 and first[1] == T.TickGap(3, 3, False)


def test_conflicting_duplicate_tick_is_not_silently_accepted():
    tracker = SequenceTracker()
    tracker.accept(parse_event(tick(), '1'))
    other = tick()
    other['price'] = 101
    with pytest.raises(SourceError, match='conflicting duplicate'):
        tracker.accept(parse_event(other, '1'))


def test_http_poll_parses_supplied_schema_and_locally_filters_sequences():
    frames = iter([json.dumps([tick(1), tick(2)]), json.dumps([tick(2), tick(3)])])
    source = HttpSource(SourceConfig('http', url='http://localhost:9999/events', poll_interval_ms=1),
                        script_tf='1', fetch=lambda: next(frames))
    async def run():
        result = []
        async for event in source.events(from_seq=1):
            result.append(event)
            if len(result) == 2:
                break
        return result
    assert [event.tick.seq for event in asyncio.run(run())] == [2, 3]


def test_http_malformed_response_stops_with_redacted_error():
    source = HttpSource(SourceConfig('http', url='https://example.test/events?token=private'),
                        script_tf='1', fetch=lambda: b'private invalid payload')
    async def run():
        return await anext(source.events())
    with pytest.raises(SourceError) as error:
        asyncio.run(run())
    assert 'private' not in str(error.value)


def test_websocket_reconnect_preserves_seq_without_inventing_resume_protocol(monkeypatch):
    frames = iter([[json.dumps(tick(5))], [json.dumps(tick(5)), json.dumps(tick(7))]])
    calls = []
    sleeps = []
    class Socket:
        def __init__(self):
            self.rows = iter(next(frames))
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        def __aiter__(self):
            return self
        async def __anext__(self):
            try:
                return next(self.rows)
            except StopIteration:
                raise StopAsyncIteration
    def connect(url, **kwargs):
        calls.append((url, kwargs))
        return Socket()
    async def sleep(duration):
        sleeps.append(duration)
    monkeypatch.setattr('pineforge_live.sources.websocket.asyncio.sleep', sleep)
    source = WebSocketSource(SourceConfig('websocket', url='wss://example.test/events'), script_tf='1', connect=connect)
    async def run():
        result = []
        async for event in source.events(from_seq=4):
            result.append(event)
            if len(result) == 3:
                break
        return result
    events = asyncio.run(run())
    assert events[1] == T.TickGap(6, 6, False)
    assert len(calls) == 2 and all(url == 'wss://example.test/events' for url, _ in calls)
    assert all('from_seq' not in kwargs for _, kwargs in calls)
    assert sleeps == [1]


def test_websocket_bad_frame_is_not_retried():
    class Socket:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        def __aiter__(self):
            return self
        async def __anext__(self):
            return '{"token":"private"}'
    source = WebSocketSource(SourceConfig('websocket', url='wss://example.test'), script_tf='1', connect=lambda *a, **kw: Socket())
    async def run():
        await anext(source.events())
    with pytest.raises(SourceError) as error:
        asyncio.run(run())
    assert 'private' not in str(error.value)


@pytest.mark.parametrize('url', ['http://external.test', 'https://secret:token@host.test', 'file:///tmp/source',
                                  'https://host.test#secret', 'https://host.test:99999', 'https://host.test\n'])
def test_source_url_validation(url):
    with pytest.raises(SourceError):
        validate_url(url)


def test_explicit_self_hosted_source():
    assert SourceConfig('http', url='http://server.lan', allow_insecure=True).url
    assert SourceConfig('websocket', url='ws://127.0.0.1:8765').url
    with pytest.raises(SourceError):
        SourceConfig('stdin', url='https://example.test')
    with pytest.raises(SourceError):
        SourceConfig('http', url='https://example.test', timeout_ms=True)


@pytest.mark.parametrize('rows', ['0,1,2,0,1,1\n120000,1,2,0,1,1\n', '0,1,2,0,1,nan\n',
                                  '1,1,2,0,1,1\n', '0,1,2,0,1,1\n0,1,2,0,1,1\n', ''])
def test_history_rejects_gap_nonfinite_unaligned_duplicates_empty(tmp_path, rows):
    path = tmp_path / 'history.csv'
    path.write_text('timestamp,open,high,low,close,volume\n' + rows)
    with pytest.raises(SourceError):
        load_history(path, '1')


def test_http_source_over_real_loopback_transport():
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            payload = json.dumps([tick(1), bar()]).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        config = SourceConfig('http', url=f'http://127.0.0.1:{server.server_port}/events')
        async def run():
            source = HttpSource(config, script_tf='1').events()
            try:
                return await anext(source), await anext(source)
            finally:
                await source.aclose()
        a, b = asyncio.run(run())
        assert isinstance(a, T.Tick) and isinstance(b, T.Confirmed)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_websocket_source_over_real_loopback_transport():
    pytest.importorskip('websockets.asyncio.server')
    from websockets.asyncio.server import serve
    async def run():
        async def handler(socket):
            await socket.send(json.dumps([tick(10), bar()]))
            await socket.wait_closed()
        async with serve(handler, '127.0.0.1', 0) as server:
            port = server.sockets[0].getsockname()[1]
            config = SourceConfig('websocket', url=f'ws://127.0.0.1:{port}/events')
            source = WebSocketSource(config, script_tf='1').events(from_seq=9)
            try:
                return await anext(source), await anext(source)
            finally:
                await source.aclose()
    a, b = asyncio.run(run())
    assert a.tick.seq == 10 and isinstance(b, T.Confirmed)


def test_stdin_pipe_cancellation_does_not_wait_for_eof_or_change_fd_flags():
    import fcntl
    import os
    read_fd, write_fd = os.pipe()
    stream = os.fdopen(read_fd, 'r')
    flags_before = fcntl.fcntl(read_fd, fcntl.F_GETFL)
    async def run():
        source = JsonlSource(script_tf='1', stream=stream).events()
        task = asyncio.create_task(anext(source))
        # An incomplete line stays pending; cancellation must not join a
        # blocked executor thread while the writer intentionally stays open.
        os.write(write_fd, b'{"type":"tick"')
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1)
        await source.aclose()
        assert not asyncio.get_running_loop().remove_reader(read_fd)
    try:
        asyncio.run(run())
        assert fcntl.fcntl(read_fd, fcntl.F_GETFL) == flags_before
    finally:
        stream.close()
        os.close(write_fd)


def test_stdin_pipe_fragmented_frames_and_last_line_without_newline():
    import os
    read_fd, write_fd = os.pipe()
    stream = os.fdopen(read_fd, 'r')
    async def run():
        source = JsonlSource(script_tf='1', stream=stream).events()
        async def writer():
            payload = (json.dumps(tick(1)) + '\n' + json.dumps(tick(2))).encode()
            os.write(write_fd, payload[:20])
            await asyncio.sleep(0.01)
            os.write(write_fd, payload[20:])
            os.close(write_fd)
        task = asyncio.create_task(writer())
        result = [event async for event in source]
        await task
        return result
    try:
        assert [event.tick.seq for event in asyncio.run(run())] == [1, 2]
    finally:
        stream.close()


def test_stdin_pipe_rejects_oversized_unterminated_frame_before_eof():
    import os
    read_fd, write_fd = os.pipe()
    stream = os.fdopen(read_fd, 'r')
    async def run():
        source = JsonlSource(script_tf='1', stream=stream).events()
        async def writer():
            # Write in a worker so OS pipe capacity cannot block the event loop.
            await asyncio.to_thread(os.write, write_fd, b'x' * (MAX_FRAME_BYTES + 1))
        task = asyncio.create_task(writer())
        with pytest.raises(SourceError, match='one MiB'):
            await asyncio.wait_for(anext(source), timeout=2)
        await task
        await source.aclose()
    try:
        asyncio.run(run())
    finally:
        stream.close()
        os.close(write_fd)


def test_stdin_subprocess_ctrl_c_exits_while_writer_remains_open():
    import signal
    import subprocess
    import sys
    code = """import asyncio
from pineforge_live.sources.jsonl import JsonlSource
async def main():
    pending = asyncio.create_task(anext(JsonlSource(script_tf='1').events()))
    await asyncio.sleep(0)
    print('ready', flush=True)
    await pending
asyncio.run(main())
"""
    process = subprocess.Popen([sys.executable, '-c', code], stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert process.stdout.readline().strip() == 'ready'
        process.send_signal(signal.SIGINT)
        process.wait(timeout=3)
        assert process.returncode != 0
        assert not process.stdin.closed
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=3)
        process.stdin.close()
        process.stdout.close()
        process.stderr.close()


@pytest.mark.parametrize('failure',['payload','received','sent'])
def test_oversized_websocket_is_not_retried(failure,monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from pineforge_live.sources.websocket import WebSocketSource
    from pineforge_live.sources.base import SourceConfig,SourceError,MAX_FRAME_BYTES
    if failure=='payload':
        excmod=pytest.importorskip('websockets.exceptions')
        error=excmod.PayloadTooBig(MAX_FRAME_BYTES+1,MAX_FRAME_BYTES)
    else:
        error=OSError('size failure')
        setattr(error,'rcvd' if failure=='received' else 'sent',SimpleNamespace(code=1009))
    class Socket:
        async def __aenter__(self):return self
        async def __aexit__(self,*a):pass
        def __aiter__(self):return self
        async def __anext__(self):raise error
    sleeps=[]
    async def sleep(seconds):sleeps.append(seconds);raise AssertionError('must not retry oversized frame')
    monkeypatch.setattr(asyncio,'sleep',sleep)
    source=WebSocketSource(SourceConfig('websocket',url='ws://127.0.0.1:1'),script_tf='1',connect=lambda *a,**k:Socket())
    with pytest.raises(SourceError,match='one MiB'):
        asyncio.run(anext(source.events()))
    assert sleeps==[]
