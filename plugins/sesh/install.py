"""Install the standalone `sesh` Codex plugin by hand, until Jolly Roger delivers it (COR-4884).

Run from a codex-plugins checkout (any commit that contains this file):

    python3 plugins/sesh/install.py              # install or repair; safe to rerun
    python3 plugins/sesh/install.py --uninstall  # remove it again (e.g. before Jolly Roger takes over)

What it does, using only Codex's own plugin commands (the same ones setup.py uses):

1. Copies `plugins/sesh` at the pinned commit below (`git archive`, no working-tree files) into a
   private local marketplace `e3-sesh` at `$CODEX_HOME/sesh/marketplace/<commit>/`.
2. `codex plugin marketplace add` that folder and `codex plugin add sesh@e3-sesh`. Codex copies the
   plugin into `$CODEX_HOME/plugins/cache/e3-sesh/sesh/<version>/` and writes
   `[plugins."sesh@e3-sesh"] enabled = true` to `config.toml`.
3. Checks that every other plugin and marketplace is exactly as before.

It touches nothing else: no other plugin or marketplace, no hooks.json, no scheduler, and no Sesh
state under `$CODEX_HOME/sesh/session-logging`. It works with or without codex-session-logging; when
that logger is active, the sesh hooks step aside at run time so nothing fires twice.

If another `sesh` plugin is already installed (for example `sesh@jolly-roger-sesh` from the Jolly
Roger native catalog), it does nothing, because two copies would run every hook twice.
Jolly Roger's legacy retirement only removes `@coreedge-local` plugins, so it leaves this one alone.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

PLUGIN = "sesh"
MARKETPLACE = "e3-sesh"
PLUGIN_ID = f"{PLUGIN}@{MARKETPLACE}"
# The codex-plugins main merge that contains this plugin release (#97). Bump both together.
COMMIT = "f22c6ec22ca4b2e609b46e3a2896ead93ee00108"
VERSION = "0.1.0"
PLUGIN_PATH = "plugins/sesh"
REPOSITORY = "https://github.com/e3-solutions/codex-plugins.git"
CODEX_CANDIDATES = (
    "~/.local/bin/codex",
    "/opt/homebrew/bin/codex",
    "/usr/local/bin/codex",
    "/home/linuxbrew/.linuxbrew/bin/codex",
    "/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex",
)
MAX_FILES = 512
MAX_BYTES = 8 * 1024 * 1024


class InstallError(RuntimeError):
    pass


def codex_home(env=None) -> Path:
    env = os.environ if env is None else env
    return Path(env.get("CODEX_HOME") or Path(env.get("HOME") or Path.home()) / ".codex").expanduser()


def find_codex(explicit=None) -> str:
    if explicit:
        return explicit
    found = shutil.which("codex")
    if found:
        return found
    for candidate in CODEX_CANDIDATES:
        path = Path(candidate).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    raise InstallError("Codex CLI not found. Install Codex, or pass --codex /path/to/codex.")


def _codex(codex: str, home: Path, *args: str) -> str:
    env = dict(os.environ, CODEX_HOME=str(home))
    result = subprocess.run([codex, "plugin", *args], text=True, capture_output=True, env=env,
                            timeout=120, check=False)
    if result.returncode:
        raise InstallError(f"codex plugin {' '.join(args)} failed: {(result.stderr or result.stdout).strip()[:500]}")
    return result.stdout


def inventory(codex: str, home: Path):
    """(installed plugins by id, marketplaces by name) from Codex itself."""
    try:
        installed = json.loads(_codex(codex, home, "list", "--json"))["installed"]
        markets = json.loads(_codex(codex, home, "marketplace", "list", "--json"))["marketplaces"]
    except (ValueError, KeyError, TypeError) as error:
        raise InstallError("could not read the Codex plugin inventory") from error
    if not isinstance(installed, list) or not isinstance(markets, list):
        raise InstallError("could not read the Codex plugin inventory")
    plugins = {item["pluginId"]: item for item in installed
               if isinstance(item, dict) and isinstance(item.get("pluginId"), str)}
    marketplaces = {item["name"]: item for item in markets
                    if isinstance(item, dict) and isinstance(item.get("name"), str)}
    return plugins, marketplaces


def _others(plugins: dict, marketplaces: dict):
    return ({k: v for k, v in plugins.items() if k != PLUGIN_ID},
            {k: v for k, v in marketplaces.items() if k != MARKETPLACE})


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _git(repo: Path, *args: str, binary=False):
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, timeout=120, check=False)
    if result.returncode:
        raise InstallError(f"git {' '.join(args)} failed: {result.stderr.decode(errors='replace').strip()[:500]}")
    return result.stdout if binary else result.stdout.decode()


def plugin_files(repo: Path, commit: str = COMMIT) -> dict:
    """The plugin's files at the pinned commit, as {relative path: (bytes, mode)}."""
    probe = subprocess.run(["git", "-C", str(repo), "cat-file", "-e", commit + "^{commit}"],
                           capture_output=True, check=False)
    if probe.returncode:
        _git(repo, "fetch", "--quiet", "--no-tags", REPOSITORY, commit)
    raw = _git(repo, "archive", "--format=tar", commit, PLUGIN_PATH, binary=True)
    files, total = {}, 0
    prefix = PurePosixPath(PLUGIN_PATH)
    with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
        for member in archive.getmembers():
            if member.isdir() or member.name == "pax_global_header":
                continue
            path = PurePosixPath(member.name)
            if not member.isfile() or path.is_absolute() or ".." in path.parts:
                raise InstallError(f"unsafe file in plugin archive: {member.name}")
            relative = str(path.relative_to(prefix))
            content = archive.extractfile(member).read()
            total += len(content)
            files[relative] = (content, 0o755 if member.mode & 0o111 else 0o644)
    if len(files) > MAX_FILES or total > MAX_BYTES:
        raise InstallError("plugin archive is too large")
    try:
        manifest = json.loads(files[".codex-plugin/plugin.json"][0])
    except (KeyError, ValueError) as error:
        raise InstallError("plugin manifest missing at the pinned commit") from error
    if manifest.get("name") != PLUGIN or manifest.get("version") != VERSION:
        raise InstallError(f"pinned commit has {manifest.get('name')} {manifest.get('version')}, expected {PLUGIN} {VERSION}")
    return files


def marketplace_manifest() -> bytes:
    document = {"name": MARKETPLACE, "interface": {"displayName": "E3 Sesh"},
                "plugins": [{"name": PLUGIN, "source": {"source": "local", "path": f"./plugins/{PLUGIN}"},
                             "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
                             "category": "Productivity"}]}
    return (json.dumps(document, indent=2, sort_keys=True) + "\n").encode()


def release_files(files: dict) -> dict:
    result = {f"plugins/{PLUGIN}/{path}": value for path, value in files.items()}
    result[".agents/plugins/marketplace.json"] = (marketplace_manifest(), 0o644)
    return result


def _matches(root: Path, expected: dict) -> bool:
    if root.is_symlink() or not root.is_dir():
        return False
    seen = set()
    for path in root.rglob("*"):
        if path.is_symlink():
            return False
        if path.is_dir():
            continue
        relative = path.relative_to(root).as_posix()
        if relative not in expected or path.read_bytes() != expected[relative][0]:
            return False
        seen.add(relative)
    return seen == set(expected)


def stage(home: Path, files: dict, commit: str = COMMIT) -> Path:
    """Write the private marketplace for `commit`, or verify an existing one byte for byte."""
    parent = home / PLUGIN / "marketplace"
    root = parent / commit
    expected = release_files(files)
    if _matches(root, expected):
        return root
    parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=parent, prefix=".stage-") as temporary:
        staged = Path(temporary) / "marketplace"
        for relative, (content, mode) in expected.items():
            target = staged / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            target.chmod(mode)
        if root.exists() or root.is_symlink():
            if root.is_symlink() or not root.is_dir():
                root.unlink()
            else:
                shutil.rmtree(root)
        os.replace(staged, root)
    return root


def _prune(home: Path, keep: str | None) -> None:
    parent = home / PLUGIN / "marketplace"
    if parent.is_symlink() or not parent.is_dir():
        return
    for child in parent.iterdir():
        if child.name != keep and not child.is_symlink() and child.is_dir():
            shutil.rmtree(child)
    if keep is None:
        try:
            parent.rmdir()
        except OSError:
            pass


def _cache_ok(home: Path) -> bool:
    return (home / "plugins" / "cache" / MARKETPLACE / PLUGIN / VERSION / "scripts" / "session_start.py").is_file()


def logger_active(plugins: dict) -> bool:
    return any(pid.split("@", 1)[0] == "codex-session-logging" and item.get("enabled") is not False
               for pid, item in plugins.items())


def install(home: Path, codex: str, repo: Path) -> dict:
    home.mkdir(parents=True, exist_ok=True)  # Codex refuses a CODEX_HOME that does not exist.
    plugins, marketplaces = inventory(codex, home)
    others = [pid for pid, item in plugins.items() if item.get("name") == PLUGIN and pid != PLUGIN_ID]
    if others:
        return {"status": "skipped", "reason": f"{others[0]} is already installed; not adding a second copy"}
    occupied = [pid for pid, item in plugins.items()
                if item.get("marketplaceName") == MARKETPLACE and pid != PLUGIN_ID]
    if occupied:
        raise InstallError(f"marketplace {MARKETPLACE} has another plugin installed ({occupied[0]}); not touching it")
    before = _others(plugins, marketplaces)

    root = stage(home, plugin_files(repo))
    actions = []
    market = marketplaces.get(MARKETPLACE)
    source = (market or {}).get("marketplaceSource") or {}
    if market is not None and (source.get("sourceType") != "local" or source.get("source") != str(root)):
        _codex(codex, home, "marketplace", "remove", MARKETPLACE)
        market = None
        actions.append("replaced marketplace")
    if market is None:
        _codex(codex, home, "marketplace", "add", str(root), "--json")
        actions.append("added marketplace")
    current = plugins.get(PLUGIN_ID)
    plugin_source = (current or {}).get("source") or {}
    if (current is None or current.get("enabled") is False or current.get("version") != VERSION
            or plugin_source.get("path") != str(root / "plugins" / PLUGIN) or not _cache_ok(home)):
        _codex(codex, home, "add", PLUGIN_ID, "--json")
        actions.append("installed plugin")

    plugins, marketplaces = inventory(codex, home)
    item = plugins.get(PLUGIN_ID) or {}
    if item.get("version") != VERSION or item.get("enabled") is False or not _cache_ok(home):
        raise InstallError("sesh did not install as expected")
    if _others(plugins, marketplaces) != before:
        raise InstallError("another plugin or marketplace changed during install; please check `codex plugin list`")
    _prune(home, COMMIT)
    return {"status": "installed" if actions else "unchanged", "plugin": PLUGIN_ID, "version": VERSION,
            "commit": COMMIT, "actions": actions,
            "codex_session_logging_active": logger_active(plugins)}


def uninstall(home: Path, codex: str) -> dict:
    if not home.is_dir():
        return {"status": "absent", "actions": []}
    plugins, marketplaces = inventory(codex, home)
    before = _others(plugins, marketplaces)
    actions = []
    if PLUGIN_ID in plugins:
        _codex(codex, home, "remove", PLUGIN_ID, "--json")
        actions.append("removed plugin")
    plugins, _ = inventory(codex, home)
    if MARKETPLACE in marketplaces:
        if any(item.get("marketplaceName") == MARKETPLACE for item in plugins.values()):
            raise InstallError(f"marketplace {MARKETPLACE} has another plugin installed; left it in place")
        _codex(codex, home, "marketplace", "remove", MARKETPLACE)
        actions.append("removed marketplace")
    plugins, marketplaces = inventory(codex, home)
    if PLUGIN_ID in plugins or MARKETPLACE in marketplaces:
        raise InstallError("sesh did not uninstall cleanly")
    if _others(plugins, marketplaces) != before:
        raise InstallError("another plugin or marketplace changed during uninstall; please check `codex plugin list`")
    _prune(home, None)
    cache = home / "plugins" / "cache" / MARKETPLACE
    if cache.is_dir() and not cache.is_symlink() and not any(cache.rglob("*.py")):
        shutil.rmtree(cache)
    return {"status": "removed" if actions else "absent", "actions": actions}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Install the standalone sesh Codex plugin (COR-4884).")
    parser.add_argument("--uninstall", action="store_true", help="remove sesh@e3-sesh and its marketplace")
    parser.add_argument("--codex", help="path to the Codex CLI (default: found on PATH)")
    args = parser.parse_args(argv)
    home = codex_home()
    try:
        codex = find_codex(args.codex)
        result = uninstall(home, codex) if args.uninstall else install(home, codex, _repo_root())
    except (InstallError, OSError, subprocess.TimeoutExpired) as error:
        print(f"sesh install failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    if result["status"] in ("installed", "unchanged"):
        default = Path(os.environ.get("HOME") or Path.home()) / ".codex"
        if home.resolve() != default.resolve():
            print(f"Note: sesh hooks look in {default}; CODEX_HOME is {home}.", file=sys.stderr)
        if result.get("codex_session_logging_active"):
            print("codex-session-logging is active here, so sesh steps aside and the logger runs the Sesh hooks.",
                  file=sys.stderr)
        print("Done. Start a new Codex thread; if Codex asks, trust the sesh hooks once.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
