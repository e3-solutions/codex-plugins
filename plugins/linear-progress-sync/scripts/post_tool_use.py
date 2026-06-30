#!/usr/bin/env python3
from __future__ import annotations

import json

from linear_sync import handle_post_tool_use, read_stdin_json


def main() -> None:
    queued = handle_post_tool_use(read_stdin_json())
    if queued:
        print(json.dumps({"queued": queued["id"], "type": queued["type"]}))


if __name__ == "__main__":
    main()

