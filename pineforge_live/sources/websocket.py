"""Generic WebSocket frames, reconnecting without inventing a venue protocol.

No subscribe frame or from_seq query is sent. from_seq filters locally and
sequence gaps are emitted as unhealed TickGap events for the runtime.
"""
import asyncio

from .base import MAX_FRAME_BYTES, SequenceTracker, SourceError, parse_frame, validate_url


class WebSocketSource:
    def __init__(self, config, *, script_tf, connect=None):
        self.config, self.script_tf, self._connect = config, script_tf, connect

    async def events(self, from_seq=None):
        connect = self._connect
        if connect is None:
            try:
                from websockets.asyncio.client import connect
            except ImportError:
                raise SourceError('websocket: install the websocket optional dependency') from None
        tracker = SequenceTracker(from_seq)
        backoff = self.config.reconnect_initial_ms
        while True:
            validate_url(self.config.url, schemes=('wss', 'ws'), allow_insecure=self.config.allow_insecure)
            try:
                async with connect(self.config.url, open_timeout=self.config.timeout_ms / 1000,
                                   max_size=MAX_FRAME_BYTES) as socket:
                    async for frame in socket:
                        events = parse_frame(frame, self.script_tf)
                        backoff = self.config.reconnect_initial_ms
                        for event in events:
                            for output in tracker.accept(event):
                                yield output
            except (SourceError, asyncio.CancelledError):
                raise
            except Exception as exc:
                oversized=(type(exc).__module__.startswith('websockets.') and
                           any(cls.__name__=='PayloadTooBig' for cls in type(exc).__mro__))
                oversized=oversized or any(getattr(getattr(exc,part,None),'code',None)==1009
                                           for part in ('rcvd','sent'))
                if oversized:
                    raise SourceError('feed frame: exceeds one MiB limit') from None
                # Retry transport failures, never malformed generic feed frames.
                # Exception details can include sensitive endpoint query strings.
                if not isinstance(exc, (OSError, TimeoutError)) and not type(exc).__module__.startswith('websockets.'):
                    raise SourceError('websocket: connection failed') from None
            await asyncio.sleep(backoff / 1000)
            backoff = min(backoff * 2, self.config.reconnect_max_ms)
