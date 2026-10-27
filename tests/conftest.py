from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolated_sherlock_home(tmp_path_factory, monkeypatch):
    """Keep the machine's real ~/.sherlock session-exclusion policy out of every test.

    Claude Session Logging reads ``$SHERLOCK_HOME/session-exclusions.json``
    (COR-5002); tests that need a policy write one into their own temp home.
    """
    monkeypatch.setenv("SHERLOCK_HOME", str(tmp_path_factory.mktemp("sherlock-home")))
