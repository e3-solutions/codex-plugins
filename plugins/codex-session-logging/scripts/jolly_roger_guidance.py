#!/usr/bin/env python3
"""Forum/Sesh guidance only; never starts telemetry or standalone updaters."""
import sys
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
from jolly_roger import main

if __name__ == "__main__":
    raise SystemExit(main("guidance"))
