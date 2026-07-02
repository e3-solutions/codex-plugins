#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from linear_sync import install_codex_hooks


def main() -> None:
    parser = argparse.ArgumentParser(description="Install Linear Progress Sync hooks into ~/.codex/hooks.json.")
    parser.add_argument(
        "--plugin-root",
        default=str(Path(__file__).resolve().parents[3]),
        help="codex-plugins repo root or linear-progress-sync plugin root.",
    )
    parser.add_argument("--codex-home", help="Override CODEX_HOME for tests or nonstandard installs.")
    parser.add_argument("--dry-run", action="store_true", help="Print the merged hook plan without writing.")
    args = parser.parse_args()

    result = install_codex_hooks(
        plugin_repo_root=args.plugin_root,
        codex_home_path=args.codex_home,
        dry_run=args.dry_run,
    )
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
