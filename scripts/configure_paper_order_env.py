#!/usr/bin/env python3
"""Safely configure the environment contract for natural-language PAPER orders.

The command edits one dotenv file in place.  Existing usable credentials are
kept, missing/unsafe credentials are generated independently, duplicate
managed assignments are collapsed, and the final file is installed with an
atomic same-directory ``os.replace``.  Secret values are never written to
stdout or stderr.

Examples::

    python scripts/configure_paper_order_env.py --runtime local
    python scripts/configure_paper_order_env.py --runtime aws --env-file .env.aws
    python scripts/configure_paper_order_env.py --runtime local \
        --rotate-key MCP_RESEARCH_API_KEY \
        --rotate-key MCP_TRADING_ORDER_API_KEY

``.env.example`` is the only tracked template. Runtime-specific ``.env.*``
files are private inputs, not additional templates or sources of truth.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import stat
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]

SERVICE_SECRET_KEYS = (
    "MCP_TRADING_ORDER_API_KEY",
    "TRADING_SERVICE_AUTH_SECRET",
    "TRADING_INTERNAL_SERVICE_AUTH_SECRET",
    "CEO_DISCORD_INGRESS_API_KEY",
)
AWS_DATABASE_SECRET_KEYS = (
    "HEDGEFUND_RUNTIME_DB_PASSWORD",
    "HEDGEFUND_ORDER_DB_PASSWORD",
    "HEDGEFUND_TRADING_DB_PASSWORD",
    "HEDGEFUND_ACCOUNTING_DB_PASSWORD",
)
ROTATABLE_MCP_KEYS = (
    "MCP_RESEARCH_API_KEY",
    "MCP_TRADING_ORDER_API_KEY",
)
AWS_REQUIRED_KEYS = (
    "SUPABASE_URL",
    "HEDGEFUND_TSDB_PASSWORD",
)

LOCAL_USER_ID = "00000000-0000-4000-8000-00000000cec0"
LOCAL_FUND_ID = "5c26db42-ce83-4daf-b1dc-c81680c13a6c"
LOCAL_BOOK_ID = "07d913de-9a5b-4cf5-b893-31a625445761"
LOCAL_TRADING_GRANT = json.dumps(
    [
        {
            "user_id": LOCAL_USER_ID,
            "fund_id": LOCAL_FUND_ID,
            "book_id": LOCAL_BOOK_ID,
            "name": "MAIN",
            "role": "TRADER",
            "fund_status": "ACTIVE",
            "book_status": "ACTIVE",
        }
    ],
    ensure_ascii=True,
    separators=(",", ":"),
)

_ASSIGNMENT_RE = re.compile(
    r"^\s*(?:export\s+)?(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*=(?P<value>.*)$"
)
_URL_SAFE_SECRET_RE = re.compile(r"^[A-Za-z0-9._~-]{32,}$")
_UNSAFE_VALUE_MARKERS = (
    "change_me",
    "changeme",
    "placeholder",
    "replace-with",
    "replace_me",
    "example-secret",
)


class ConfigurationError(RuntimeError):
    """A safe configuration error whose message never contains a value."""


@dataclass(frozen=True)
class EnvAssignment:
    key: str
    raw_value: str

    @property
    def value(self) -> str:
        return _logical_value(self.raw_value)


@dataclass(frozen=True)
class ConfigureResult:
    path: Path
    runtime: str
    changed: bool
    preserved_secret_keys: tuple[str, ...]
    generated_secret_keys: tuple[str, ...]
    deduplicated_keys: tuple[str, ...]


def _logical_value(raw_value: str) -> str:
    """Return a dotenv value for validation without evaluating substitutions."""

    value = raw_value.strip()
    if len(value) >= 2 and value[0] in {"'", '"'} and value[-1] == value[0]:
        return value[1:-1].strip()
    # In unquoted dotenv syntax, a whitespace-prefixed ``#`` begins a comment.
    for index, character in enumerate(value):
        if character == "#" and index > 0 and value[index - 1].isspace():
            return value[:index].rstrip()
    return value


def _parse_assignments(text: str) -> dict[str, EnvAssignment]:
    """Parse effective assignments using dotenv's last-assignment-wins rule."""

    assignments: dict[str, EnvAssignment] = {}
    for line in text.splitlines():
        match = _ASSIGNMENT_RE.match(line)
        if match is None:
            continue
        key = match.group("key")
        assignments[key] = EnvAssignment(
            key=key, raw_value=match.group("value").strip()
        )
    return assignments


def _duplicate_assignment_keys(text: str) -> tuple[str, ...]:
    """Return duplicate dotenv key names without exposing their values."""

    counts: dict[str, int] = {}
    for line in text.splitlines():
        match = _ASSIGNMENT_RE.match(line)
        if match is not None:
            key = match.group("key")
            counts[key] = counts.get(key, 0) + 1
    return tuple(sorted(key for key, count in counts.items() if count > 1))


def _safe_nonempty(value: str) -> bool:
    normalized = value.strip().casefold()
    return bool(normalized) and not any(
        marker in normalized for marker in _UNSAFE_VALUE_MARKERS
    )


def _valid_secret(value: str, *, url_safe: bool = False) -> bool:
    return (
        len(value) >= 32
        and _safe_nonempty(value)
        and not value.startswith("${")
        and (not url_safe or _URL_SAFE_SECRET_RE.fullmatch(value) is not None)
    )


def _new_secret() -> str:
    # 48 random bytes produce a 64-character URL-safe value without dotenv
    # quoting requirements.  Every managed credential is generated by an
    # independent call.
    return secrets.token_urlsafe(48)


def _secret_settings(
    assignments: Mapping[str, EnvAssignment],
    *,
    keys: Sequence[str],
    generator: Callable[[], str],
    rotate_keys: frozenset[str] = frozenset(),
) -> tuple[dict[str, str], tuple[str, ...], tuple[str, ...]]:
    """Preserve safe distinct secrets and generate every remaining credential."""

    preserved_raw: dict[str, str] = {}
    preserved_values: set[str] = set()
    for key in keys:
        if key in rotate_keys:
            continue
        assignment = assignments.get(key)
        if assignment is None or not _valid_secret(
            assignment.value, url_safe=key in AWS_DATABASE_SECRET_KEYS
        ):
            continue
        if assignment.value in preserved_values:
            # Keep the first key and rotate the later duplicate.  Which key was
            # rotated is safe to report; the credential itself never is.
            continue
        preserved_raw[key] = assignment.raw_value
        preserved_values.add(assignment.value)

    settings = dict(preserved_raw)
    used_values = set(preserved_values)
    generated: list[str] = []
    for key in keys:
        if key in settings:
            continue
        for _attempt in range(128):
            candidate = generator()
            if (
                _valid_secret(
                    candidate, url_safe=key in AWS_DATABASE_SECRET_KEYS
                )
                and candidate not in used_values
            ):
                settings[key] = candidate
                used_values.add(candidate)
                generated.append(key)
                break
        else:
            raise ConfigurationError("could not generate distinct PAPER-order secrets")

    return settings, tuple(preserved_raw), tuple(generated)


def _secret_keys_for_runtime(runtime: str) -> tuple[str, ...]:
    if runtime == "aws":
        return SERVICE_SECRET_KEYS + AWS_DATABASE_SECRET_KEYS
    return SERVICE_SECRET_KEYS


def _runtime_settings(runtime: str) -> dict[str, str]:
    common = {
        "USER_PAPER_ORDER_WORKFLOW_ENABLED": "true",
        "LS_ENV": "PAPER",
        "TRADING_EXECUTION_MODE": "PAPER",
        "TRADING_BROKER_ADAPTER": "paper",
    }
    if runtime == "local":
        return {
            "APP_ENV": "local",
            "PORTFOLIO_AUTH_MODE": "fixture",
            # Fixture mode is not anonymous mode.  The exact X-User-Id must be
            # present and must match the explicit grant below.
            "PORTFOLIO_AUTH_REQUIRED": "true",
            "PORTFOLIO_FIXTURE_TRADING_BOOKS_JSON": LOCAL_TRADING_GRANT,
            **common,
        }
    if runtime == "aws":
        return {
            "APP_ENV": "production",
            "PORTFOLIO_AUTH_MODE": "supabase_jwt",
            "PORTFOLIO_AUTH_REQUIRED": "true",
            "PORTFOLIO_FIXTURE_TRADING_BOOKS_JSON": "[]",
            # Market data is read-only LIVE data; order execution remains
            # independently pinned to PAPER by LS_ENV/TRADING_* below.
            "LS_MARKET_ENV": "LIVE",
            # The AWS Compose overlay builds the private control DSN from the
            # local Timescale/PostgreSQL service and this database name.  A
            # hosted Supabase DATABASE_URL must never be carried into the
            # operational data plane.
            "HEDGEFUND_CONTROL_DB_NAME": "control",
            **common,
        }
    raise ConfigurationError("runtime must be local or aws")


def _aws_required_settings(
    assignments: Mapping[str, EnvAssignment],
) -> dict[str, str]:
    missing = [
        key
        for key in AWS_REQUIRED_KEYS
        if key not in assignments or not _safe_nonempty(assignments[key].value)
    ]
    if missing:
        # Names are not credentials and make the failure actionable.  Never add
        # the rejected raw values to this message.
        raise ConfigurationError(
            "AWS runtime requires valid values for: " + ", ".join(missing)
        )
    return {key: assignments[key].raw_value for key in AWS_REQUIRED_KEYS}


def _newline_for(text: str) -> str:
    first_lf = text.find("\n")
    if first_lf >= 1 and text[first_lf - 1] == "\r":
        return "\r\n"
    return "\n"


def _render_env(text: str, settings: Mapping[str, str]) -> str:
    """Render one effective assignment per key without changing its value."""

    newline = _newline_for(text)
    assignments = _parse_assignments(text)
    duplicate_keys = frozenset(_duplicate_assignment_keys(text))
    rendered: list[str] = []
    seen: set[str] = set()
    for line in text.splitlines():
        match = _ASSIGNMENT_RE.match(line)
        key = match.group("key") if match is not None else None
        if key is None:
            rendered.append(line)
            continue
        if key in seen:
            continue
        if key in settings:
            rendered.append(f"{key}={settings[key]}")
        elif key in duplicate_keys:
            # dotenv uses last-assignment-wins. Keep that effective raw value at
            # the first documented position and remove every shadow assignment.
            rendered.append(f"{key}={assignments[key].raw_value}")
        else:
            rendered.append(line)
        seen.add(key)

    missing = [key for key in settings if key not in seen]
    if missing:
        if rendered and rendered[-1].strip():
            rendered.append("")
        rendered.append("# Managed by scripts/configure_paper_order_env.py")
        rendered.extend(f"{key}={settings[key]}" for key in missing)

    return newline.join(rendered) + newline


def _atomic_write(path: Path, content: str) -> None:
    """Write and fsync a sibling temporary file, then atomically replace path."""

    parent = path.parent
    if not parent.exists() or not parent.is_dir():
        raise ConfigurationError("environment file parent directory does not exist")

    previous_mode: int | None = None
    if path.exists():
        previous_mode = stat.S_IMODE(path.stat().st_mode)

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if previous_mode is not None:
            os.chmod(temporary, previous_mode)
        os.replace(temporary, path)
    finally:
        # On replacement failure the original remains intact; remove only our
        # sibling temporary file.  Never unlink the destination here.
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def configure_environment(
    *,
    runtime: str,
    env_file: Path,
    generator: Callable[[], str] | None = None,
    rotate_keys: Sequence[str] = (),
) -> ConfigureResult:
    """Configure ``env_file`` and return metadata that contains no secret values."""

    path = env_file.expanduser().resolve(strict=False)
    try:
        original = path.read_text(encoding="utf-8") if path.exists() else ""
    except (OSError, UnicodeError) as exc:
        raise ConfigurationError("could not read the environment file") from exc

    assignments = _parse_assignments(original)
    duplicate_keys = _duplicate_assignment_keys(original)
    requested_rotation = frozenset(str(key).strip() for key in rotate_keys)
    invalid_rotation = requested_rotation.difference(ROTATABLE_MCP_KEYS)
    if invalid_rotation:
        raise ConfigurationError(
            "unsupported credential rotation key(s): "
            + ", ".join(sorted(invalid_rotation))
        )
    settings = _runtime_settings(runtime)
    if runtime == "aws":
        # Validate before generating credentials or touching the file.  A
        # partially configured production file must never be written.
        settings.update(_aws_required_settings(assignments))

    managed_secret_keys = list(_secret_keys_for_runtime(runtime))
    for key in ROTATABLE_MCP_KEYS:
        if key in requested_rotation and key not in managed_secret_keys:
            managed_secret_keys.append(key)
    secret_settings, preserved, generated = _secret_settings(
        assignments,
        keys=managed_secret_keys,
        generator=generator or _new_secret,
        rotate_keys=requested_rotation,
    )
    settings.update(secret_settings)
    rendered = _render_env(original, settings)
    changed = rendered != original
    if changed:
        try:
            _atomic_write(path, rendered)
        except ConfigurationError:
            raise
        except OSError as exc:
            raise ConfigurationError(
                "could not atomically replace the environment file"
            ) from exc

    return ConfigureResult(
        path=path,
        runtime=runtime,
        changed=changed,
        preserved_secret_keys=preserved,
        generated_secret_keys=generated,
        deduplicated_keys=duplicate_keys,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", choices=("local", "aws"), required=True)
    parser.add_argument(
        "--env-file",
        type=Path,
        default=REPO_ROOT / ".env",
        help="dotenv file to update atomically (default: repository .env)",
    )
    parser.add_argument(
        "--rotate-key",
        action="append",
        default=[],
        choices=ROTATABLE_MCP_KEYS,
        help=(
            "replace this MCP credential even when it is currently valid; "
            "repeat for both credentials"
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = configure_environment(
            runtime=args.runtime,
            env_file=args.env_file,
            rotate_keys=args.rotate_key,
        )
    except ConfigurationError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    action = "updated" if result.changed else "already configured"
    print(
        f"PAPER-order environment {action} for {result.runtime}; "
        f"preserved {len(result.preserved_secret_keys)} secret(s), "
        f"generated {len(result.generated_secret_keys)} secret(s), "
        f"deduplicated {len(result.deduplicated_keys)} key(s)."
    )
    print("Secret values were not printed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
