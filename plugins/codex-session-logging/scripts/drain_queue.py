#!/usr/bin/env python3
from __future__ import annotations

import json

from session_logging import drain_queue


def main() -> None:
    print(json.dumps(drain_queue(), sort_keys=True))


if __name__ == "__main__":
    main()
