#!/usr/bin/env python3
"""Render an isolated Cloud Run Job JSON document; perform no cloud mutation."""
from __future__ import annotations

import argparse
import json
import re


def job_spec(*, name: str, image: str, service_account: str,
             bucket: str, manifest_sha256: str, tasks: int = 1,
             timeout_seconds: int = 7200) -> dict:
    if not re.fullmatch(r"pineforge-live-[a-z0-9-]{1,45}[a-z0-9]", name):
        raise ValueError("job name must be a distinct pineforge-live-* name")
    if not re.fullmatch(r"[^\s@]+@sha256:[0-9a-f]{64}", image):
        raise ValueError("image must be pinned by SHA-256 digest")
    if not re.fullmatch(r"[a-z0-9-]+@[a-z0-9-]+\.iam\.gserviceaccount\.com", service_account):
        raise ValueError("invalid service account email")
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{1,220}[a-z0-9]", bucket):
        raise ValueError("invalid evidence bucket")
    if not re.fullmatch(r"[0-9a-f]{64}", manifest_sha256):
        raise ValueError("invalid manifest SHA-256")
    if isinstance(tasks, bool) or not isinstance(tasks, int) or not 1 <= tasks <= 8:
        raise ValueError("tasks must be between 1 and 8")
    if (isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, int)
            or not 60 <= timeout_seconds <= 14400):
        raise ValueError("timeout must be between 60 and 14400 seconds")
    return {
        "apiVersion": "run.googleapis.com/v1", "kind": "Job",
        "metadata": {"name": name, "labels": {"purpose": "pineforge-live-verification"}},
        "spec": {"template": {"spec": {
            "parallelism": tasks, "taskCount": tasks,
            "template": {"spec": {
                "serviceAccountName": service_account,
                "timeoutSeconds": str(timeout_seconds), "maxRetries": 0,
                "containers": [{
                    "image": image,
                    "command": ["python"],
                    "args": ["-m", "pineforge_live.verification.campaign_worker"],
                    "resources": {"limits": {"cpu": "8", "memory": "32Gi"}},
                    "env": [
                        {"name": "PINEFORGE_EVIDENCE_BUCKET", "value": bucket},
                        {"name": "PINEFORGE_LIVE_MANIFEST_SHA256", "value": manifest_sha256},
                        {"name": "PINEFORGE_LIVE_IMAGE_DIGEST", "value": image.split("@", 1)[1]},
                    ],
                }],
            }},
        }}},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("name", "image", "service-account", "bucket", "manifest-sha256"):
        parser.add_argument(f"--{name}", required=True)
    parser.add_argument("--tasks", type=int, default=1)
    parser.add_argument("--timeout-seconds", type=int, default=7200)
    args = parser.parse_args()
    print(json.dumps(job_spec(**vars(args)), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
