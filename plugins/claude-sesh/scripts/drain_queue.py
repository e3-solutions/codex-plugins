#!/usr/bin/env python3
"""Upload queued Sesh linkage events (started in the background by the hooks)."""
from __future__ import annotations

import json

from sesh_linking import drain_queue


def main() -> None:
    print(json.dumps(drain_queue(), sort_keys=True))


if __name__ == "__main__":
    main()
