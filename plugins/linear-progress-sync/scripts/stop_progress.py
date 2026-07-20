#!/usr/bin/env python3
"""Legacy Stop hook entrypoint.

Linear progress updates are commit-driven. Keep this file as a no-op so stale
hook registrations from older plugin releases cannot publish edit-only updates.
"""


def main() -> None:
    return


if __name__ == "__main__":
    main()
