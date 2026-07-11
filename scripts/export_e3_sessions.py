#!/usr/bin/env python3
"""Export locally stored Codex and Claude sessions for verified GitHub org repos."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import zipfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


DEFAULT_ORG = "e3-solutions"
REDACTED = "[REDACTED]"
REMOTE_KEYS = {"repo_remote", "repository_url", "repositoryurl", "remote_url", "remoteurl"}
SECRET_PATTERNS = (
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{12,}"),
    re.compile(r"\b(?:sk|rk|pk)-(?:proj-)?[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bxox(?:a|b|p|r|s)-[A-Za-z0-9-]{12,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(
        r"(?i)(\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|service[_-]?role[_-]?key|"
        r"supabase_service_role_key|database_url|db_url|password|authorization|cookie|secret)\b"
        r"[\"']?\s*[:=]\s*[\"']?)((?!\[REDACTED\])[^\"',\s}]+)"
    ),
    re.compile(r"(?i)(\b(?:postgres(?:ql)?|mysql)://[^:\s/]+:)([^@\s/]+)(@)"),
)
PRIVATE_KEY_PATTERN = re.compile(
    r"-----BEGIN [^-\r\n]*PRIVATE KEY-----.*?-----END [^-\r\n]*PRIVATE KEY-----",
    flags=re.DOTALL,
)


@dataclass(frozen=True)
class SessionFile:
    source: str
    path: Path
    relative_path: str
    session_id: str | None
    cwd: str | None
    repo_remote: str | None
    verification: str


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Create a shareable ZIP of local Codex and Claude sessions for e3-solutions repos."
    )
    parser.add_argument("--output", type=Path, help="Output ZIP path (defaults to Desktop).")
    parser.add_argument("--org", default=DEFAULT_ORG, help="GitHub organization to include.")
    parser.add_argument("--codex-home", type=Path, default=default_codex_home())
    parser.add_argument("--claude-home", type=Path, default=Path.home() / ".claude")
    parser.add_argument(
        "--raw",
        action="store_true",
        help="Do not redact common credentials. Use only for a trusted recipient.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print discovery results without writing a ZIP.")
    args = parser.parse_args(argv)

    output = args.output.expanduser() if args.output else default_output_path()
    result = export_sessions(
        codex_home=args.codex_home.expanduser(),
        claude_home=args.claude_home.expanduser(),
        output=output,
        org=args.org,
        redact=not args.raw,
        dry_run=args.dry_run,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def default_codex_home() -> Path:
    configured = os.environ.get("CODEX_HOME")
    return Path(configured).expanduser() if configured else Path.home() / ".codex"


def default_output_path() -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    desktop = Path.home() / "Desktop"
    parent = desktop if desktop.is_dir() else Path.cwd()
    return parent / f"e3-ai-sessions-{timestamp}.zip"


def export_sessions(
    *,
    codex_home: Path,
    claude_home: Path,
    output: Path,
    org: str = DEFAULT_ORG,
    redact: bool = True,
    dry_run: bool = False,
) -> dict[str, Any]:
    codex_root = codex_home / "sessions"
    claude_root = claude_home / "projects"
    codex_paths = sorted(codex_root.glob("**/*.jsonl")) if codex_root.exists() else []
    claude_paths = sorted(claude_root.glob("*/*.jsonl")) if claude_root.exists() else []

    codex_identities = {path: read_codex_identity(path) for path in codex_paths}
    cwd_remotes = verified_cwd_remotes(codex_identities.values(), org=org)
    logged_claude_sessions = claude_logged_session_ids(claude_home)

    selected: list[SessionFile] = []
    skipped = {"codex": 0, "claude": 0}
    for path, identity in codex_identities.items():
        candidate = verify_session(
            source="codex",
            path=path,
            root=codex_root,
            identity=identity,
            org=org,
            cwd_remotes=cwd_remotes,
        )
        if candidate:
            selected.append(candidate)
        else:
            skipped["codex"] += 1

    for path in claude_paths:
        identity = read_claude_identity(path)
        candidate = verify_session(
            source="claude",
            path=path,
            root=claude_root,
            identity=identity,
            org=org,
            cwd_remotes=cwd_remotes,
            trusted_session_ids=logged_claude_sessions,
        )
        if candidate:
            selected.append(candidate)
        else:
            skipped["claude"] += 1

    summary: dict[str, Any] = {
        "output": None if dry_run else str(output.resolve()),
        "organization": org,
        "redacted": redact,
        "selected": {
            "codex": sum(item.source == "codex" for item in selected),
            "claude": sum(item.source == "claude" for item in selected),
            "total": len(selected),
        },
        "skipped_unverified_or_other_org": skipped,
    }
    if dry_run:
        summary["sessions"] = [manifest_entry(item) for item in selected]
        return summary

    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        raise FileExistsError(f"refusing to overwrite existing export: {output}")
    partial_output = output.with_name(f".{output.name}.{os.getpid()}.partial")
    archive_root = output.stem
    manifest: dict[str, Any] = {
        "format_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "organization": org,
        "redacted": redact,
        "source_roots": {"codex": str(codex_root), "claude": str(claude_root)},
        "skipped_unverified_or_other_org": skipped,
        "sessions": [],
    }
    total_redactions = 0
    try:
        with zipfile.ZipFile(partial_output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=1) as archive:
            for item in selected:
                archive_path = f"{archive_root}/{item.source}/{item.relative_path}"
                export_meta = write_session_file(archive, item.path, archive_path, redact=redact)
                total_redactions += export_meta["redactions"]
                manifest["sessions"].append({**manifest_entry(item), **export_meta, "archive_path": archive_path})
            manifest["counts"] = {
                "codex": summary["selected"]["codex"],
                "claude": summary["selected"]["claude"],
                "total": len(selected),
                "redactions": total_redactions,
            }
            archive.writestr(
                f"{archive_root}/manifest.json",
                json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            )
            archive.writestr(f"{archive_root}/README.txt", archive_readme(org=org, redacted=redact))
        os.replace(partial_output, output)
    except BaseException:
        partial_output.unlink(missing_ok=True)
        raise

    summary["bytes"] = output.stat().st_size
    summary["redactions"] = total_redactions
    return summary


def verify_session(
    *,
    source: str,
    path: Path,
    root: Path,
    identity: dict[str, str | None],
    org: str,
    cwd_remotes: dict[str, str],
    trusted_session_ids: set[str] | None = None,
) -> SessionFile | None:
    cwd = clean_string(identity.get("cwd"))
    session_id = clean_string(identity.get("session_id"))
    explicit_remote = clean_string(identity.get("repo_remote"))
    remote = explicit_remote
    verification = "transcript_metadata"
    if explicit_remote and not remote_belongs_to_org(explicit_remote, org):
        return None
    if not remote and cwd and cwd in cwd_remotes:
        remote = cwd_remotes[cwd]
        verification = "matching_codex_cwd"
    if not remote and cwd:
        remote = git_origin_remote(cwd)
        verification = "live_git_remote"
    if remote and not remote_belongs_to_org(remote, org):
        return None
    if not remote:
        if session_id and trusted_session_ids and session_id in trusted_session_ids:
            verification = "e3_only_claude_logger"
        else:
            return None
    try:
        relative = path.relative_to(root).as_posix()
    except ValueError:
        relative = path.name
    return SessionFile(source, path, relative, session_id, cwd, remote, verification)


def verified_cwd_remotes(identities: Iterator[dict[str, str | None]], *, org: str) -> dict[str, str]:
    candidates: dict[str, set[str]] = {}
    for identity in identities:
        cwd = clean_string(identity.get("cwd"))
        remote = clean_string(identity.get("repo_remote"))
        if cwd and remote and remote_belongs_to_org(remote, org):
            candidates.setdefault(cwd, set()).add(remote)
    return {cwd: next(iter(remotes)) for cwd, remotes in candidates.items() if len(remotes) == 1}


def read_codex_identity(path: Path) -> dict[str, str | None]:
    for row in iter_jsonl(path):
        if row.get("type") != "session_meta" or not isinstance(row.get("payload"), dict):
            continue
        payload = row["payload"]
        git = payload.get("git") if isinstance(payload.get("git"), dict) else {}
        return {
            "session_id": clean_string(payload.get("session_id")) or clean_string(payload.get("id")),
            "cwd": clean_string(payload.get("cwd")),
            "repo_remote": clean_string(git.get("repository_url")) or find_remote(payload),
        }
    return {"session_id": None, "cwd": None, "repo_remote": None}


def read_claude_identity(path: Path) -> dict[str, str | None]:
    identity: dict[str, str | None] = {"session_id": None, "cwd": None, "repo_remote": None}
    for row in iter_jsonl(path):
        identity["session_id"] = identity["session_id"] or clean_string(row.get("sessionId"))
        identity["cwd"] = identity["cwd"] or clean_string(row.get("cwd"))
        identity["repo_remote"] = identity["repo_remote"] or find_remote(row)
        if all(identity.values()):
            break
    return identity


def find_remote(value: Any, *, depth: int = 0) -> str | None:
    if depth > 3 or not isinstance(value, dict):
        return None
    for key, item in value.items():
        normalized = str(key).replace("-", "_").lower()
        if normalized in REMOTE_KEYS and isinstance(item, str):
            return item
    for key in ("git", "metadata", "repository", "repo"):
        nested = value.get(key)
        found = find_remote(nested, depth=depth + 1)
        if found:
            return found
    return None


def iter_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    try:
        handle = path.open(encoding="utf-8", errors="replace")
    except OSError:
        return
    with handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                yield value


def claude_logged_session_ids(claude_home: Path) -> set[str]:
    path = claude_home / "session-logging" / "events.jsonl"
    session_ids: set[str] = set()
    if not path.exists():
        return session_ids
    for row in iter_jsonl(path):
        session_id = clean_string(row.get("session_id")) or clean_string(row.get("sessionId"))
        if session_id:
            session_ids.add(session_id)
    return session_ids


def git_origin_remote(cwd: str) -> str | None:
    path = Path(cwd).expanduser()
    if not path.exists():
        return None
    try:
        completed = subprocess.run(
            ["git", "-C", str(path), "remote", "get-url", "origin"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return completed.stdout.strip() or None if completed.returncode == 0 else None


def remote_belongs_to_org(remote: str | None, org: str) -> bool:
    if not remote:
        return False
    pattern = rf"(?:github\.com|www\.github\.com)[:/]+{re.escape(org)}(?:/|$)"
    return re.search(pattern, remote, flags=re.IGNORECASE) is not None


def write_session_file(
    archive: zipfile.ZipFile,
    source: Path,
    archive_path: str,
    *,
    redact: bool,
) -> dict[str, int | str]:
    digest = hashlib.sha256()
    byte_count = 0
    redactions = 0
    if not redact:
        with source.open("rb") as handle, archive.open(archive_path, "w") as target:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                target.write(chunk)
                digest.update(chunk)
                byte_count += len(chunk)
        return {"sha256": digest.hexdigest(), "bytes": byte_count, "redactions": 0}

    with source.open(encoding="utf-8", errors="replace") as handle, archive.open(archive_path, "w") as target:
        for line in handle:
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                rendered, count = redact_text(line)
            else:
                redacted_value, count = redact_json_value(parsed)
                rendered = json.dumps(redacted_value, ensure_ascii=False, separators=(",", ":")) + "\n"
            data = rendered.encode("utf-8")
            target.write(data)
            digest.update(data)
            byte_count += len(data)
            redactions += count
    return {"sha256": digest.hexdigest(), "bytes": byte_count, "redactions": redactions}


def redact_text(text: str) -> tuple[str, int]:
    result, count = PRIVATE_KEY_PATTERN.subn(REDACTED, text)
    for pattern in SECRET_PATTERNS:
        if pattern.groups == 3:
            result, replaced = pattern.subn(rf"\1{REDACTED}\3", result)
        elif pattern.groups:
            result, replaced = pattern.subn(rf"\1{REDACTED}", result)
        else:
            result, replaced = pattern.subn(REDACTED, result)
        count += replaced
    return result, count


def redact_json_value(value: Any) -> tuple[Any, int]:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, list):
        output: list[Any] = []
        count = 0
        for item in value:
            rendered, replacements = redact_json_value(item)
            output.append(rendered)
            count += replacements
        return output, count
    if isinstance(value, dict):
        output: dict[str, Any] = {}
        count = 0
        for key, item in value.items():
            rendered, replacements = redact_json_value(item)
            output[key] = rendered
            count += replacements
        return output, count
    return value, 0


def manifest_entry(item: SessionFile) -> dict[str, Any]:
    value = asdict(item)
    value["path"] = str(item.path)
    return value


def archive_readme(*, org: str, redacted: bool) -> str:
    mode = "Common credential patterns were redacted." if redacted else "WARNING: This is a raw, unredacted export."
    return (
        f"Local Codex and Claude session export for GitHub organization {org}.\n\n"
        f"{mode}\n"
        "manifest.json lists each included source file, repository verification method, archive path, "
        "byte size, SHA-256 digest, and redaction count. Original JSONL files remain grouped by client.\n"
        "Sessions that could not be verified as belonging to the organization were omitted.\n"
    )


def clean_string(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


if __name__ == "__main__":
    sys.exit(main())
