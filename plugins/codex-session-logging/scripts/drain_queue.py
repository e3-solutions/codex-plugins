#!/usr/bin/env python3
from __future__ import annotations

import json
import os
from pathlib import Path
import sys

if os.environ.get("JOLLY_ROGER_MANAGED") == "1":
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from session_logging import drain_queue


def main() -> None:
    print(json.dumps(drain_queue(), sort_keys=True))


if __name__ == "__main__":
    main()
