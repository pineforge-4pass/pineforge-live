"""Isolated Cloud Run worker; registry snapshots are immutable inputs only."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time

from .cloud_io import (
    GcsStore, canonical_json_bytes, file_identity, safe_extract_tar, task_coordinates,
)

_SHA = re.compile(r"[0-9a-f]{64}\Z")
_COMMIT = re.compile(r"[0-9a-f]{40}\Z")
_RUN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_POLICIES = {"high-first", "low-first", "seeded"}
_STATUSES = {"passed", "failed", "unsupported", "unmeasured", "data-mismatch"}
_MAX_OBJECT_BYTES = 4 * 1024**3


def _integer(value, name, minimum=0, maximum=2**63-1):
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"invalid {name}")
    return value


def _sha(value, name="SHA-256"):
    if not isinstance(value, str) or not _SHA.fullmatch(value):
        raise ValueError(f"invalid {name}")
    return value


def _descriptor(value, *, source=False):
    if not isinstance(value, dict):
        raise ValueError("artifact descriptor must be an object")
    _sha(value.get("sha256"))
    _integer(value.get("bytes"), "artifact byte length", maximum=_MAX_OBJECT_BYTES)
    if source:
        for key in ("commit",):
            if not isinstance(value.get(key), str) or not _COMMIT.fullmatch(value[key]):
                raise ValueError(f"source requires exact {key}")
        if value.get("tree") is not None and not _COMMIT.fullmatch(str(value["tree"])):
            raise ValueError("invalid optional source tree")
        if value.get("format", "git-bundle") != "git-bundle":
            raise ValueError("source must use a Git bundle")
    return value


def validate_manifest(manifest, index, count):
    if not isinstance(manifest, dict) or manifest.get("schemaVersion") != "pineforge-live-two-input-run/v1":
        raise ValueError("unsupported live verification manifest")
    run_id = manifest.get("runId")
    if not isinstance(run_id, str) or not _RUN.fullmatch(run_id):
        raise ValueError("invalid runId")
    _integer(count, "task count", 1, 8)
    _integer(index, "task index", 0, count-1)
    live_commit = manifest.get("liveCommit")
    if not isinstance(live_commit, str) or not _COMMIT.fullmatch(live_commit):
        raise ValueError("liveCommit must be an exact commit")
    ids = manifest.get("probeIds")
    if (not isinstance(ids, list) or not 1 <= len(ids) <= 10000
            or any(not isinstance(p, str) or not p or len(p) > 512 for p in ids)
            or len(set(ids)) != len(ids)):
        raise ValueError("probeIds must be a nonempty unique bounded list")
    for name in ("engine", "codegen", "lab"):
        _descriptor(manifest.get("sources", {}).get(name), source=True)
    packet = _descriptor(manifest.get("probePacket"))
    _sha(packet.get("manifestSha256"), "packet manifest SHA-256")
    policies = manifest.get("tickPolicies", ["high-first", "low-first"])
    if (not isinstance(policies, list) or not policies
            or any(not isinstance(p, str) or p not in _POLICIES for p in policies)
            or len(set(policies)) != len(policies)):
        raise ValueError("tickPolicies must select unique supported policies")
    _integer(manifest.get("replayBars", 16), "replayBars", 1, 10000)
    _integer(manifest.get("dailyReplayBars", 2), "dailyReplayBars", 1, 1000)
    _integer(manifest.get("caseTimeoutSeconds", 900), "caseTimeoutSeconds", 1, 3600)
    _integer(manifest.get("seed", 20260909), "seed", -(2**63), 2**63-1)
    if manifest.get("buildProfile", "engine-default-v1") != "engine-default-v1":
        raise ValueError("this worker supports engine-default-v1 only")
    return ids[index::count]


def _tail(path, limit=6000):
    with Path(path).open("rb") as stream:
        stream.seek(0, 2)
        stream.seek(max(0, stream.tell()-limit))
        return stream.read().decode("utf-8", errors="replace")


def run_process(command, *, stdout_path, stderr_path, cwd=None, timeout=900):
    """Bound process lifetime, retain streamed logs, kill descendants on timeout."""
    with Path(stdout_path).open("wb") as stdout, Path(stderr_path).open("wb") as stderr:
        process = subprocess.Popen(command, cwd=cwd, stdout=stdout, stderr=stderr,
                                   start_new_session=True)
        try:
            return process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            raise


def _run(command, *, cwd=None, timeout=900):
    with tempfile.TemporaryDirectory(prefix="pineforge-worker-command-") as scratch:
        stdout, stderr = Path(scratch)/"stdout", Path(scratch)/"stderr"
        status = run_process(command, cwd=cwd, timeout=timeout,
                             stdout_path=stdout, stderr_path=stderr)
        if status:
            raise RuntimeError(f"{Path(command[0]).name} failed ({status}): {_tail(stderr)}")
        return _tail(stdout, 1024*1024).strip()


def checkout(store, descriptor, root):
    """Only a verified Git bundle and declared commit/tree can supply code."""
    _descriptor(descriptor, source=True)
    root = Path(root)
    bundle = root.with_suffix(".bundle")
    store.download(descriptor["sha256"], bundle, size=descriptor["bytes"])
    _run(["git", "clone", "--no-checkout", "--", str(bundle), str(root)])
    _run(["git", "-C", str(root), "checkout", "--detach", descriptor["commit"]])
    if _run(["git", "-C", str(root), "rev-parse", "HEAD"]) != descriptor["commit"]:
        raise RuntimeError("source commit readback mismatch")
    tree = _run(["git", "-C", str(root), "rev-parse", "HEAD^{tree}"])
    if descriptor.get("tree") is not None and tree != descriptor["tree"]:
        raise RuntimeError("source tree readback mismatch")
    return {"commit": descriptor["commit"], "tree": tree, **file_identity(bundle)}


def fetch_feed(store, descriptor, cache):
    _descriptor(descriptor)
    cache = Path(cache)
    identity = {"sha256": descriptor["sha256"], "bytes": descriptor["bytes"]}
    target = cache/(descriptor["sha256"]+".csv")
    if target.exists():
        if target.is_symlink() or file_identity(target) != identity:
            raise RuntimeError("cached feed identity mismatch")
        return target
    parts = descriptor.get("parts")
    if not isinstance(parts, list) or not parts:
        raise ValueError("feed requires ordered parts")
    for part in parts:
        _descriptor(part)
    if sum(part["bytes"] for part in parts) != descriptor["bytes"]:
        raise ValueError("feed part byte total mismatch")
    pieces = []
    for part in parts:
        path = cache/(part["sha256"]+".part")
        expected = {"sha256": part["sha256"], "bytes": part["bytes"]}
        if path.exists():
            if path.is_symlink() or file_identity(path) != expected:
                raise RuntimeError("cached feed part identity mismatch")
        else:
            store.download(part["sha256"], path, size=part["bytes"])
        pieces.append(path)
    temporary = target.with_suffix(".assembling")
    try:
        with temporary.open("xb") as output:
            for piece in pieces:
                with piece.open("rb") as source:
                    shutil.copyfileobj(source, output, length=1024*1024)
        if file_identity(temporary) != identity:
            raise RuntimeError("assembled feed identity mismatch")
        os.link(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def select_packet_probes(packet, manifest, assigned):
    if not isinstance(packet, dict) or packet.get("schemaVersion") != "pineforge-campaign-input-packet/v1":
        raise ValueError("unsupported campaign input packet")
    if packet.get("registryReadOnly") is not True:
        raise ValueError("packet lacks read-only registry provenance")
    probes = packet.get("probes")
    if not isinstance(probes, list) or not probes:
        raise ValueError("packet contains no probes")
    by_id = {}
    for probe in probes:
        probe_id = probe.get("probe_id") if isinstance(probe, dict) else None
        if not isinstance(probe_id, str) or not probe_id or probe_id in by_id:
            raise ValueError("duplicate or invalid packet probe identity")
        by_id[probe_id] = probe
    if any(probe_id not in by_id for probe_id in manifest["probeIds"]):
        raise ValueError("requested probe missing from packet")
    # A larger registry export never expands the manifest's ordered selection.
    return [by_id[probe_id] for probe_id in assigned]


def _templates(packet):
    result = {}
    for template in packet.get("templates", []):
        key = (template["lane"], template["group"])
        if key in result:
            raise ValueError("duplicate lane template")
        result[key] = template
    return result


def _evidence_paths(probe, packet_root):
    result = {}
    expected = {"strategy": "strategy_sha256", "tvTrades": "tv_trades_sha256",
                "metrics": "metrics_sha256", "meta": "meta_sha256"}
    for kind, descriptor in probe["evidence"].items():
        if descriptor is None:
            continue
        if kind not in {*expected, "inputs"}:
            raise ValueError("unknown probe evidence kind")
        _descriptor(descriptor)
        raw_path = descriptor.get("path")
        if not isinstance(raw_path, str) or Path(raw_path).is_absolute():
            raise ValueError("invalid relative evidence path")
        path = (packet_root/raw_path).resolve()
        if not path.is_relative_to(packet_root.resolve()) or path.is_symlink():
            raise RuntimeError("probe evidence path escapes packet")
        if file_identity(path) != {"sha256": descriptor["sha256"], "bytes": descriptor["bytes"]}:
            raise RuntimeError("probe evidence identity mismatch")
        if kind in expected and probe.get(expected[kind]) != descriptor["sha256"]:
            raise RuntimeError("probe head and evidence digest disagree")
        result[kind] = str(path)
    if not {"strategy", "tvTrades", "metrics"} <= result.keys():
        raise ValueError("probe lacks required source, trades, or metrics")
    return result


def _merge_report(entry, report, status, policies):
    if not isinstance(report, dict) or report.get("status") not in _STATUSES:
        raise RuntimeError("case produced an invalid status")
    for key in ("probeId", "symbol", "script_tf", "sourceSha256", "evidence"):
        if key in report:
            raise RuntimeError("case report attempts to replace controller identity")
    if report["status"] == "passed":
        modes = report.get("modes")
        expected_modes = {"bars-direct", *("ticks-"+p for p in policies)}
        if (status != 0 or report.get("live_backtest_equal") is not True
                or not isinstance(modes, dict) or set(modes) != expected_modes
                or any(not isinstance(mode, dict) or mode.get("ok") is not True for mode in modes.values())):
            raise RuntimeError("case pass lacks successful evidence for every requested mode")
    entry.update(report)


def _archive_case(case_dir, artifact):
    total = 0
    with tarfile.open(artifact, "w:gz") as tar:
        for path in sorted(case_dir.rglob("*")):
            if path.is_symlink():
                raise RuntimeError("case artifact contains a symlink")
            if (not path.is_file() or path.suffix in (".sqlite3", ".stop", ".lock")
                    or "-wal" in path.name or "-shm" in path.name):
                continue
            total += path.stat().st_size
            if total > 2*1024**3:
                raise RuntimeError("case artifacts exceed 2 GiB")
            tar.add(path, arcname=str(path.relative_to(case_dir)), recursive=False)


def run_task(manifest, store, index, count, workspace, *, manifest_sha256=None):
    assigned = validate_manifest(manifest, index, count)
    run_id = manifest["runId"]
    workspace = Path(workspace)
    workspace.mkdir(parents=True, exist_ok=False)
    result = {
        "schemaVersion": "pineforge-live-two-input-results/v1", "runId": run_id,
        "task": index, "tasks": count, "liveCommit": manifest["liveCommit"],
        "manifestSha256": manifest_sha256, "manifest": manifest,
        "assignedProbeIds": assigned, "startedAtMs": time.time_ns()//1_000_000,
        "results": [], "setupError": None, "registryMutated": False,
    }
    try:
        baked_commit = os.environ.get("PINEFORGE_LIVE_BUILD_COMMIT")
        if baked_commit != manifest["liveCommit"]:
            raise RuntimeError("review image live commit does not match manifest")
        result["imageDigest"] = os.environ.get("PINEFORGE_LIVE_IMAGE_DIGEST")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", result["imageDigest"] or ""):
            raise RuntimeError("review job must pin its image digest")
        if manifest.get("imageDigest") not in (None, result["imageDigest"]):
            raise RuntimeError("review job image digest does not match manifest")
        if not assigned:
            result["emptyTask"] = True
        else:
            code = workspace/"code"
            code.mkdir()
            result["sources"] = {
                name: checkout(store, manifest["sources"][name], code/name)
                for name in ("engine", "codegen", "lab")
            }
            engine = code/"engine"
            _run(["cmake", "-S", str(engine), "-B", str(engine/"build"), "-G", "Ninja",
                  "-DCMAKE_BUILD_TYPE=Release", "-DPINEFORGE_BUILD_TESTS=OFF",
                  "-DPINEFORGE_BUILD_TUTORIAL=OFF", "-DPINEFORGE_BUILD_CORPUS_STRATEGIES=OFF",
                  "-DCMAKE_CXX_FLAGS="])
            _run(["cmake", "--build", str(engine/"build"), "--target", "pineforge", "--parallel",
                  str(min(os.cpu_count() or 1, 8))], timeout=1800)
            result["compiler"] = _run(["g++", "--version"]).splitlines()[0]
            archive = workspace/"packet.tar.gz"
            packet_desc = manifest["probePacket"]
            store.download(packet_desc["sha256"], archive, size=packet_desc["bytes"])
            packet_root = workspace/"packet"
            safe_extract_tar(archive, packet_root, max_bytes=2*1024**3)
            packet_manifest = packet_root/"manifest.json"
            if file_identity(packet_manifest)["sha256"] != packet_desc["manifestSha256"]:
                raise RuntimeError("probe packet manifest identity mismatch")
            packet = json.loads(packet_manifest.read_text())
            chosen = select_packet_probes(packet, manifest, assigned)
            result["population"] = packet["population"]["sha256"]
            result["baseline"] = packet["baseline"]
            templates = _templates(packet)
            cache = workspace/"feeds"
            cache.mkdir()
            builds = workspace/"strategies"
            builds.mkdir()
            for number, probe in enumerate(chosen):
                key = _sha(probe["strategy_sha256"], "strategy source SHA-256")
                case_dir = workspace/f"case-{number}"
                case_dir.mkdir()
                entry = {"probeId": probe["probe_id"], "symbol": probe["symbol"],
                         "script_tf": probe["timeframe"], "sourceSha256": key,
                         "status": "unmeasured"}
                result["results"].append(entry)
                try:
                    template = templates[(probe["lane"], probe["group"])]
                    if template["symbol"] != probe["symbol"] or template["script_tf"] != probe["timeframe"]:
                        raise ValueError("probe and lane symbol/timeframe mismatch")
                    refs = {name: str(fetch_feed(store, template[name], cache))
                            for name in ("chart", "finer", "daily") if template.get(name)}
                    if not {"chart", "finer"} <= refs.keys():
                        raise ValueError("both chart and minute feed are required")
                    evidence = _evidence_paths(probe, packet_root)
                    case = {
                        "probe": probe, "template": template, "evidence": evidence, "feeds": refs,
                        "engine": str(engine), "codegen": str(code/"codegen"), "lab": str(code/"lab"),
                        "build_dir": str(builds/key), "output": str(case_dir),
                        "replay_bars": manifest.get("replayBars", 16),
                        "daily_replay_bars": manifest.get("dailyReplayBars", 2),
                        "tick_policies": manifest.get("tickPolicies", ["high-first", "low-first"]),
                        "seed": manifest.get("seed", 20260909), "liveCommit": manifest["liveCommit"],
                    }
                    config = case_dir/"case.json"
                    config.write_bytes(canonical_json_bytes(case))
                    status = run_process(
                        [sys.executable, "-m", "pineforge_live.verification.probe_case", str(config)],
                        stdout_path=case_dir/"stdout.log", stderr_path=case_dir/"stderr.log",
                        timeout=manifest.get("caseTimeoutSeconds", 900),
                    )
                    report_path = case_dir/"result.json"
                    if not report_path.exists():
                        raise RuntimeError("case produced no result; "+_tail(case_dir/"stderr.log", 3000))
                    if report_path.stat().st_size > 16*1024**2:
                        raise RuntimeError("case result exceeds 16 MiB")
                    _merge_report(entry, json.loads(report_path.read_text()), status, case["tick_policies"])
                except subprocess.TimeoutExpired:
                    entry.update(status="unmeasured", error="case timeout; process group terminated")
                except Exception as exc:
                    entry.update(status="failed", error=f"{type(exc).__name__}: {exc}")
                artifact = workspace/f"case-{number}.tar.gz"
                try:
                    _archive_case(case_dir, artifact)
                    entry["evidence"] = store.upload_output(
                        artifact, run_id=run_id, task=index, name=artifact.name,
                        content_type="application/gzip",
                    )
                except Exception as exc:
                    entry.update(status="unmeasured", error=f"evidence publication failed: {type(exc).__name__}: {exc}")
                    raise
                print(json.dumps({"task": index, "probeId": entry["probeId"], "status": entry["status"],
                                  "error": entry.get("error")}, sort_keys=True), flush=True)
                (workspace/"partial.json").write_bytes(canonical_json_bytes(result))
                # Writable Cloud Run files consume memory; retire proven detail.
                shutil.rmtree(case_dir)
                artifact.unlink()
    except Exception as exc:
        result["setupError"] = f"{type(exc).__name__}: {exc}"
    result["finishedAtMs"] = time.time_ns()//1_000_000
    result["summary"] = {name: sum(row["status"] == name for row in result["results"])
                         for name in sorted(_STATUSES)}
    result["summary"]["assigned"] = len(assigned)
    result["summary"]["attempted"] = len(result["results"])
    result["summary"]["measured"] = sum(row["status"] not in {"unmeasured", "unsupported"}
                                           and "evidence" in row for row in result["results"])
    result["ok"] = (not result["setupError"]
                    and [row["probeId"] for row in result["results"]] == assigned
                    and all(row["status"] == "passed" and "evidence" in row for row in result["results"]))
    output = workspace/"results.json"
    output.write_bytes(canonical_json_bytes(result))
    receipt = store.upload_output(output, run_id=run_id, task=index,
                                  name="results.json", content_type="application/json")
    print(json.dumps({"ok": result["ok"], "summary": result["summary"], "receipt": receipt},
                     sort_keys=True), flush=True)
    return 0 if result["ok"] else 1


def main():
    if "CLOUD_RUN_TASK_INDEX" not in os.environ:
        raise RuntimeError("campaign verification must execute on Cloud Run")
    index, count = task_coordinates()
    store = GcsStore.from_environment()
    sha = os.environ.get("PINEFORGE_LIVE_MANIFEST_SHA256", "")
    manifest = store.read_json(sha)
    validate_manifest(manifest, index, count)
    return run_task(manifest, store, index, count,
                    Path("/workspace")/(manifest["runId"]+"-"+str(index)),
                    manifest_sha256=sha)


if __name__ == "__main__":
    raise SystemExit(main())
