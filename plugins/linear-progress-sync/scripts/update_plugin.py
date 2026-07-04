#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from linear_sync import global_config_dir, install_codex_hooks, write_json_atomic


JsonDict = dict[str, Any]
PLUGIN_NAME = "linear-progress-sync"
DEFAULT_INTERVAL_SECONDS = 6 * 60 * 60
DEFAULT_MANIFEST_URL = (
    "https://raw.githubusercontent.com/e3-solutions/codex-plugins/main/"
    "plugins/linear-progress-sync/update-manifest.json"
)
AUTO_UPDATE_ENV = "LINEAR_SYNC_AUTO_UPDATE"
MANIFEST_URL_ENV = "LINEAR_SYNC_UPDATE_MANIFEST_URL"


def current_plugin_root() -> Path:
    return Path(__file__).resolve().parents[1]


def default_cache_parent(plugin_root: Path | None = None) -> Path:
    root = Path(plugin_root or current_plugin_root()).resolve()
    if root.parent.name == PLUGIN_NAME:
        return root.parent
    return Path.home() / ".codex" / "plugins" / "cache" / "coreedge-local" / PLUGIN_NAME


def update_state_path() -> Path:
    return global_config_dir() / "update.json"


def read_update_state(path: str | Path | None = None) -> JsonDict:
    state_path = Path(path or update_state_path())
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def write_update_state(path: str | Path, payload: JsonDict) -> None:
    write_json_atomic(Path(path), payload)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def now_iso(now: datetime | None = None) -> str:
    current = now or utc_now()
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def auto_update_enabled(state: JsonDict | None = None) -> bool:
    env = os.environ.get(AUTO_UPDATE_ENV)
    if isinstance(env, str) and env.strip().lower() in {"0", "false", "no", "off"}:
        return False
    config = state or {}
    return config.get("enabled") is not False


def should_check_for_update(
    state: JsonDict,
    *,
    now: datetime | None = None,
    interval_seconds: int = DEFAULT_INTERVAL_SECONDS,
) -> bool:
    last_checked = parse_time(state.get("last_checked_at"))
    if last_checked is None:
        return True
    current = now or utc_now()
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return (current.astimezone(timezone.utc) - last_checked).total_seconds() >= interval_seconds


def version_parts(version: str) -> tuple[int, ...]:
    parts: list[int] = []
    for piece in str(version or "").replace("-", ".").split("."):
        digits = ""
        for char in piece:
            if char.isdigit():
                digits += char
            else:
                break
        parts.append(int(digits or 0))
    return tuple(parts or [0])


def version_is_newer(candidate: str, current: str) -> bool:
    left = list(version_parts(candidate))
    right = list(version_parts(current))
    size = max(len(left), len(right))
    left.extend([0] * (size - len(left)))
    right.extend([0] * (size - len(right)))
    return tuple(left) > tuple(right)


def plugin_version(plugin_root: str | Path) -> str:
    manifest_path = Path(plugin_root) / ".codex-plugin" / "plugin.json"
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    version = data.get("version") if isinstance(data, dict) else None
    if not isinstance(version, str) or not version.strip():
        raise ValueError(f"{manifest_path} missing plugin version")
    return version.strip()


def read_manifest(url: str) -> JsonDict:
    with urllib.request.urlopen(url, timeout=20) as response:
        data = json.loads(response.read().decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("update manifest must be a JSON object")
    return data


def download_file(url: str, destination: Path) -> None:
    with urllib.request.urlopen(url, timeout=60) as response, destination.open("wb") as handle:
        shutil.copyfileobj(response, handle)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_sha256(path: Path, expected: str | None) -> None:
    if not expected:
        return
    actual = sha256_file(path)
    if actual.lower() != expected.strip().lower():
        raise ValueError(f"SHA256 mismatch for update archive: expected {expected}, got {actual}")


def extract_archive(archive: Path, destination: Path) -> None:
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as zip_file:
            for member in zip_file.infolist():
                target = safe_extract_path(destination, member.filename)
                if member.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with zip_file.open(member) as source, target.open("wb") as handle:
                    shutil.copyfileobj(source, handle)
        return
    if tarfile.is_tarfile(archive):
        with tarfile.open(archive) as tar_file:
            for member in tar_file.getmembers():
                target = safe_extract_path(destination, member.name)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                source = tar_file.extractfile(member)
                if source is None:
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with source, target.open("wb") as handle:
                    shutil.copyfileobj(source, handle)
        return
    raise ValueError(f"unsupported update archive format: {archive}")


def safe_extract_path(destination: Path, member_name: str) -> Path:
    target = (destination / member_name).resolve()
    root = destination.resolve()
    if root != target and root not in target.parents:
        raise ValueError(f"unsafe archive member path: {member_name}")
    return target


def has_plugin_manifest(path: Path) -> bool:
    return (path / ".codex-plugin" / "plugin.json").is_file()


def find_plugin_dir(extracted_root: Path, plugin_subdir: str | None = None) -> Path:
    if plugin_subdir:
        parts = Path(plugin_subdir).parts
        direct = extracted_root.joinpath(*parts)
        if has_plugin_manifest(direct):
            return direct
        for candidate in extracted_root.rglob(parts[-1]):
            if not candidate.is_dir():
                continue
            if candidate.parts[-len(parts) :] == parts and has_plugin_manifest(candidate):
                return candidate
    for candidate in extracted_root.rglob(".codex-plugin"):
        plugin_root = candidate.parent
        if has_plugin_manifest(plugin_root):
            return plugin_root
    raise ValueError("update archive did not contain a Codex plugin")


def install_plugin_dir(source: Path, *, cache_parent: Path, version: str) -> Path:
    cache_parent.mkdir(parents=True, exist_ok=True)
    target = cache_parent / version
    temp_parent = Path(tempfile.mkdtemp(prefix=f".{version}.", dir=str(cache_parent)))
    temp_target = temp_parent / version
    try:
        shutil.copytree(source, temp_target)
        if target.exists():
            shutil.rmtree(target)
        shutil.move(str(temp_target), str(target))
    finally:
        shutil.rmtree(temp_parent, ignore_errors=True)
    return target


def update_lock_path(state_path: Path) -> Path:
    return state_path.with_suffix(state_path.suffix + ".lock")


def acquire_lock(lock_path: Path) -> int:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    os.write(fd, str(os.getpid()).encode("utf-8"))
    return fd


def release_lock(fd: int | None, lock_path: Path) -> None:
    if fd is not None:
        os.close(fd)
    try:
        lock_path.unlink()
    except FileNotFoundError:
        pass


def run_update(
    *,
    current_plugin_root: str | Path | None = None,
    cache_parent: str | Path | None = None,
    manifest_url: str | None = None,
    state_path: str | Path | None = None,
    force: bool = False,
    now: datetime | None = None,
    interval_seconds: int = DEFAULT_INTERVAL_SECONDS,
    install_hooks: bool = True,
    use_lock: bool = True,
) -> JsonDict:
    root = Path(current_plugin_root or globals()["current_plugin_root"]()).resolve()
    state_file = Path(state_path or update_state_path())
    state = read_update_state(state_file)
    current_version = plugin_version(root)
    current_time = now or utc_now()
    if not auto_update_enabled(state):
        return {"updated": False, "skipped": "disabled", "current_version": current_version}
    if not force and not should_check_for_update(state, now=current_time, interval_seconds=interval_seconds):
        return {"updated": False, "skipped": "not due", "current_version": current_version}

    lock_fd: int | None = None
    lock_path = update_lock_path(state_file)
    if use_lock:
        try:
            lock_fd = acquire_lock(lock_path)
        except FileExistsError:
            return {"updated": False, "skipped": "locked", "current_version": current_version}

    try:
        url = manifest_url or os.environ.get(MANIFEST_URL_ENV) or state.get("manifest_url") or DEFAULT_MANIFEST_URL
        manifest = read_manifest(str(url))
        latest_version = str(manifest.get("version") or "").strip()
        archive_url = str(manifest.get("archive_url") or "").strip()
        if not latest_version or not archive_url:
            raise ValueError("update manifest requires version and archive_url")

        next_state = dict(state)
        next_state.update(
            {
                "last_checked_at": now_iso(current_time),
                "latest_version": latest_version,
                "manifest_url": str(url),
            }
        )

        if not version_is_newer(latest_version, current_version):
            next_state["last_result"] = "current"
            write_update_state(state_file, next_state)
            return {
                "updated": False,
                "skipped": "current",
                "current_version": current_version,
                "latest_version": latest_version,
            }

        parent = Path(cache_parent) if cache_parent else default_cache_parent(root)
        with tempfile.TemporaryDirectory(prefix="linear-progress-sync-update.") as temp_dir:
            temp = Path(temp_dir)
            archive = temp / "update-archive"
            download_file(archive_url, archive)
            verify_sha256(archive, str(manifest.get("sha256") or "").strip() or None)
            extract_dir = temp / "extract"
            extract_dir.mkdir()
            extract_archive(archive, extract_dir)
            plugin_dir = find_plugin_dir(extract_dir, str(manifest.get("plugin_subdir") or "").strip() or None)
            installed = install_plugin_dir(plugin_dir, cache_parent=parent, version=latest_version)

        if install_hooks:
            install_codex_hooks(plugin_repo_root=installed)

        next_state["last_result"] = "updated"
        next_state["installed_version"] = latest_version
        write_update_state(state_file, next_state)
        return {
            "updated": True,
            "current_version": current_version,
            "installed_version": latest_version,
            "path": str(parent / latest_version),
        }
    except Exception as exc:
        next_state = dict(state)
        next_state.update({"last_checked_at": now_iso(current_time), "last_result": "failed", "last_error": str(exc)})
        write_update_state(state_file, next_state)
        raise
    finally:
        if use_lock:
            release_lock(lock_fd, lock_path)


def maybe_spawn_auto_update(
    *,
    plugin_root: str | Path | None = None,
    state_path: str | Path | None = None,
    interval_seconds: int = DEFAULT_INTERVAL_SECONDS,
) -> JsonDict:
    state_file = Path(state_path or update_state_path())
    state = read_update_state(state_file)
    if not auto_update_enabled(state):
        return {"spawned": False, "reason": "disabled"}
    if not should_check_for_update(state, interval_seconds=interval_seconds):
        return {"spawned": False, "reason": "not due"}
    script = Path(__file__).resolve()
    args = [sys.executable, str(script)]
    if plugin_root:
        args.extend(["--plugin-root", str(plugin_root)])
    if state_path:
        args.extend(["--state-path", str(state_file)])
    subprocess.Popen(
        args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        close_fds=True,
    )
    return {"spawned": True}


def main() -> None:
    parser = argparse.ArgumentParser(description="Update the installed Linear Progress Sync plugin cache.")
    parser.add_argument("--plugin-root", default=str(current_plugin_root()), help="Currently running plugin root.")
    parser.add_argument("--cache-parent", help="Installed plugin version parent directory.")
    parser.add_argument("--manifest-url", help="Override update manifest URL.")
    parser.add_argument("--state-path", help="Override update state path.")
    parser.add_argument("--force", action="store_true", help="Check even when the throttle interval has not elapsed.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable output.")
    args = parser.parse_args()

    result = run_update(
        current_plugin_root=args.plugin_root,
        cache_parent=args.cache_parent,
        manifest_url=args.manifest_url,
        state_path=args.state_path,
        force=args.force,
    )
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    elif result.get("updated"):
        print(f"Installed Linear Progress Sync {result['installed_version']}.")
    elif result.get("skipped"):
        print(f"Linear Progress Sync update skipped: {result['skipped']}.")


if __name__ == "__main__":
    main()
