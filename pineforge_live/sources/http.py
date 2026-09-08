"""Polling an operator-supplied endpoint that returns a generic event or array."""
import asyncio
import urllib.error
import urllib.request

from .base import MAX_FRAME_BYTES, SequenceTracker, SourceError, parse_frame, validate_url


class _RedirectPolicy(urllib.request.HTTPRedirectHandler):
    def __init__(self, allow_insecure):
        self.allow_insecure = allow_insecure

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_url(newurl, allow_insecure=self.allow_insecure)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class HttpSource:
    def __init__(self, config, *, script_tf, fetch=None):
        self.config, self.script_tf = config, script_tf
        self._fetch = fetch or self._request

    def _request(self):
        validate_url(self.config.url, allow_insecure=self.config.allow_insecure)
        opener = urllib.request.build_opener(_RedirectPolicy(self.config.allow_insecure))
        request = urllib.request.Request(self.config.url, headers={'Accept': 'application/json'})
        try:
            with opener.open(request, timeout=self.config.timeout_ms / 1000) as response:
                return response.read(MAX_FRAME_BYTES + 1)
        except (OSError, urllib.error.URLError):
            raise SourceError('http: request failed') from None

    async def snapshot(self, from_seq=None):
        """Read one finite polling response for a scheduled check."""
        tracker = SequenceTracker(from_seq)
        frame = await asyncio.to_thread(self._fetch)
        return [output for event in parse_frame(frame, self.script_tf)
                for output in tracker.accept(event)]

    async def events(self, from_seq=None):
        tracker = SequenceTracker(from_seq)
        while True:
            frame = await asyncio.to_thread(self._fetch)
            for event in parse_frame(frame, self.script_tf):
                for output in tracker.accept(event):
                    yield output
            await asyncio.sleep(self.config.poll_interval_ms / 1000)
