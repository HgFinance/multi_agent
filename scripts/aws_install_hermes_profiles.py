#!/usr/bin/env python3
"""Atomically merge repository-owned AWS Hermes profile fragments.

The AWS runtime profiles contain host-specific integrations and previously
authenticated provider state.  This helper therefore owns only three narrow
targets: one Trading MCP entry and one marked SOUL section in each of CEO and
Trading.  It never replaces a whole runtime profile.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
from pathlib import Path
from typing import Sequence

import yaml


SCHEMA_VERSION = "hgfinance.aws-hermes-profile-backup.v2"
MCP_KEY = "MCP_TRADING_ORDER_API_KEY"
MCP_PLACEHOLDER = "${MCP_TRADING_ORDER_API_KEY}"
MAX_PROFILE_FILE_BYTES = 2 * 1024 * 1024
PROFILE_FILES_BY_PROFILE = (
    ("ceo-agent", ("SOUL.md",)),
    ("trading-department", ("config.yaml", "SOUL.md")),
)
PROFILE_TARGETS = tuple(
    (profile, filename)
    for profile, filenames in PROFILE_FILES_BY_PROFILE
    for filename in filenames
)
TRADING_CONFIG_SOURCE = Path("departments/02-trading/hermes/config.yaml")
SOUL_SOURCES = {
    "ceo-agent": (
        Path("departments/00-ceo-office/hermes/SOUL.md"),
        "## Marked direct user PAPER-order lane",
    ),
    "trading-department": (
        Path("departments/02-trading/hermes/SOUL.md"),
        "## Marked direct user PAPER-order interpretation lane",
    ),
}
_ASSIGNMENT_RE = re.compile(
    r"^\s*(?:export\s+)?(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*=(?P<value>.*)$"
)
_H2_RE = re.compile(r"(?m)^##[ \t]+[^\r\n]+[ \t]*\r?$")
_PLACEHOLDER_MARKERS = (
    "${",
    "change_me",
    "changeme",
    "example",
    "placeholder",
    "replace_me",
    "secret_here",
    "your_api_key",
)


class ProfileInstallError(RuntimeError):
    """Non-disclosing profile deployment error."""


def _logical_dotenv_value(raw: str) -> str:
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    for index, character in enumerate(value):
        if character == "#" and index > 0 and value[index - 1].isspace():
            return value[:index].rstrip()
    return value


def _read_mcp_key(runtime_env: Path) -> str:
    try:
        lines = runtime_env.read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeError) as exc:
        raise ProfileInstallError("private runtime environment is unreadable") from exc
    value: str | None = None
    for line in lines:
        match = _ASSIGNMENT_RE.match(line)
        if match is not None and match.group("key") == MCP_KEY:
            value = _logical_dotenv_value(match.group("value"))
    lowered = str(value or "").casefold()
    if (
        value is None
        or len(value.encode("utf-8")) < 32
        or any(marker in lowered for marker in _PLACEHOLDER_MARKERS)
        or len(set(value)) == 1
        or any(ord(character) < 33 or ord(character) > 126 for character in value)
    ):
        raise ProfileInstallError(f"{MCP_KEY} is missing or invalid")
    return value


def _read_file(path: Path, *, source: str, allow_empty: bool = False) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise ProfileInstallError(f"{source} is invalid")
    try:
        size = path.stat().st_size
        if size > MAX_PROFILE_FILE_BYTES or (size == 0 and not allow_empty):
            raise ProfileInstallError(f"{source} is invalid")
        return path.read_bytes()
    except OSError as exc:
        raise ProfileInstallError(f"{source} is unreadable") from exc


def _utf8(data: bytes, *, source: str) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeError as exc:
        raise ProfileInstallError(f"{source} is not valid UTF-8") from exc


def _yaml_mapping(data: bytes, *, source: str) -> dict[object, object]:
    try:
        document = yaml.safe_load(_utf8(data, source=source))
    except yaml.YAMLError as exc:
        raise ProfileInstallError(f"{source} is not valid YAML") from exc
    if not isinstance(document, dict):
        raise ProfileInstallError(f"{source} must be a YAML mapping")
    return document


def _merge_trading_config(
    *, release_data: bytes, runtime_data: bytes, mcp_key: str
) -> bytes:
    release_text = _utf8(release_data, source="repository Trading config")
    if release_text.count(MCP_PLACEHOLDER) != 1:
        raise ProfileInstallError(
            "repository Trading config has an invalid MCP credential marker"
        )
    release = _yaml_mapping(release_data, source="repository Trading config")
    release_servers = release.get("mcp_servers")
    if not isinstance(release_servers, dict):
        raise ProfileInstallError("repository Trading config has no MCP mapping")
    release_order_server = release_servers.get("user-paper-order")
    if not isinstance(release_order_server, dict):
        raise ProfileInstallError("repository Trading config has no PAPER-order MCP")
    headers = release_order_server.get("headers")
    if (
        not isinstance(headers, dict)
        or headers.get("Authorization") != f"Bearer {MCP_PLACEHOLDER}"
    ):
        raise ProfileInstallError(
            "repository Trading MCP has an invalid credential header"
        )
    headers["Authorization"] = f"Bearer {mcp_key}"

    runtime = _yaml_mapping(runtime_data, source="runtime Trading config")
    runtime_servers = runtime.get("mcp_servers")
    if runtime_servers is None:
        runtime_servers = {}
        runtime["mcp_servers"] = runtime_servers
    if not isinstance(runtime_servers, dict):
        raise ProfileInstallError("runtime Trading MCP settings are invalid")
    runtime_servers["user-paper-order"] = release_order_server
    try:
        rendered = yaml.safe_dump(
            runtime,
            allow_unicode=True,
            default_flow_style=False,
            sort_keys=False,
            width=4096,
        ).encode("utf-8")
    except yaml.YAMLError as exc:
        raise ProfileInstallError("runtime Trading config could not be rendered") from exc
    if len(rendered) > MAX_PROFILE_FILE_BYTES:
        raise ProfileInstallError("runtime Trading config rendering failed")
    verified = _yaml_mapping(rendered, source="rendered runtime Trading config")
    verified_servers = verified.get("mcp_servers")
    verified_order = (
        verified_servers.get("user-paper-order")
        if isinstance(verified_servers, dict)
        else None
    )
    verified_headers = (
        verified_order.get("headers") if isinstance(verified_order, dict) else None
    )
    if (
        not isinstance(verified_headers, dict)
        or verified_headers.get("Authorization") != f"Bearer {mcp_key}"
    ):
        raise ProfileInstallError("runtime Trading config rendering failed")
    return rendered


def _section_bounds(text: str, heading: str, *, source: str) -> tuple[int, int] | None:
    matches = list(
        re.finditer(rf"(?m)^{re.escape(heading)}[ \t]*\r?$", text)
    )
    if len(matches) > 1:
        raise ProfileInstallError(f"{source} contains duplicate managed sections")
    if not matches:
        return None
    start = matches[0].start()
    following = _H2_RE.search(text, matches[0].end())
    return start, following.start() if following is not None else len(text)


def _newline_style(text: str) -> str:
    return "\r\n" if text.count("\r\n") > text.count("\n") // 2 else "\n"


def _merge_soul_section(
    *, release_data: bytes, runtime_data: bytes, heading: str, profile: str
) -> bytes:
    release = _utf8(release_data, source=f"repository {profile} SOUL")
    runtime = _utf8(runtime_data, source=f"runtime {profile} SOUL")
    release_bounds = _section_bounds(
        release, heading, source=f"repository {profile} SOUL"
    )
    if release_bounds is None:
        raise ProfileInstallError(f"repository {profile} SOUL lacks its managed section")
    runtime_bounds = _section_bounds(runtime, heading, source=f"runtime {profile} SOUL")
    release_section = release[slice(*release_bounds)].rstrip(" \t\r\n")
    newline = _newline_style(runtime)
    managed = release_section.replace("\r\n", "\n").replace("\r", "\n")
    managed = managed.replace("\n", newline)

    if runtime_bounds is None:
        if runtime.endswith(("\n", "\r")):
            separator = newline if not runtime.endswith(("\n\n", "\r\n\r\n")) else ""
        else:
            separator = newline * 2
        merged = runtime + separator + managed + newline
    else:
        start, end = runtime_bounds
        suffix = runtime[end:]
        merged = runtime[:start] + managed + (newline * 2 if suffix else newline) + suffix
    encoded = merged.encode("utf-8")
    if len(encoded) > MAX_PROFILE_FILE_BYTES:
        raise ProfileInstallError(f"runtime {profile} SOUL rendering failed")
    return encoded


def _render_targets(
    *, release_root: Path, runtime_root: Path, mcp_key: str
) -> dict[tuple[str, str], bytes]:
    rendered: dict[tuple[str, str], bytes] = {}
    release_config = _read_file(
        release_root / TRADING_CONFIG_SOURCE,
        source="repository Trading config",
    )
    runtime_config = _read_file(
        runtime_root / "profiles/trading-department/config.yaml",
        source="runtime Trading config",
    )
    rendered[("trading-department", "config.yaml")] = _merge_trading_config(
        release_data=release_config,
        runtime_data=runtime_config,
        mcp_key=mcp_key,
    )
    for profile, (relative_source, heading) in SOUL_SOURCES.items():
        release_soul = _read_file(
            release_root / relative_source,
            source=f"repository {profile} SOUL",
        )
        runtime_soul = _read_file(
            runtime_root / "profiles" / profile / "SOUL.md",
            source=f"runtime {profile} SOUL",
        )
        rendered[(profile, "SOUL.md")] = _merge_soul_section(
            release_data=release_soul,
            runtime_data=runtime_soul,
            heading=heading,
            profile=profile,
        )
    return rendered


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _owner(path: Path) -> tuple[int, int] | None:
    try:
        metadata = path.stat()
    except OSError:
        return None
    return metadata.st_uid, metadata.st_gid


def _running_as_root() -> bool:
    return hasattr(os, "geteuid") and os.geteuid() == 0


def _ensure_directory(path: Path, *, mode: int) -> None:
    missing: list[Path] = []
    cursor = path
    while not cursor.exists():
        missing.append(cursor)
        parent = cursor.parent
        if parent == cursor:
            break
        cursor = parent
    inherited_owner = _owner(cursor)
    for directory in reversed(missing):
        directory.mkdir(mode=mode)
        if _running_as_root() and inherited_owner is not None:
            os.chown(directory, *inherited_owner)
        os.chmod(directory, mode)
        inherited_owner = _owner(directory)


def _atomic_write(path: Path, data: bytes, *, mode: int) -> None:
    _ensure_directory(path.parent, mode=0o700)
    target_owner = _owner(path) if path.exists() else _owner(path.parent)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, mode)
        else:
            os.chmod(temporary, mode)
        if _running_as_root() and target_owner is not None:
            os.fchown(descriptor, *target_owner)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        try:
            os.chmod(path, mode)
        except OSError:
            pass
        _fsync_directory(path.parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _validate_destination(runtime_root: Path) -> None:
    if runtime_root.is_symlink() or not runtime_root.is_dir():
        raise ProfileInstallError("Hermes runtime root is invalid")
    profiles_root = runtime_root / "profiles"
    if profiles_root.is_symlink() or not profiles_root.is_dir():
        raise ProfileInstallError("Hermes profiles root is invalid")
    for profile, filenames in PROFILE_FILES_BY_PROFILE:
        profile_dir = profiles_root / profile
        if profile_dir.is_symlink() or not profile_dir.is_dir():
            raise ProfileInstallError("Hermes runtime profile is invalid")
        for filename in filenames:
            target = profile_dir / filename
            if target.is_symlink() or (target.exists() and not target.is_file()):
                raise ProfileInstallError("Hermes runtime profile file is invalid")


def _backup_profiles(runtime_root: Path, backup_dir: Path) -> None:
    if backup_dir.is_symlink() or not backup_dir.is_dir():
        raise ProfileInstallError("Hermes profile backup directory is invalid")
    if any(backup_dir.iterdir()):
        raise ProfileInstallError("Hermes profile backup directory is not empty")

    profiles: list[dict[str, object]] = []
    for profile, filenames in PROFILE_FILES_BY_PROFILE:
        files: list[dict[str, object]] = []
        for filename in filenames:
            target = runtime_root / "profiles" / profile / filename
            data = _read_file(target, source="runtime Hermes profile target")
            mode = stat.S_IMODE(target.stat().st_mode)
            backup_target = backup_dir / "profiles" / profile / filename
            _atomic_write(backup_target, data, mode=0o600)
            files.append(
                {
                    "name": filename,
                    "present": True,
                    "mode": mode,
                    "sha256": _sha256(data),
                }
            )
        profiles.append({"profile": profile, "files": files})
    manifest = {"schema_version": SCHEMA_VERSION, "profiles": profiles}
    _atomic_write(
        backup_dir / "manifest.json",
        json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8"),
        mode=0o600,
    )


def _load_manifest(backup_dir: Path) -> dict[str, object]:
    try:
        manifest = json.loads((backup_dir / "manifest.json").read_text("utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise ProfileInstallError("Hermes profile backup manifest is invalid") from exc
    if not isinstance(manifest, dict) or manifest.get("schema_version") != SCHEMA_VERSION:
        raise ProfileInstallError("Hermes profile backup manifest is invalid")
    profiles = manifest.get("profiles")
    expected_profiles = {profile for profile, _filenames in PROFILE_FILES_BY_PROFILE}
    if (
        not isinstance(profiles, list)
        or len(profiles) != len(expected_profiles)
        or not all(isinstance(entry, dict) for entry in profiles)
        or {entry.get("profile") for entry in profiles} != expected_profiles
    ):
        raise ProfileInstallError("Hermes profile backup manifest is invalid")
    return manifest


def restore_profiles(*, runtime_root: Path, backup_dir: Path) -> None:
    """Restore exactly the three managed files captured before installation."""

    runtime_root = runtime_root.resolve(strict=False)
    backup_dir = backup_dir.resolve(strict=False)
    _validate_destination(runtime_root)
    manifest = _load_manifest(backup_dir)
    expected_by_profile = dict(PROFILE_FILES_BY_PROFILE)
    restore_plan: list[tuple[Path, bytes, int]] = []
    for profile_entry in manifest["profiles"]:  # type: ignore[index]
        if not isinstance(profile_entry, dict):
            raise ProfileInstallError("Hermes profile backup manifest is invalid")
        profile = profile_entry.get("profile")
        files = profile_entry.get("files")
        expected_files = set(expected_by_profile.get(str(profile), ()))
        if (
            not isinstance(profile, str)
            or not isinstance(files, list)
            or len(files) != len(expected_files)
            or not all(isinstance(entry, dict) for entry in files)
            or {entry.get("name") for entry in files} != expected_files
        ):
            raise ProfileInstallError("Hermes profile backup manifest is invalid")
        for entry in files:
            if (
                not isinstance(entry, dict)
                or entry.get("present") is not True
                or not isinstance(entry.get("name"), str)
            ):
                raise ProfileInstallError("Hermes profile backup manifest is invalid")
            filename = entry["name"]
            backup_target = backup_dir / "profiles" / profile / filename
            data = _read_file(
                backup_target,
                source="Hermes profile backup file",
                allow_empty=True,
            )
            if _sha256(data) != entry.get("sha256"):
                raise ProfileInstallError("Hermes profile backup checksum failed")
            mode = entry.get("mode")
            if (
                not isinstance(mode, int)
                or isinstance(mode, bool)
                or not 0 <= mode <= 0o777
            ):
                raise ProfileInstallError("Hermes profile backup mode is invalid")
            target = runtime_root / "profiles" / profile / filename
            restore_plan.append((target, data, mode))
    for target, data, mode in restore_plan:
        _atomic_write(target, data, mode=mode)


def install_profiles(
    *,
    release_root: Path,
    runtime_env: Path,
    runtime_root: Path,
    backup_dir: Path,
) -> None:
    """Merge the three managed targets as one rollback-capable operation."""

    release_root = release_root.resolve(strict=True)
    runtime_env = runtime_env.resolve(strict=True)
    runtime_root = runtime_root.resolve(strict=True)
    backup_dir = backup_dir.resolve(strict=True)
    key = _read_mcp_key(runtime_env)
    _validate_destination(runtime_root)
    rendered = _render_targets(
        release_root=release_root,
        runtime_root=runtime_root,
        mcp_key=key,
    )
    _backup_profiles(runtime_root, backup_dir)
    try:
        for profile, filename in PROFILE_TARGETS:
            _atomic_write(
                runtime_root / "profiles" / profile / filename,
                rendered[(profile, filename)],
                mode=0o600,
            )
    except Exception as exc:
        try:
            restore_profiles(runtime_root=runtime_root, backup_dir=backup_dir)
        except Exception:
            pass
        raise ProfileInstallError("Hermes profile installation failed") from exc


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    install = subparsers.add_parser("install")
    install.add_argument("--release-root", type=Path, required=True)
    install.add_argument("--runtime-env", type=Path, required=True)
    install.add_argument("--runtime-root", type=Path, required=True)
    install.add_argument("--backup-dir", type=Path, required=True)
    restore = subparsers.add_parser("restore")
    restore.add_argument("--runtime-root", type=Path, required=True)
    restore.add_argument("--backup-dir", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "install":
            install_profiles(
                release_root=args.release_root,
                runtime_env=args.runtime_env,
                runtime_root=args.runtime_root,
                backup_dir=args.backup_dir,
            )
        else:
            restore_profiles(
                runtime_root=args.runtime_root,
                backup_dir=args.backup_dir,
            )
    except (OSError, ProfileInstallError):
        print("ERROR: AWS Hermes profile operation failed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
