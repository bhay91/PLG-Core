#!/usr/bin/env python3
"""Build a validated, review-only .ppsresearch v1 package offline."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from plg_core.intake.research_package import build_ppsresearch_package


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--research", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--target-proposal-id", type=int)
    args = parser.parse_args()
    try:
        payload = json.loads(args.research.read_text(encoding="utf-8"))
        result = build_ppsresearch_package(payload, args.source.read_bytes(), output_path=args.output, overwrite=args.overwrite, target_proposal_id=args.target_proposal_id)
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    print(f"output: {args.output}")
    print("package: ppsresearch v1")
    print(f"size: {len(result)} bytes")
    print("validation: success")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
