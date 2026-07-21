from __future__ import annotations

import unittest
from unittest.mock import patch

import session_logging


class RemoteBelongsToOrgTests(unittest.TestCase):
    def test_accepts_a_configured_github_ssh_alias(self) -> None:
        with patch("session_logging.ssh_host_resolves_to_github", return_value=True) as resolves:
            allowed = session_logging.remote_belongs_to_org(
                "git@github-coreedge:e3-solutions/negotiation.git",
                "e3-solutions",
            )

        self.assertTrue(allowed)
        resolves.assert_called_once_with("github-coreedge")

    def test_rejects_an_ssh_alias_that_does_not_resolve_to_github(self) -> None:
        with patch("session_logging.ssh_host_resolves_to_github", return_value=False):
            allowed = session_logging.remote_belongs_to_org(
                "git@internal-git:e3-solutions/negotiation.git",
                "e3-solutions",
            )

        self.assertFalse(allowed)

    def test_rejects_other_organizations(self) -> None:
        self.assertFalse(
            session_logging.remote_belongs_to_org(
                "git@github.com:other-org/negotiation.git",
                "e3-solutions",
            )
        )


if __name__ == "__main__":
    unittest.main()
