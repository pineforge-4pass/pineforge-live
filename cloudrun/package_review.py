#!/usr/bin/env python3
"""Package exact local Git commits and evidence refs without executing probes.

Writes artifacts locally. It never uploads, invokes cloud build, or creates jobs.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True).stdout.strip()


def _identity(path: Path) -> dict:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return {"sha256": digest.hexdigest(), "bytes": size}


def _pairs(values: list[str]) -> dict[str, str]:
    result = {}
    for value in values:
        name, sep, entry = value.partition("=")
        if not sep or not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", name) or not entry:
            raise ValueError("expected name=value")
        if name in result:
            raise ValueError(f"duplicate source name: {name}")
        result[name] = entry
    return result


def package_sources(sources: dict[str, str], commits: dict[str, str],
                    output: Path, evidence_refs: dict | None = None) -> dict:
    """Create Git bundles, retaining real commit identities for provenance.

    Bundles include only objects reachable from the named commit. No checkout,
    untracked content, environment file, credentials directory or binary cache
    is copied. Gitlinks are named in the manifest and require a separately
    supplied source bundle if the worker needs that submodule.
    """
    if not sources or sources.keys() != commits.keys():
        raise ValueError("every source requires exactly one --commit")
    output = output.resolve()
    if output.exists():
        raise ValueError("output directory must be new")
    selected = []
    for name, raw_repo in sorted(sources.items()):
        repo = Path(raw_repo).resolve()
        commit = commits[name]
        if not re.fullmatch(r"[0-9a-f]{40}", commit):
            raise ValueError("source commits must be full lowercase 40-character hashes")
        if _git(repo, "rev-parse", f"{commit}^{{commit}}") != commit:
            raise ValueError("commit identity mismatch")
        tree = _git(repo, "rev-parse", f"{commit}^{{tree}}")
        entries = _git(repo, "ls-tree", "-r", commit).splitlines()
        gitlinks = {}
        for entry in entries:
            info, path = entry.split("\t", 1)
            mode, _, oid = info.split()
            if mode == "160000":
                gitlinks[path] = oid
        # A Git bundle contains history, so check reachable historical names as
        # well as the selected tree before packaging credential-shaped files.
        for entry in _git(repo, "rev-list", "--objects", commit).splitlines():
            _, _, path = entry.partition(" ")
            leaf = Path(path).name.lower()
            if (leaf in {".env", ".dev.vars", "credentials.json", "credentials.db", "lab.sqlite"}
                    or leaf.endswith((".tfvars", ".tfvars.json", ".pem", ".key"))):
                raise ValueError(f"refusing sensitive-looking tracked path in {name}: {path}")
        selected.append((name, repo, commit, tree, gitlinks))
    output.mkdir(parents=True)
    manifest = {"schema_version": "pineforge-live-source-packet/v1", "sources": {},
                "evidence_refs": evidence_refs or {}}
    for name, repo, commit, tree, gitlinks in selected:
        artifact = output / f"{name}.bundle"
        # `git bundle create <raw SHA>` has no named ref and refuses an empty
        # bundle. Name the exact commit only in an isolated, shared-object bare
        # clone; never add temporary refs to the operator's source repository.
        with tempfile.TemporaryDirectory(prefix="pineforge-live-package-") as scratch:
            clone = Path(scratch) / "source.git"
            _git(repo, "clone", "--bare", "--shared", "--no-hardlinks", "--",
                 str(repo), str(clone))
            ref = "refs/heads/pineforge-live-packet"
            _git(clone, "update-ref", ref, commit)
            _git(clone, "bundle", "create", str(artifact), ref)
        _git(repo, "bundle", "verify", str(artifact))
        identity = _identity(artifact)
        manifest["sources"][name] = {"commit": commit, "tree": tree,
            "format": "git-bundle", "filename": artifact.name, **identity,
            "gitlinks": gitlinks}
    raw = json.dumps(manifest, sort_keys=True, separators=(",", ":"),
                     ensure_ascii=False, allow_nan=False).encode()
    (output / "sources.json").write_bytes(raw)
    (output / "sources.sha256").write_text(hashlib.sha256(raw).hexdigest() + "\n")
    return manifest


def live_context(repo: Path, commit: str, destination: Path) -> None:
    """Create an allowlisted image context from the exact live commit."""
    destination = destination.resolve()
    if destination.exists():
        raise ValueError("image context destination must be new")
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("live commit must be a full hash")
    # Git archive outputs only the four named tracked paths, never .git or .env.
    destination.mkdir(parents=True)
    archive = destination / "context.tar"
    _git(repo, "archive", "--format=tar", f"--output={archive}", commit, "--",
         "pineforge_live", "cloudrun", "LICENSE", "pyproject.toml")
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from pineforge_live.verification.cloud_io import safe_extract_tar
    safe_extract_tar(archive, destination / "context")
    (destination / "identity.json").write_text(json.dumps({
        "commit": commit, "tree": _git(repo, "rev-parse", f"{commit}^{{tree}}"),
        "archive": _identity(archive),
    }, indent=2) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", action="append", default=[], metavar="NAME=LOCAL_PATH")
    parser.add_argument("--commit", action="append", default=[], metavar="NAME=FULL_SHA")
    parser.add_argument("--evidence-refs", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--live-context", type=Path,
                        help="also create an image context from source named live")
    args = parser.parse_args()
    sources, commits = _pairs(args.source), _pairs(args.commit)
    evidence = json.loads(args.evidence_refs.read_text()) if args.evidence_refs else None
    if evidence is not None and not isinstance(evidence, dict):
        raise ValueError("evidence refs must be a JSON object")
    if args.live_context and "live" not in sources:
        raise ValueError("--live-context requires the live source")
    package_sources(sources, commits, args.output, evidence)
    if args.live_context:
        live_context(Path(sources["live"]).resolve(), commits["live"], args.live_context)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
