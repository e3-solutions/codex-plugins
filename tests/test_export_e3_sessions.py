from __future__ import annotations

import importlib.util
import json
import sys
import zipfile
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "export_e3_sessions.py"


def load_exporter():
    spec = importlib.util.spec_from_file_location("export_e3_sessions", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def codex_rows(*, session_id: str, remote: str, secret: str = "safe") -> list[dict]:
    return [
        {
            "type": "session_meta",
            "payload": {
                "id": session_id,
                "cwd": "/deleted/repo",
                "git": {"repository_url": remote},
            },
        },
        {"type": "event_msg", "payload": {"type": "user_message", "message": secret}},
    ]


def test_export_includes_only_verified_e3_sessions_and_redacts_secrets(tmp_path):
    exporter = load_exporter()
    codex_home = tmp_path / "codex"
    claude_home = tmp_path / "claude"
    output = tmp_path / "sessions.zip"

    write_jsonl(
        codex_home / "sessions" / "2026" / "e3.jsonl",
        codex_rows(
            session_id="codex-e3",
            remote="git@github.com:e3-solutions/internal.git",
            secret="SUPABASE_SERVICE_ROLE_KEY=super-secret-value",
        ),
    )
    write_jsonl(
        codex_home / "sessions" / "2026" / "personal.jsonl",
        codex_rows(session_id="codex-personal", remote="https://github.com/person/private.git"),
    )
    write_jsonl(
        claude_home / "projects" / "-deleted-e3" / "claude-e3.jsonl",
        [
            {
                "type": "user",
                "sessionId": "claude-e3",
                "cwd": "/deleted/repo",
                "repository_url": "https://github.com/e3-solutions/internal",
                "message": {"content": "Authorization: Bearer abcdefghijklmnopqrstuvwxyz"},
            }
        ],
    )
    write_jsonl(
        claude_home / "projects" / "-personal" / "personal.jsonl",
        [
            {
                "type": "user",
                "sessionId": "claude-personal",
                "repository_url": "https://github.com/person/private",
            }
        ],
    )

    result = exporter.export_sessions(
        codex_home=codex_home,
        claude_home=claude_home,
        output=output,
    )

    assert result["selected"] == {"codex": 1, "claude": 1, "total": 2}
    assert result["skipped_unverified_or_other_org"] == {"codex": 1, "claude": 1}
    assert result["redactions"] == 2
    with zipfile.ZipFile(output) as archive:
        names = archive.namelist()
        manifest_name = next(name for name in names if name.endswith("/manifest.json"))
        manifest = json.loads(archive.read(manifest_name))
        exported = b"\n".join(archive.read(row["archive_path"]) for row in manifest["sessions"])
    assert len(manifest["sessions"]) == 2
    assert b"super-secret-value" not in exported
    assert b"abcdefghijklmnopqrstuvwxyz" not in exported
    assert exported.count(b"[REDACTED]") == 2
    assert not any("personal.jsonl" in name for name in names)


def test_claude_e3_logger_can_verify_deleted_repo_session(tmp_path):
    exporter = load_exporter()
    claude_home = tmp_path / "claude"
    session_id = "logged-e3-session"
    write_jsonl(
        claude_home / "projects" / "-deleted" / f"{session_id}.jsonl",
        [{"type": "user", "sessionId": session_id, "cwd": "/no/longer/exists"}],
    )
    write_jsonl(
        claude_home / "session-logging" / "events.jsonl",
        [{"session_id": session_id, "event_type": "thread_started"}],
    )

    result = exporter.export_sessions(
        codex_home=tmp_path / "codex",
        claude_home=claude_home,
        output=tmp_path / "sessions.zip",
        dry_run=True,
    )

    assert result["selected"] == {"codex": 0, "claude": 1, "total": 1}
    assert result["sessions"][0]["verification"] == "e3_only_claude_logger"


def test_export_accepts_verified_github_ssh_alias_and_canonicalizes_manifest(tmp_path):
    exporter = load_exporter()
    codex_home = tmp_path / "codex"
    write_jsonl(
        codex_home / "sessions" / "2026" / "alias.jsonl",
        codex_rows(
            session_id="codex-alias",
            remote="git@github-coreedge:e3-solutions/negotiation.git",
        ),
    )

    with patch.object(exporter, "ssh_host_resolves_to_github", return_value=True) as resolves:
        result = exporter.export_sessions(
            codex_home=codex_home,
            claude_home=tmp_path / "claude",
            output=tmp_path / "sessions.zip",
            dry_run=True,
        )

    assert result["selected"] == {"codex": 1, "claude": 0, "total": 1}
    assert result["sessions"][0]["repo_remote"] == "https://github.com/e3-solutions/negotiation.git"
    resolves.assert_any_call("github-coreedge")


def test_export_rejects_unverified_ssh_alias(tmp_path):
    exporter = load_exporter()
    codex_home = tmp_path / "codex"
    write_jsonl(
        codex_home / "sessions" / "2026" / "alias.jsonl",
        codex_rows(
            session_id="codex-alias",
            remote="git@internal-git:e3-solutions/negotiation.git",
        ),
    )

    with patch.object(exporter, "ssh_host_resolves_to_github", return_value=False):
        result = exporter.export_sessions(
            codex_home=codex_home,
            claude_home=tmp_path / "claude",
            output=tmp_path / "sessions.zip",
            dry_run=True,
        )

    assert result["selected"] == {"codex": 0, "claude": 0, "total": 0}


def test_raw_export_preserves_original_bytes(tmp_path):
    exporter = load_exporter()
    codex_home = tmp_path / "codex"
    source = codex_home / "sessions" / "raw.jsonl"
    write_jsonl(
        source,
        codex_rows(
            session_id="raw",
            remote="https://github.com/e3-solutions/repo.git",
            secret="password=hunter2",
        ),
    )
    output = tmp_path / "raw.zip"

    result = exporter.export_sessions(
        codex_home=codex_home,
        claude_home=tmp_path / "claude",
        output=output,
        redact=False,
    )

    with zipfile.ZipFile(output) as archive:
        manifest_name = next(name for name in archive.namelist() if name.endswith("/manifest.json"))
        manifest = json.loads(archive.read(manifest_name))
        exported = archive.read(manifest["sessions"][0]["archive_path"])
    assert result["redacted"] is False
    assert result["redactions"] == 0
    assert exported == source.read_bytes()


def test_export_refuses_to_overwrite_existing_archive(tmp_path):
    exporter = load_exporter()
    output = tmp_path / "existing.zip"
    output.write_bytes(b"keep me")

    try:
        exporter.export_sessions(
            codex_home=tmp_path / "codex",
            claude_home=tmp_path / "claude",
            output=output,
        )
    except FileExistsError:
        pass
    else:
        raise AssertionError("expected existing output to be rejected")

    assert output.read_bytes() == b"keep me"
    assert not list(tmp_path.glob("*.partial"))


def test_redacted_export_keeps_jsonl_valid_for_private_keys_and_escaped_quotes(tmp_path):
    exporter = load_exporter()
    codex_home = tmp_path / "codex"
    source = codex_home / "sessions" / "secrets.jsonl"
    private_key = "-----BEGIN PRIVATE KEY-----\nabc123\n-----END PRIVATE KEY-----"
    write_jsonl(
        source,
        codex_rows(
            session_id="secrets",
            remote="https://github.com/e3-solutions/repo.git",
            secret=f'password=value-before-escaped-quote\\" tail {private_key}',
        ),
    )
    output = tmp_path / "redacted.zip"

    exporter.export_sessions(
        codex_home=codex_home,
        claude_home=tmp_path / "claude",
        output=output,
    )

    with zipfile.ZipFile(output) as archive:
        manifest_name = next(name for name in archive.namelist() if name.endswith("/manifest.json"))
        manifest = json.loads(archive.read(manifest_name))
        lines = archive.read(manifest["sessions"][0]["archive_path"]).decode().splitlines()
    parsed = [json.loads(line) for line in lines]
    rendered = json.dumps(parsed)
    assert "abc123" not in rendered
    assert "value-before-escaped-quote" not in rendered
    assert rendered.count("[REDACTED]") >= 2
