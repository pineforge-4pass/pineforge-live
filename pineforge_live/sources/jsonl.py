"""Finite JSONL files or cancellable newline-delimited stdin."""
import asyncio
import os
import stat
import sys
from pathlib import Path

from .base import MAX_FRAME_BYTES, SequenceTracker, SourceError, parse_frame


async def _descriptor_lines(fd):
    """Read ready pipe/TTY bytes without an executor worker or fd flag changes."""
    loop = asyncio.get_running_loop()
    pending = bytearray()
    while True:
        future = loop.create_future()

        def readable():
            # Remove readiness before completing the future: one read owns each
            # notification and cancellation leaves no callback holding stdin.
            loop.remove_reader(fd)
            if future.done():
                return
            try:
                future.set_result(os.read(fd, 65_536))
            except OSError as exc:
                future.set_exception(exc)

        try:
            loop.add_reader(fd, readable)
            chunk = await future
        finally:
            loop.remove_reader(fd)
        if not chunk:
            if pending:
                yield bytes(pending)
            return
        pending.extend(chunk)
        while b'\n' in pending:
            index = pending.index(b'\n')
            if index + 1 > MAX_FRAME_BYTES:
                raise SourceError('feed frame: exceeds one MiB limit')
            line = bytes(pending[:index + 1])
            del pending[:index + 1]
            yield line
        if len(pending) > MAX_FRAME_BYTES:
            raise SourceError('feed frame: exceeds one MiB limit')


async def _stream_lines(stream):
    try:
        fd = stream.fileno()
        use_descriptor = not stat.S_ISREG(os.fstat(fd).st_mode)
    except (AttributeError, OSError, ValueError):
        use_descriptor = False
    if use_descriptor:
        lines = _descriptor_lines(fd)
        try:
            async for line in lines:
                yield line
        finally:
            await lines.aclose()
        return
    # Regular files and injected StringIO cannot await an external writer.
    while True:
        line = await asyncio.to_thread(stream.readline, MAX_FRAME_BYTES + 1)
        if not line:
            return
        yield line


class JsonlSource:
    def __init__(self, path=None, *, script_tf, stream=None):
        self.path = Path(path) if path is not None else None
        self.script_tf = script_tf
        self.stream = stream

    async def events(self, from_seq=None):
        tracker = SequenceTracker(from_seq)
        try:
            stream = self.stream if self.stream is not None else (self.path.open('r', encoding='utf-8') if self.path else sys.stdin)
        except OSError:
            raise SourceError('jsonl: could not open input') from None
        close = self.stream is None and self.path is not None
        lines = _stream_lines(stream)
        try:
            async for line in lines:
                if not line.strip():
                    continue
                for event in parse_frame(line, self.script_tf):
                    for output in tracker.accept(event):
                        yield output
        except (OSError, UnicodeError):
            raise SourceError('jsonl: read failed') from None
        finally:
            await lines.aclose()
            if close:
                stream.close()
