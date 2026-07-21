from __future__ import annotations

import unittest
from unittest.mock import patch

import session_logging


class RemoteBelongsToOrgTests(unittest.TestCase):
    def test_accepts_a_configured_github_ssh_alias(self) -> None:
        with patch("session_logging.ssh_host_resolves_to_github", return_value=True) as resolves:
            canonical = session_logging.canonical_github_remote(
                "git@github-coreedge:e3-solutions/negotiation.git",
                "e3-solutions",
            )

        self.assertEqual(canonical, "https://github.com/e3-solutions/negotiation.git")
        resolves.assert_called_once_with("github-coreedge")

    def test_rejects_an_ssh_alias_that_does_not_resolve_to_github(self) -> None:
        with patch("session_logging.ssh_host_resolves_to_github", return_value=False):
            canonical = session_logging.canonical_github_remote(
                "git@internal-git:e3-solutions/negotiation.git",
                "e3-solutions",
            )

        self.assertIsNone(canonical)

    def test_canonicalizes_github_https_and_rejects_other_organizations(self) -> None:
        self.assertEqual(
            session_logging.canonical_github_remote(
                "https://github.com/e3-solutions/negotiation",
                "e3-solutions",
            ),
            "https://github.com/e3-solutions/negotiation.git",
        )
        self.assertIsNone(
            session_logging.canonical_github_remote(
                "git@github.com:other-org/negotiation.git",
                "e3-solutions",
            )
        )


if __name__ == "__main__":
    unittest.main()
