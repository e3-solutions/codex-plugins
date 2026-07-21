from __future__ import annotations

import unittest
from pathlib import Path
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

    def test_client_context_submits_the_canonical_remote(self) -> None:
        record = {
            "metadata": {
                "cwd": "/tmp/negotiation",
                "repo_remote": "git@github-coreedge:e3-solutions/negotiation.git",
            }
        }
        with (
            patch("session_logging.ssh_host_resolves_to_github", return_value=True),
            patch("session_logging.git_config_value", return_value=None),
            patch("session_logging.local_hostname", return_value="test-host"),
            patch("session_logging.local_username", return_value="jai"),
            patch("session_logging.local_installation_id", return_value="installation-id"),
            patch("session_logging.saved_linear_user_name", return_value="Jai"),
        ):
            context = session_logging.client_context(record, base=Path("/tmp/logging"))

        self.assertEqual(
            context["repo_remote"],
            "https://github.com/e3-solutions/negotiation.git",
        )


if __name__ == "__main__":
    unittest.main()
