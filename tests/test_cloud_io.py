"""Offline protocol checks: no GCP access and no strategy measurements."""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile
from urllib.error import HTTPError
from urllib.parse import parse_qs, unquote, urlparse

import pytest

from pineforge_live.verification.cloud_io import (
    CloudIOError, GcsStore, canonical_json_bytes, safe_extract_tar, task_coordinates,
)


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


class FakeGcs:
    def __init__(self):
        self.objects = {}
        self.requests = []
        self.corrupt_readback = False

    def open(self, request, timeout):
        self.requests.append(request)
        url = urlparse(request.full_url)
        query = parse_qs(url.query)
        assert request.get_header("Authorization") == "Bearer fake-runtime-token"
        if request.get_method() == "POST":
            key = query["name"][0]
            assert query["ifGenerationMatch"] == ["0"]
            if key in self.objects:
                raise HTTPError(request.full_url, 412, "exists", {}, None)
            self.objects[key] = request.data.read()
            return io.BytesIO(b'{"generation":"123"}')
        key = unquote(url.path.split("/o/", 1)[1])
        if key not in self.objects:
            raise HTTPError(request.full_url, 404, "missing", {}, None)
        value = self.objects[key]
        if self.corrupt_readback and key.startswith("live-verification/"):
            value += b"changed"
        return io.BytesIO(value)

    def store(self):
        return GcsStore("test-evidence", opener=self.open,
                        token_provider=lambda: "fake-runtime-token")


def test_download_checks_hash_size_and_never_replaces(tmp_path):
    fake = FakeGcs()
    raw = b"evidence\n" * 10000
    fake.objects[f"sha256/{_sha(raw)}"] = raw
    output = tmp_path / "feed.csv"
    assert fake.store().download(_sha(raw), output, size=len(raw)) == {
        "sha256": _sha(raw), "bytes": len(raw)}
    assert output.read_bytes() == raw
    with pytest.raises(CloudIOError, match="already exists"):
        fake.store().download(_sha(raw), output)
    with pytest.raises(CloudIOError, match="byte limit"):
        fake.store().download(_sha(raw), tmp_path / "short", size=len(raw)-1)
    assert not (tmp_path / "short").exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["feed.csv"]


def test_download_hash_mismatch_and_absent_object_leave_no_file(tmp_path):
    fake = FakeGcs()
    fake.objects[f"sha256/{'0'*64}"] = b"wrong"
    with pytest.raises(CloudIOError, match="identity mismatch"):
        fake.store().download("0"*64, tmp_path / "bad")
    with pytest.raises(CloudIOError, match="HTTP 404"):
        fake.store().download("1"*64, tmp_path / "absent")
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("raw", [b'{"a":1,"a":2}', b'{"a":NaN}'])
def test_manifest_parser_refuses_ambiguous_json(raw):
    fake = FakeGcs()
    fake.objects[f"sha256/{_sha(raw)}"] = raw
    with pytest.raises(CloudIOError):
        fake.store().read_json(_sha(raw))


def test_conditional_upload_readback_and_conflicting_existing_object(tmp_path):
    fake = FakeGcs()
    source = tmp_path / "result.json"
    source.write_bytes(canonical_json_bytes({"ok": True}))
    kwargs = {"run_id": "review-123", "task": 0, "name": "result.json"}
    receipt = fake.store().upload_output(source, **kwargs)
    assert receipt["readback_verified"] is True
    assert receipt["generation"] == "123"
    assert receipt["key"] == "live-verification/review-123/0/result.json"
    assert "generation=123" in fake.requests[-1].full_url
    # 412 is safe only after comparing all remote bytes.
    assert fake.store().upload_output(source, **kwargs)["readback_verified"]
    source.write_bytes(b"different")
    with pytest.raises(CloudIOError):
        fake.store().upload_output(source, **kwargs)


def test_successful_upload_with_bad_readback_is_not_success(tmp_path):
    fake = FakeGcs()
    fake.corrupt_readback = True
    source = tmp_path / "receipt"
    source.write_bytes(b"abc")
    with pytest.raises(CloudIOError):
        fake.store().upload_output(source, run_id="test", task=0, name="receipt")


@pytest.mark.parametrize("kwargs", [
    {"run_id": "../campaign", "task": 0, "name": "x"},
    {"run_id": "valid", "task": -1, "name": "x"},
    {"run_id": "valid", "task": True, "name": "x"},
    {"run_id": "valid", "task": 0, "name": "../sha256/x"},
])
def test_output_scope_rejected_before_authentication(tmp_path, kwargs):
    with pytest.raises(CloudIOError):
        FakeGcs().store().upload_output(tmp_path / "nonexistent", **kwargs)


def _tar(path, entries):
    with tarfile.open(path, "w") as tar:
        for name, kind, data in entries:
            member = tarfile.TarInfo(name)
            member.type = kind
            member.size = len(data) if kind == tarfile.REGTYPE else 0
            member.linkname = "../../outside" if kind in {tarfile.SYMTYPE, tarfile.LNKTYPE} else ""
            member.mode = 0o755
            tar.addfile(member, io.BytesIO(data) if kind == tarfile.REGTYPE else None)


def test_safe_tar_extraction_preserves_files_and_executable_mode(tmp_path):
    archive = tmp_path / "source.tar"
    _tar(archive, [("tree", tarfile.DIRTYPE, b""),
                   ("tree/run.py", tarfile.REGTYPE, b"print(1)\n")])
    destination = tmp_path / "unpacked"
    safe_extract_tar(archive, destination)
    assert (destination / "tree/run.py").read_bytes() == b"print(1)\n"
    assert (destination / "tree/run.py").stat().st_mode & 0o111


@pytest.mark.parametrize("entries", [
    [("../outside", tarfile.REGTYPE, b"x")],
    [("/absolute", tarfile.REGTYPE, b"x")],
    [("a/../../outside", tarfile.REGTYPE, b"x")],
    [("a", tarfile.SYMTYPE, b"")],
    [("a", tarfile.LNKTYPE, b"")],
    [("a", tarfile.FIFOTYPE, b"")],
    [("same", tarfile.REGTYPE, b"1"), ("same", tarfile.REGTYPE, b"2")],
    [("a", tarfile.REGTYPE, b"1"), ("a/b", tarfile.REGTYPE, b"2")],
])
def test_unsafe_archive_refused_before_destination_created(tmp_path, entries):
    archive = tmp_path / "source.tar"
    _tar(archive, entries)
    destination = tmp_path / "unpacked"
    with pytest.raises(CloudIOError):
        safe_extract_tar(archive, destination)
    assert not destination.exists()


def test_archive_expansion_limit(tmp_path):
    archive = tmp_path / "source.tar"
    _tar(archive, [("a", tarfile.REGTYPE, b"too large")])
    with pytest.raises(CloudIOError, match="byte limit"):
        safe_extract_tar(archive, tmp_path / "unpacked", max_bytes=2)


def test_task_coordinates():
    assert task_coordinates({}) == (0, 1)
    assert task_coordinates({"CLOUD_RUN_TASK_INDEX": "2", "CLOUD_RUN_TASK_COUNT": "3"}) == (2, 3)
    for env in ({"CLOUD_RUN_TASK_COUNT": "0"}, {"CLOUD_RUN_TASK_INDEX": "1"},
                {"CLOUD_RUN_TASK_INDEX": "-1"}, {"CLOUD_RUN_TASK_COUNT": "1.0"}):
        with pytest.raises(CloudIOError):
            task_coordinates(env)


def _module(name):
    filename = Path(__file__).resolve().parents[1] / "cloudrun" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_job_spec_refuses_shared_names_and_unpinned_image():
    render = _module("job_spec").job_spec
    kwargs = dict(name="pineforge-live-review-test", image="example/image@sha256:" + "a"*64,
                  service_account="runner@project.iam.gserviceaccount.com",
                  bucket="test-evidence", manifest_sha256="b"*64)
    spec = render(**kwargs)
    inner = spec["spec"]["template"]["spec"]["template"]["spec"]
    assert inner["maxRetries"] == 0
    assert "volumes" not in inner
    for override in ({"name": "pineforge-case-runner"}, {"image": "image:latest"}, {"tasks": 9}):
        with pytest.raises(ValueError):
            render(**{**kwargs, **override})


def test_source_packet_contains_exact_commit_and_excludes_untracked(tmp_path):
    package = _module("package_review").package_sources
    repo = tmp_path / "repo"
    repo.mkdir()
    def git(*args):
        return subprocess.run(["git", "-C", str(repo), *args], check=True,
                              capture_output=True, text=True).stdout.strip()
    git("init", "-q")
    (repo / "source.py").write_text("value = 1\n")
    git("add", "source.py")
    git("-c", "user.name=Test", "-c", "user.email=test@example.test",
        "commit", "-qm", "fixture")
    commit = git("rev-parse", "HEAD")
    (repo / ".env").write_text("untracked-secret-test-marker")
    output = tmp_path / "packet"
    result = package({"engine": str(repo)}, {"engine": commit}, output)
    source = result["sources"]["engine"]
    assert source["commit"] == commit
    assert source["tree"] == git("rev-parse", "HEAD^{tree}")
    assert source["sha256"] == _sha((output / "engine.bundle").read_bytes())
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "--no-checkout", str(output / "engine.bundle"), str(clone)],
                   check=True, capture_output=True)
    shown = subprocess.run(["git", "-C", str(clone), "show", f"{commit}:source.py"],
                           check=True, capture_output=True, text=True)
    assert shown.stdout == "value = 1\n"
    assert not (clone / ".env").exists()
