#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
from datetime import datetime, timezone
from typing import Any


RUN_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def _load_json(path: pathlib.Path | None) -> dict[str, Any]:
    if path is None or not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _workflow_payload() -> dict[str, Any]:
    repository = os.environ.get("GITHUB_REPOSITORY")
    server_url = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    run_id = os.environ.get("GITHUB_RUN_ID")
    workflow_url = f"{server_url}/{repository}/actions/runs/{run_id}" if repository and run_id else None
    return {
        "repository": repository,
        "ref": os.environ.get("GITHUB_REF_NAME") or os.environ.get("GITHUB_REF"),
        "sha": os.environ.get("GITHUB_SHA"),
        "run_id": run_id,
        "run_number": os.environ.get("GITHUB_RUN_NUMBER"),
        "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT"),
        "url": workflow_url,
    }


def _compact_metadata(metadata: dict[str, Any], artifact_sha256: str, artifact_uri: str | None) -> dict[str, Any]:
    artifact = dict(metadata.get("artifact") or {})
    artifact["sha256"] = artifact_sha256
    if artifact_uri:
        artifact["uri"] = artifact_uri
    return {
        "training_host": metadata.get("training_host"),
        "artifact": artifact,
        "manifest": metadata.get("manifest") or {},
        "metrics": metadata.get("metrics") or {},
        "promotion": metadata.get("promotion") or {},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Write sanitized Zone 5 model registry metadata.")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--artifact-sha256", required=True)
    parser.add_argument("--artifact-uri", default="")
    parser.add_argument("--metadata-json", type=pathlib.Path)
    parser.add_argument("--output-dir", type=pathlib.Path, default=pathlib.Path("model_registry"))
    args = parser.parse_args()

    if not RUN_ID_RE.fullmatch(args.run_id):
        raise SystemExit(f"Invalid run_id: {args.run_id!r}")

    metadata = _load_json(args.metadata_json)
    payload = {
        "schema_version": 1,
        "run_id": args.run_id,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "source": _compact_metadata(metadata, args.artifact_sha256, args.artifact_uri or None),
        "workflow": _workflow_payload(),
    }

    output_dir = args.output_dir
    promotions_dir = output_dir / "promotions"
    promotions_dir.mkdir(parents=True, exist_ok=True)

    promotion_path = promotions_dir / f"{args.run_id}.json"
    latest_path = output_dir / "zone5-production-latest.json"
    rendered = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    promotion_path.write_text(rendered, encoding="utf-8")
    latest_path.write_text(rendered, encoding="utf-8")
    print(promotion_path)
    print(latest_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
