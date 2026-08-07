#!/usr/bin/env python3
"""Print a redacted staging deployment status."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-dir", type=Path, default=Path("/var/lib/infinityscan/staging/releases"))
    args = parser.parse_args()
    states = []
    for path in sorted(args.state_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        states.append({key: data.get(key) for key in ("environment", "release_id", "git_commit_sha", "active_slot", "event", "smoke_test")})
    print(json.dumps({"environment": "staging", "releases": states}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
