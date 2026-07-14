#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from resident_updater import activate_release


def main() -> None:
    parser = argparse.ArgumentParser(description="Install the zero-touch Core Edge Codex plugin updater.")
    parser.add_argument(
        "--plugin-root",
        required=True,
        help="Core Edge marketplace repository root used during setup.",
    )
    parser.add_argument("--json", action="store_true", help="Print machine-readable output.")
    args = parser.parse_args()
    supplied = Path(args.plugin_root).expanduser().resolve()
    repo_root = supplied.parents[1] if (supplied / ".codex-plugin" / "plugin.json").is_file() else supplied
    result = activate_release(repo_root)
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        service = result.get("service") or {}
        status = "scheduled" if service.get("scheduled") else "installed with SessionStart fallback"
        print(f"Core Edge resident updater {status}; marketplace {result['version']} is active.")


if __name__ == "__main__":
    main()
