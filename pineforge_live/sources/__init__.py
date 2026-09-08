"""Operator-supplied broker-neutral market data."""
from .base import SourceConfig, SourceError, load_history, parse_bar, parse_event, parse_frame
from .jsonl import JsonlSource


def create_source(config: SourceConfig, script_tf: str, *, input_tf: str | None = None):
    # The transport parses incoming bars at the supplied input timeframe;
    # warmup history remains in the strategy's timeframe.
    feed_tf = input_tf or script_tf
    if config.kind in {'stdin', 'jsonl'}:
        return JsonlSource(config.path, script_tf=feed_tf)
    if config.kind == 'http':
        from .http import HttpSource
        return HttpSource(config, script_tf=feed_tf)
    if config.kind == 'websocket':
        from .websocket import WebSocketSource
        return WebSocketSource(config, script_tf=feed_tf)
    raise SourceError('source.kind: unsupported source')


__all__ = ['SourceConfig', 'SourceError', 'JsonlSource', 'create_source', 'load_history', 'parse_bar', 'parse_event', 'parse_frame']
