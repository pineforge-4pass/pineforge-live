"""Immutable Cloud Run evidence IO; authentication stays in runtime memory."""
from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import tarfile
import tempfile
import time
from typing import Callable
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

_SHA = re.compile(r"[0-9a-f]{64}\Z")
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_BUCKET = re.compile(r"[a-z0-9][a-z0-9._-]{1,220}[a-z0-9]\Z")
_CHUNK = 1024 * 1024
_MAX_BYTES = 4 * 1024**3
_METADATA = (
    "http://metadata.google.internal/computeMetadata/v1/instance/"
    "service-accounts/default/token"
)


class CloudIOError(RuntimeError):
    """An input identity or immutable output receipt could not be proven."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise CloudIOError(f"refusing HTTP redirect ({code})")


def _nonnegative(value: int, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CloudIOError(f"{label} must be a nonnegative integer")
    return value


def _digest(value: str) -> str:
    if not isinstance(value, str) or _SHA.fullmatch(value) is None:
        raise CloudIOError("invalid SHA-256")
    return value


def file_identity(path: str | Path) -> dict:
    digest = hashlib.sha256()
    size = 0
    with Path(path).open("rb") as stream:
        while chunk := stream.read(_CHUNK):
            digest.update(chunk)
            size += len(chunk)
    return {"sha256": digest.hexdigest(), "bytes": size}


def canonical_json_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def _json_bytes(raw: bytes):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise CloudIOError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    def nonfinite(value):
        raise CloudIOError(f"nonfinite JSON number: {value}")

    try:
        return json.loads(raw, object_pairs_hook=pairs, parse_constant=nonfinite)
    except (UnicodeError, ValueError) as exc:
        raise CloudIOError("invalid JSON document") from exc


class GcsStore:
    """SHA-verified downloads and conditional, read-back-verified uploads.

    ``opener`` and ``token_provider`` are injectable for offline protocol tests.
    The default never invokes gcloud or reads developer credentials.
    """

    def __init__(self, bucket: str, *, opener=None,
                 token_provider: Callable[[], str] | None = None,
                 timeout: float = 60):
        if not isinstance(bucket, str) or _BUCKET.fullmatch(bucket) is None:
            raise CloudIOError("invalid evidence bucket name")
        self.bucket = bucket
        self.timeout = timeout
        self._open = opener or build_opener(_NoRedirect()).open
        self._metadata_open = build_opener(ProxyHandler({}), _NoRedirect()).open
        self._token_provider = token_provider
        self._cached_token: tuple[str, float] | None = None

    @classmethod
    def from_environment(cls) -> "GcsStore":
        bucket = os.environ.get("PINEFORGE_EVIDENCE_BUCKET", "")
        if not bucket:
            raise CloudIOError("PINEFORGE_EVIDENCE_BUCKET is required")
        return cls(bucket)

    def _token(self) -> str:
        if self._token_provider is not None:
            token = self._token_provider()
        elif self._cached_token and time.monotonic() < self._cached_token[1]:
            token = self._cached_token[0]
        else:
            request = Request(_METADATA, headers={"Metadata-Flavor": "Google"})
            try:
                with self._metadata_open(request, timeout=5) as response:
                    raw = response.read(65537)
                if len(raw) > 65536:
                    raise CloudIOError("oversized metadata token response")
                document = _json_bytes(raw)
                token = document.get("access_token") if isinstance(document, dict) else None
                lifetime = float(document.get("expires_in", 300))
            except (HTTPError, OSError, ValueError, TypeError, AttributeError) as exc:
                raise CloudIOError("runtime metadata authentication failed") from exc
            if not isinstance(token, str) or not token:
                raise CloudIOError("runtime metadata returned no OAuth token")
            self._cached_token = (token, time.monotonic() + max(1, min(lifetime - 60, 3000)))
        if not isinstance(token, str) or not token or "\n" in token or "\r" in token:
            raise CloudIOError("invalid runtime OAuth token")
        return token

    def _url(self, key: str, **query) -> str:
        url = ("https://storage.googleapis.com/storage/v1/b/"
               f"{quote(self.bucket, safe='')}/o/{quote(key, safe='')}")
        return url + ("?" + urlencode(query) if query else "")

    def _get(self, key: str, sink, *, sha256: str, size: int | None,
             max_bytes: int, generation: str | None = None) -> dict:
        _digest(sha256)
        _nonnegative(max_bytes, "max_bytes")
        if size is not None:
            _nonnegative(size, "size")
            if size > max_bytes:
                raise CloudIOError("declared input exceeds download limit")
        query = {"alt": "media"}
        if generation is not None:
            query["generation"] = generation
        request = Request(self._url(key, **query),
                          headers={"Authorization": f"Bearer {self._token()}"})
        digest = hashlib.sha256()
        seen = 0
        try:
            with self._open(request, timeout=self.timeout) as response:
                while chunk := response.read(_CHUNK):
                    seen += len(chunk)
                    if seen > max_bytes or (size is not None and seen > size):
                        raise CloudIOError("download exceeds declared byte limit")
                    digest.update(chunk)
                    sink.write(chunk)
        except HTTPError as exc:
            raise CloudIOError(f"GCS read failed (HTTP {exc.code})") from exc
        if digest.hexdigest() != sha256 or (size is not None and seen != size):
            raise CloudIOError("download identity mismatch")
        return {"sha256": sha256, "bytes": seen}

    def download(self, sha256: str, destination: str | Path, *,
                 size: int | None = None, max_bytes: int = _MAX_BYTES) -> dict:
        """Create destination exclusively after validating the complete object."""
        _digest(sha256)
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists() or destination.is_symlink():
            raise CloudIOError("download destination already exists")
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as stream:
                temporary = Path(stream.name)
                identity = self._get(f"sha256/{sha256}", stream, sha256=sha256,
                                     size=size, max_bytes=max_bytes)
                stream.flush()
                os.fsync(stream.fileno())
            os.link(temporary, destination)  # atomic creation; never replace an existing file
            return identity
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def read_json(self, sha256: str, *, size: int | None = None,
                  max_bytes: int = 16 * 1024**2):
        sink = io.BytesIO()
        self._get(f"sha256/{_digest(sha256)}", sink, sha256=sha256,
                  size=size, max_bytes=max_bytes)
        return _json_bytes(sink.getvalue())

    def upload_output(self, source: str | Path, *, run_id: str, task: int,
                      name: str, content_type: str = "application/octet-stream") -> dict:
        """Create one immutable output and prove exact remote byte readback.

        HTTP 412 is accepted only when the existing object's bytes match.
        """
        if not isinstance(run_id, str) or _NAME.fullmatch(run_id) is None:
            raise CloudIOError("invalid run_id")
        _nonnegative(task, "task")
        if not isinstance(name, str) or _NAME.fullmatch(name) is None:
            raise CloudIOError("invalid artifact name")
        source = Path(source)
        identity = file_identity(source)
        key = f"live-verification/{run_id}/{task}/{name}"
        query = urlencode({"uploadType": "media", "name": key, "ifGenerationMatch": "0"})
        url = ("https://storage.googleapis.com/upload/storage/v1/b/"
               f"{quote(self.bucket, safe='')}/o?{query}")
        generation = None
        with source.open("rb") as stream:
            request = Request(url, data=stream, method="POST", headers={
                "Authorization": f"Bearer {self._token()}",
                "Content-Type": content_type,
                "Content-Length": str(identity["bytes"]),
            })
            try:
                with self._open(request, timeout=self.timeout) as response:
                    raw = response.read(1024 * 1024 + 1)
                if len(raw) > 1024 * 1024:
                    raise CloudIOError("oversized GCS upload receipt")
                document = _json_bytes(raw)
                if not isinstance(document, dict) or not str(document.get("generation", "")).isdigit():
                    raise CloudIOError("GCS upload receipt has no generation")
                generation = str(document["generation"])
            except HTTPError as exc:
                if exc.code != 412:
                    raise CloudIOError(f"GCS upload failed (HTTP {exc.code})") from exc
        # Discard bytes while independently hashing the entire stored object.
        with open(os.devnull, "wb") as sink:
            self._get(key, sink, sha256=identity["sha256"], size=identity["bytes"],
                      max_bytes=identity["bytes"], generation=generation)
        return {"uri": f"gs://{self.bucket}/{key}", "key": key,
                **identity, "generation": generation, "readback_verified": True}


def task_coordinates(environ=None) -> tuple[int, int]:
    env = os.environ if environ is None else environ
    try:
        raw_index = env.get("CLOUD_RUN_TASK_INDEX", "0")
        raw_count = env.get("CLOUD_RUN_TASK_COUNT", "1")
        if not re.fullmatch(r"0|[1-9][0-9]*", raw_index):
            raise ValueError
        if not re.fullmatch(r"[1-9][0-9]*", raw_count):
            raise ValueError
        index, count = int(raw_index), int(raw_count)
        if not 0 <= index < count:
            raise ValueError
        return index, count
    except (ValueError, TypeError) as exc:
        raise CloudIOError("invalid CLOUD_RUN_TASK_INDEX/COUNT") from exc


def safe_extract_tar(archive: str | Path, destination: str | Path, *,
                     max_bytes: int = _MAX_BYTES, max_members: int = 100000) -> None:
    """Extract regular files/directories into a new root; links are refused.

    Validate the full member set before writing anything. Device nodes, FIFOs,
    duplicate paths, traversal, absolute paths, and links are never materialized.
    """
    _nonnegative(max_bytes, "max_bytes")
    _nonnegative(max_members, "max_members")
    destination = Path(destination)
    if destination.exists() or destination.is_symlink():
        raise CloudIOError("archive destination must be new")
    with tarfile.open(archive, mode="r:*") as tar:
        members = []
        names: dict[PurePosixPath, bool] = {}
        total = 0
        for member in tar:
            if len(members) >= max_members:
                raise CloudIOError("archive has too many members")
            raw = member.name.rstrip("/")
            path = PurePosixPath(raw)
            if (not raw or "\\" in raw or path.is_absolute()
                    or any(part in {"", ".", ".."} for part in raw.split("/"))
                    or not (member.isfile() or member.isdir()) or path in names):
                raise CloudIOError("unsafe archive member")
            _nonnegative(member.size, "archive member size")
            total += member.size
            if total > max_bytes:
                raise CloudIOError("archive exceeds extraction byte limit")
            names[path] = member.isdir()
            members.append((member, path))
        for path in names:
            if any(parent in names and not names[parent] for parent in path.parents):
                raise CloudIOError("archive file used as parent directory")
        destination.mkdir(parents=True, exist_ok=False)
        for member, path in members:
            target = destination.joinpath(*path.parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with tar.extractfile(member) as source, target.open("xb") as output:
                remaining = member.size
                while remaining:
                    chunk = source.read(min(_CHUNK, remaining))
                    if not chunk:
                        raise CloudIOError("truncated archive member")
                    output.write(chunk)
                    remaining -= len(chunk)
            target.chmod(0o755 if member.mode & 0o111 else 0o644)
