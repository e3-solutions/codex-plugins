#!/usr/bin/env python3
from __future__ import annotations

import argparse

from linear_sync import cli_root_arg, install_post_commit_hook


def main() -> None:
    parser = argparse.ArgumentParser(description="Install the linear-progress-sync Git post-commit hook.")
    parser.add_argument("--force", action="store_true", help="Replace an existing post-commit hook without backup.")
    cli_root_arg(parser)
    args = parser.parse_args()
    hook_path = install_post_commit_hook(root=args.root, force=args.force)
    print(f"Installed post-commit hook: {hook_path}")


if __name__ == "__main__":
    main()

