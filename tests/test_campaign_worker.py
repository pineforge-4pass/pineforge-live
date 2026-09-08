"""Worker controller tests with fake builds/probes/storage; no measurements."""
from __future__ import annotations

import copy
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tarfile

import pytest

from pineforge_live.verification import campaign_worker as worker
from pineforge_live.verification.cloud_io import canonical_json_bytes, file_identity


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def descriptor(raw):
    return {"sha256": sha(raw), "bytes": len(raw)}


def manifest():
    source = {**descriptor(b"unused-bundle"), "commit": "a"*40, "tree": "b"*40}
    return {"schemaVersion": "pineforge-live-two-input-run/v1", "runId": "unit-review",
            "liveCommit": "c"*40, "sources": {name: dict(source) for name in ("engine", "codegen", "lab")},
            "probePacket": {**descriptor(b"packet"), "manifestSha256": "d"*64},
            "probeIds": ["p2", "p0"], "tickPolicies": ["high-first", "low-first"]}


class Store:
    def __init__(self):
        self.objects = {}
        self.uploads = {}
        self.fail_case_upload = False

    def download(self, digest, path, *, size):
        raw = self.objects[digest]
        assert sha(raw) == digest and len(raw) == size
        Path(path).write_bytes(raw)
        return descriptor(raw)

    def upload_output(self, path, *, run_id, task, name, content_type):
        if self.fail_case_upload and name.startswith("case-"):
            raise RuntimeError("fake readback failure")
        self.uploads[name] = Path(path).read_bytes()
        return {"uri": f"gs://fake/live-verification/{run_id}/{task}/{name}",
                **file_identity(path), "readback_verified": True}


def packet_fixture(store):
    files = {}
    def evidence(raw, kind):
        d = descriptor(raw)
        d["path"] = f"evidence/{d['sha256']}"
        files[d["path"]] = raw
        return d
    inputs = {"strategy": evidence(b"strategy fixture", "strategy"),
              "tvTrades": evidence(b"trades fixture", "tvTrades"),
              "metrics": evidence(b"{}", "metrics")}
    probes = [{"probe_id": f"p{i}", "symbol": "EXAMPLE:PAIR", "timeframe": "15",
               "lane": "lane", "group": "standard",
               "strategy_sha256": inputs["strategy"]["sha256"],
               "tv_trades_sha256": inputs["tvTrades"]["sha256"],
               "metrics_sha256": inputs["metrics"]["sha256"],
               "evidence": copy.deepcopy(inputs)} for i in range(3)]
    feed_raw = b"minute feed fixture"
    feed = {**descriptor(feed_raw), "parts": [descriptor(feed_raw)]}
    store.objects[sha(feed_raw)] = feed_raw
    packet = {"schemaVersion": "pineforge-campaign-input-packet/v1", "registryReadOnly": True,
              "probes": probes, "templates": [{"lane": "lane", "group": "standard",
               "symbol": "EXAMPLE:PAIR", "script_tf": "15", "chart": feed, "finer": feed,
               "environment": {}}], "population": {"sha256": "f"*64}, "baseline": {"id": "baseline"}}
    raw_manifest = canonical_json_bytes(packet)
    files["manifest.json"] = raw_manifest
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, raw in files.items():
            member = tarfile.TarInfo(name)
            member.size = len(raw)
            tar.addfile(member, io.BytesIO(raw))
    raw_packet = buffer.getvalue()
    store.objects[sha(raw_packet)] = raw_packet
    return {**descriptor(raw_packet), "manifestSha256": sha(raw_manifest)}


def fake_environment(monkeypatch):
    monkeypatch.setenv("PINEFORGE_LIVE_BUILD_COMMIT", "c"*40)
    monkeypatch.setenv("PINEFORGE_LIVE_IMAGE_DIGEST", "sha256:"+"e"*64)
    def checkout(store, source, root):
        root.mkdir()
        return {"commit": source["commit"], "tree": source["tree"]}
    monkeypatch.setattr(worker, "checkout", checkout)
    monkeypatch.setattr(worker, "_run", lambda *args, **kwargs: "fake compiler")


def successful_case(command, *, stdout_path, stderr_path, timeout):
    config = json.loads(Path(command[-1]).read_text())
    Path(stdout_path).write_text("fake case\n")
    Path(stderr_path).write_text("")
    report = {"status": "passed", "live_backtest_equal": True, "batch_actions_in_window": 1,
              "modes": {name: {"ok": True} for name in
                        ["bars-direct", *("ticks-"+p for p in config["tick_policies"])]}}
    (Path(config["output"])/"result.json").write_bytes(canonical_json_bytes(report))
    return 0


@pytest.mark.parametrize("override", [
    {"runId": "../outside"}, {"probeIds": []}, {"probeIds": ["x", "x"]},
    {"tickPolicies": []}, {"tickPolicies": ["unknown"]}, {"replayBars": True},
    {"caseTimeoutSeconds": 3601}, {"liveCommit": "HEAD"},
])
def test_manifest_refusal_before_side_effects(override):
    with pytest.raises(ValueError):
        worker.validate_manifest({**manifest(), **override}, 0, 1)


def test_ordered_manifest_selection_is_not_expanded_by_packet():
    packet = {"schemaVersion": "pineforge-campaign-input-packet/v1", "registryReadOnly": True,
              "probes": [{"probe_id": f"p{i}"} for i in range(3)]}
    selected = worker.select_packet_probes(packet, manifest(), ["p2", "p0"])
    assert [p["probe_id"] for p in selected] == ["p2", "p0"]
    with pytest.raises(ValueError, match="missing"):
        worker.select_packet_probes(packet, {**manifest(), "probeIds": ["absent"]}, ["absent"])
    packet["probes"].append({"probe_id": "p0"})
    with pytest.raises(ValueError, match="duplicate"):
        worker.select_packet_probes(packet, manifest(), ["p0"])


def test_feed_cache_is_reverified_and_failed_assembly_never_cached(tmp_path):
    store = Store()
    raw = b"firstsecond"
    parts = [b"first", b"second"]
    for part in parts:
        store.objects[sha(part)] = part
    desc = {**descriptor(raw), "parts": [descriptor(p) for p in parts]}
    assert worker.fetch_feed(store, desc, tmp_path).read_bytes() == raw
    (tmp_path/(sha(raw)+".csv")).unlink()
    (tmp_path/(sha(parts[0])+".part")).write_bytes(b"changed")
    with pytest.raises(RuntimeError, match="cached feed part"):
        worker.fetch_feed(store, desc, tmp_path)
    assert not (tmp_path/(sha(raw)+".csv")).exists()


def test_worker_only_runs_selected_ids_and_cleans_verified_artifacts(tmp_path, monkeypatch):
    fake_environment(monkeypatch)
    store = Store()
    run = manifest()
    run["probePacket"] = packet_fixture(store)
    monkeypatch.setattr(worker, "run_process", successful_case)
    workspace = tmp_path/"worker"
    assert worker.run_task(run, store, 0, 1, workspace) == 0
    result = json.loads(store.uploads["results.json"])
    assert [r["probeId"] for r in result["results"]] == ["p2", "p0"]
    assert result["summary"]["assigned"] == result["summary"]["measured"] == 2
    assert result["ok"] is True
    assert set(store.uploads) == {"case-0.tar.gz", "case-1.tar.gz", "results.json"}
    assert not (workspace/"case-0").exists()
    assert not (workspace/"case-0.tar.gz").exists()


def test_source_image_mismatch_generates_failed_receipt_without_build(tmp_path, monkeypatch):
    fake_environment(monkeypatch)
    monkeypatch.setenv("PINEFORGE_LIVE_BUILD_COMMIT", "0"*40)
    store = Store()
    assert worker.run_task(manifest(), store, 0, 1, tmp_path/"worker") == 1
    result = json.loads(store.uploads["results.json"])
    assert "live commit" in result["setupError"]
    assert result["summary"]["measured"] == 0
    assert result["ok"] is False


def test_evidence_upload_failure_cannot_count_pass(tmp_path, monkeypatch):
    fake_environment(monkeypatch)
    store = Store()
    store.fail_case_upload = True
    run = manifest()
    run["probePacket"] = packet_fixture(store)
    monkeypatch.setattr(worker, "run_process", successful_case)
    assert worker.run_task(run, store, 0, 1, tmp_path/"worker") == 1
    result = json.loads(store.uploads["results.json"])
    assert result["results"][0]["status"] == "unmeasured"
    assert result["summary"]["measured"] == 0
    assert result["setupError"] is not None


def test_timeout_keeps_failure_logs_and_continues_next_probe(tmp_path, monkeypatch):
    fake_environment(monkeypatch)
    store = Store()
    run = manifest()
    run["probePacket"] = packet_fixture(store)
    attempts = []
    def timeout_then_pass(command, **kwargs):
        attempts.append(command)
        if len(attempts) == 1:
            Path(kwargs["stdout_path"]).write_text("partial diagnostic")
            Path(kwargs["stderr_path"]).write_text("stalled")
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        return successful_case(command, **kwargs)
    monkeypatch.setattr(worker, "run_process", timeout_then_pass)
    assert worker.run_task(run, store, 0, 1, tmp_path/"worker") == 1
    result = json.loads(store.uploads["results.json"])
    assert [r["status"] for r in result["results"]] == ["unmeasured", "passed"]
    with tarfile.open(fileobj=io.BytesIO(store.uploads["case-0.tar.gz"])) as tar:
        assert tar.extractfile("stdout.log").read() == b"partial diagnostic"


def test_pass_requires_all_modes_and_cannot_replace_identity():
    entry = {"probeId": "p"}
    with pytest.raises(RuntimeError, match="every requested mode"):
        worker._merge_report(entry, {"status": "passed", "live_backtest_equal": True,
                                    "modes": {"bars-direct": {"ok": True}}}, 0, ["high-first"])
    with pytest.raises(RuntimeError, match="identity"):
        worker._merge_report(entry, {"status": "failed", "probeId": "other"}, 1, [])


@pytest.mark.parametrize("count", [0, -1, None, True])
def test_pass_requires_nonempty_order_action_evidence(count):
    report = {"status": "passed", "live_backtest_equal": True,
              "batch_actions_in_window": count,
              "modes": {name: {"ok": True} for name in ("bars-direct", "ticks-high-first")}}
    with pytest.raises(RuntimeError, match="every requested mode"):
        worker._merge_report({"probeId": "p"}, report, 0, ["high-first"])


def test_process_timeout_kills_process_group_and_waits(monkeypatch, tmp_path):
    events = []
    class Process:
        pid = 12345
        def wait(self, timeout=None):
            events.append(("wait", timeout))
            if timeout is not None:
                raise subprocess.TimeoutExpired(["fake"], timeout)
            return -9
    def popen(command, **kwargs):
        assert kwargs["start_new_session"] is True
        return Process()
    monkeypatch.setattr(worker.subprocess, "Popen", popen)
    monkeypatch.setattr(worker.os, "killpg", lambda pid, sig: events.append(("killpg", pid)))
    with pytest.raises(subprocess.TimeoutExpired):
        worker.run_process(["fake"], stdout_path=tmp_path/"out", stderr_path=tmp_path/"err", timeout=7)
    assert events == [("wait", 7), ("killpg", 12345), ("wait", None)]


def test_local_main_refused_before_authentication(monkeypatch):
    monkeypatch.delenv("CLOUD_RUN_TASK_INDEX", raising=False)
    with pytest.raises(RuntimeError, match="Cloud Run"):
        worker.main()
