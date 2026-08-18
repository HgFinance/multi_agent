#!/usr/bin/env bash
# Deploy a detached Git release without modifying /home/ubuntu/hgfinance.
# Invoke with bash; the repository file does not need an executable bit.

set -Eeuo pipefail
umask 077
ORIGINAL_ARGS=("$@")

PROJECT_NAME="hedgefund"
LEGACY_ROOT="/home/ubuntu/hgfinance"
RELEASES_ROOT="/home/ubuntu/hgfinance-releases"
SOURCE_ENV=""
REPOSITORY_URL=""
RELEASE_REF="main"
ALLOW_FIRST_DEPLOY=0
ADOPT_EXISTING_MARKET=0
TOP_UP_PAPER_CASH=0
SEED_PAPER_PRINCIPAL=1
REFRESH_RUNTIME_ENV=0
BACKUP_BEFORE_MIGRATION=1
SWITCH_STARTED=0
PREVIOUS_RELEASE=""
RUNTIME_ENV=""
PROFILE_RUNTIME_ROOT="/home/ubuntu/.hermes"
PROFILE_BACKUP_DIR=""
PROFILE_INSTALL_ACTIVE=0
ROLLBACK_IMAGES_CAPTURED=0
declare -A ROLLBACK_IMAGE_REF_BY_CONTAINER=()
declare -A ROLLBACK_IMAGE_TAG_BY_CONTAINER=()

MANAGED_RELEASE_SERVICE_SPECS=(
  "hedgefund-ls-realtime:ls-realtime"
  "hedgefund-trading-api:trading-api"
  "hedgefund-paper-order-orchestrator-mcp:paper-order-orchestrator-mcp"
  "hedgefund-portfolio-bff:portfolio-bff"
  "hedgefund-ceo-hermes:ceo-hermes"
  "hedgefund-trading-hermes:trading-hermes"
)

say() { printf '%s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

usage() {
  cat <<'USAGE'
Usage: bash scripts/aws_deploy_paper_order_release.sh [options]

  --ref REF                    Remote branch/tag to deploy (default: main)
  --legacy-root PATH           Existing checkout used read-only
  --releases-root PATH         Dedicated bare repo/worktree/state root
  --env-file PATH              Existing .env imported into private runtime state
  --repo-url URL               Git remote (never printed); defaults to legacy origin
  --allow-first-deploy         Acknowledge that the first release has no rollback target
  --adopt-existing-market      Adopt only a fully-audited untracked market schema
  --top-up-paper-cash          Explicitly restore configured PAPER cash floor
  --no-seed-paper-principal    Skip cec0/fund/book/cash provisioning
  --refresh-runtime-env        Replace release runtime.env from --env-file
  --skip-database-backup       Explicitly skip the pre-migration private DB dump
  -h, --help                   Show this help

The legacy checkout is never pulled, reset, cleaned, built from, or written to.
USAGE
}

while (($#)); do
  case "$1" in
    --ref) [[ $# -ge 2 ]] || die "--ref requires a value"; RELEASE_REF="$2"; shift 2 ;;
    --legacy-root) [[ $# -ge 2 ]] || die "--legacy-root requires a value"; LEGACY_ROOT="$2"; shift 2 ;;
    --releases-root) [[ $# -ge 2 ]] || die "--releases-root requires a value"; RELEASES_ROOT="$2"; shift 2 ;;
    --env-file) [[ $# -ge 2 ]] || die "--env-file requires a value"; SOURCE_ENV="$2"; shift 2 ;;
    --repo-url) [[ $# -ge 2 ]] || die "--repo-url requires a value"; REPOSITORY_URL="$2"; shift 2 ;;
    --allow-first-deploy) ALLOW_FIRST_DEPLOY=1; shift ;;
    --adopt-existing-market) ADOPT_EXISTING_MARKET=1; shift ;;
    --top-up-paper-cash) TOP_UP_PAPER_CASH=1; shift ;;
    --no-seed-paper-principal) SEED_PAPER_PRINCIPAL=0; shift ;;
    --refresh-runtime-env) REFRESH_RUNTIME_ENV=1; shift ;;
    --skip-database-backup) BACKUP_BEFORE_MIGRATION=0; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown option: $1" ;;
  esac
done

if ((TOP_UP_PAPER_CASH == 1 && SEED_PAPER_PRINCIPAL == 0)); then
  die "--top-up-paper-cash cannot be combined with --no-seed-paper-principal"
fi

for command_name in git docker flock realpath python3 timeout; do
  command -v "$command_name" >/dev/null 2>&1 || die "required command is missing: $command_name"
done
docker compose version >/dev/null 2>&1 || die "Docker Compose v2 is required"
compose_version="$(docker compose version --short 2>/dev/null)"
python3 - "$compose_version" <<'PY' || die "Docker Compose 2.24.4+ is required for private port overrides"
import re
import sys
match = re.search(r"(\d+)\.(\d+)\.(\d+)", sys.argv[1])
if match is None or tuple(map(int, match.groups())) < (2, 24, 4):
    raise SystemExit(1)
PY

LEGACY_ROOT="$(realpath -e -- "$LEGACY_ROOT")" || die "legacy root does not exist"
[[ -d "$LEGACY_ROOT/.git" || -f "$LEGACY_ROOT/.git" ]] || die "legacy root is not a Git checkout"
SOURCE_ENV="${SOURCE_ENV:-$LEGACY_ROOT/.env}"
SOURCE_ENV="$(realpath -e -- "$SOURCE_ENV")" || die "environment file does not exist"
[[ -f "$SOURCE_ENV" && -r "$SOURCE_ENV" ]] || die "environment file is not readable"

install -d -m 700 -- "$RELEASES_ROOT" "$RELEASES_ROOT/worktrees" "$RELEASES_ROOT/state"
RELEASES_ROOT="$(realpath -e -- "$RELEASES_ROOT")"
exec 9>"$RELEASES_ROOT/deploy.lock"
flock -n 9 || die "another hedgefund deployment is already running"

if [[ -z "$REPOSITORY_URL" ]]; then
  REPOSITORY_URL="$(git -C "$LEGACY_ROOT" remote get-url origin 2>/dev/null)" \
    || die "cannot read the legacy origin URL"
fi
[[ -n "$REPOSITORY_URL" ]] || die "repository URL is empty"

BARE_REPOSITORY="$RELEASES_ROOT/repository.git"
if [[ ! -e "$BARE_REPOSITORY" ]]; then
  say "Preparing the dedicated release repository..."
  if ! git clone --bare --quiet "$REPOSITORY_URL" "$BARE_REPOSITORY" >/dev/null 2>&1; then
    die "dedicated release repository clone failed"
  fi
else
  [[ "$(git --git-dir="$BARE_REPOSITORY" rev-parse --is-bare-repository 2>/dev/null)" == "true" ]] \
    || die "release repository path is not a bare Git repository"
fi

say "Fetching the requested release ref without touching the legacy checkout..."
if ! git --git-dir="$BARE_REPOSITORY" fetch --quiet --prune origin "$RELEASE_REF" >/dev/null 2>&1; then
  die "release ref fetch failed"
fi
RELEASE_COMMIT="$(git --git-dir="$BARE_REPOSITORY" rev-parse --verify 'FETCH_HEAD^{commit}' 2>/dev/null)" \
  || die "release ref did not resolve to a commit"
[[ "$RELEASE_COMMIT" =~ ^[0-9a-f]{40,64}$ ]] || die "resolved release commit is invalid"
RELEASE="$RELEASES_ROOT/worktrees/$RELEASE_COMMIT"

if [[ -e "$RELEASE" ]]; then
  [[ -d "$RELEASE" ]] || die "release path exists but is not a directory"
  [[ "$(git -C "$RELEASE" rev-parse --verify HEAD 2>/dev/null)" == "$RELEASE_COMMIT" ]] \
    || die "existing release worktree points at another commit"
else
  if ! git --git-dir="$BARE_REPOSITORY" worktree add --quiet --detach "$RELEASE" "$RELEASE_COMMIT"; then
    die "release worktree creation failed"
  fi
fi
[[ -f "$RELEASE/docker-compose.yml" ]] || die "release has no root compose file"
[[ -f "$RELEASE/deploy/aws/docker-compose.paper-order.yml" ]] \
  || die "release has no AWS PAPER overlay"

# A deploy invoked from `current` must use the target release's deployment
# logic, not the previous release's copy. Hand off exactly once before any
# runtime state, profiles, databases or containers can change. The short
# unlock window is fail-closed: a concurrent deploy wins the lock and this
# target process exits without mutating runtime state.
TARGET_DEPLOY_SCRIPT="$RELEASE/scripts/aws_deploy_paper_order_release.sh"
[[ -f "$TARGET_DEPLOY_SCRIPT" ]] || die "release has no PAPER deploy script"
CURRENT_DEPLOY_SCRIPT="$(realpath -e -- "$0" 2>/dev/null || true)"
TARGET_DEPLOY_SCRIPT="$(realpath -e -- "$TARGET_DEPLOY_SCRIPT")" \
  || die "target deploy script is not readable"
if [[ "$CURRENT_DEPLOY_SCRIPT" != "$TARGET_DEPLOY_SCRIPT" ]]; then
  [[ "${HGFINANCE_DEPLOY_HANDOFF_COMMIT:-}" != "$RELEASE_COMMIT" ]] \
    || die "deployment script handoff loop detected"
  export HGFINANCE_DEPLOY_HANDOFF_COMMIT="$RELEASE_COMMIT"
  flock -u 9
  exec 9>&-
  exec bash "$TARGET_DEPLOY_SCRIPT" "${ORIGINAL_ARGS[@]}"
fi

CURRENT_LINK="$RELEASES_ROOT/current"
if [[ -L "$CURRENT_LINK" ]]; then
  PREVIOUS_RELEASE="$(realpath -e -- "$CURRENT_LINK")" || die "current release link is broken"
  case "$PREVIOUS_RELEASE/" in
    "$RELEASES_ROOT/worktrees/"*) ;;
    *) die "current release link escapes the releases root" ;;
  esac
  [[ -f "$PREVIOUS_RELEASE/docker-compose.yml" \
     && -f "$PREVIOUS_RELEASE/deploy/aws/docker-compose.paper-order.yml" ]] \
    || die "current rollback release is incomplete"
elif [[ -e "$CURRENT_LINK" ]]; then
  die "current release marker exists but is not a symbolic link"
elif ((ALLOW_FIRST_DEPLOY == 0)); then
  die "no rollback release exists; rerun with --allow-first-deploy after reviewing the first-deploy risk"
fi

RUNTIME_ENV="$RELEASES_ROOT/state/runtime.env"
if [[ ! -f "$RUNTIME_ENV" || "$REFRESH_RUNTIME_ENV" == "1" ]]; then
  runtime_temp="$(mktemp "$RELEASES_ROOT/state/runtime.env.tmp.XXXXXX")"
  install -m 600 -- "$SOURCE_ENV" "$runtime_temp"
  mv -f -- "$runtime_temp" "$RUNTIME_ENV"
fi
chmod 600 "$RUNTIME_ENV"

# This utility writes only the private state copy.  It preserves existing
# values, creates eight mutually distinct service/database secrets when
# absent and never prints a value.  The legacy repository's .env remains
# untouched.
if [[ -f "$RELEASE/scripts/configure_paper_order_env.py" ]]; then
  python3 "$RELEASE/scripts/configure_paper_order_env.py" \
    --runtime aws --env-file "$RUNTIME_ENV" >/dev/null \
    || die "PAPER runtime environment configuration failed"
fi

# Independent read-only validation.  Error output contains key names only.
if ! python3 - "$RUNTIME_ENV" <<'PY'
import re
import base64
import json
import sys
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import UUID

values = {}
for raw in Path(sys.argv[1]).read_text(encoding="utf-8-sig").splitlines():
    line = raw.strip()
    if not line or line.startswith("#"):
        continue
    if line.startswith("export "):
        line = line[7:].lstrip()
    if "=" not in line:
        continue
    key, value = line.split("=", 1)
    key = key.strip()
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    values[key] = value

required = (
    "HEDGEFUND_TSDB_PASSWORD",
    "SUPABASE_URL",
    "MCP_TRADING_ORDER_API_KEY",
    "TRADING_SERVICE_AUTH_SECRET",
    "TRADING_INTERNAL_SERVICE_AUTH_SECRET",
    "CEO_DISCORD_INGRESS_API_KEY",
    "HEDGEFUND_RUNTIME_DB_PASSWORD",
    "HEDGEFUND_ORDER_DB_PASSWORD",
    "HEDGEFUND_TRADING_DB_PASSWORD",
    "HEDGEFUND_ACCOUNTING_DB_PASSWORD",
    "DISCORD_ACTOR_MAP",
    "LS_APP_KEY",
    "LS_APP_SECRET_KEY",
    "LS_REST_BASE_URL",
)
missing = [key for key in required if not values.get(key)]
if missing:
    print("missing deployment keys: " + ", ".join(missing), file=sys.stderr)
    raise SystemExit(1)

def _actor_map_contains_seed_binding(raw, seed_user_id, seed_fund_id):
    """Match the runtime actor parser without exposing any actor identifiers."""

    seen_discord_ids = set()
    uuid_pattern = (
        r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
        r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
    )
    for entry in re.split(r"[,\s]+", raw.strip()):
        if not entry:
            continue
        parts = [part.strip() for part in entry.split(":")]
        if len(parts) == 2:
            parts.append("")
        if len(parts) != 3:
            continue
        discord_user_id, user_id, fund_id = parts
        if not re.fullmatch(r"\d{15,25}", discord_user_id):
            continue
        if not re.fullmatch(uuid_pattern, user_id):
            continue
        if fund_id and not re.fullmatch(uuid_pattern, fund_id):
            continue
        if discord_user_id in seen_discord_ids:
            # apps.api.discord_actor_map keeps the first valid binding.
            continue
        seen_discord_ids.add(discord_user_id)
        if (
            UUID(user_id) == seed_user_id
            and fund_id
            and UUID(fund_id) == seed_fund_id
        ):
            return True
    return False

try:
    seed_user_id = UUID(
        values.get(
            "PAPER_SEED_USER_ID", "00000000-0000-4000-8000-00000000cec0"
        ).strip()
    )
    seed_fund_id = UUID(
        values.get(
            "PAPER_SEED_FUND_ID", "5c26db42-ce83-4daf-b1dc-c81680c13a6c"
        ).strip()
    )
except (AttributeError, ValueError):
    print("PAPER seed identifiers must be valid UUIDs", file=sys.stderr)
    raise SystemExit(1)
if not _actor_map_contains_seed_binding(
    values["DISCORD_ACTOR_MAP"], seed_user_id, seed_fund_id
):
    print(
        "DISCORD_ACTOR_MAP must include the configured PAPER seed principal",
        file=sys.stderr,
    )
    raise SystemExit(1)
managed_secret_keys = (
    "MCP_TRADING_ORDER_API_KEY",
    "TRADING_SERVICE_AUTH_SECRET",
    "TRADING_INTERNAL_SERVICE_AUTH_SECRET",
    "CEO_DISCORD_INGRESS_API_KEY",
    "HEDGEFUND_RUNTIME_DB_PASSWORD",
    "HEDGEFUND_ORDER_DB_PASSWORD",
    "HEDGEFUND_TRADING_DB_PASSWORD",
    "HEDGEFUND_ACCOUNTING_DB_PASSWORD",
)
managed_secrets = [values[key] for key in managed_secret_keys]
if any(len(secret) < 32 for secret in managed_secrets) or len(
    set(managed_secrets)
) != len(managed_secret_keys):
    print("PAPER managed secrets must be distinct and at least 32 characters", file=sys.stderr)
    raise SystemExit(1)
database_secret_keys = (
    "HEDGEFUND_RUNTIME_DB_PASSWORD",
    "HEDGEFUND_ORDER_DB_PASSWORD",
    "HEDGEFUND_TRADING_DB_PASSWORD",
    "HEDGEFUND_ACCOUNTING_DB_PASSWORD",
)
if any(
    not re.fullmatch(r"[A-Za-z0-9._~-]+", values[key])
    for key in database_secret_keys
):
    print("PAPER database secrets must be URL-safe", file=sys.stderr)
    raise SystemExit(1)
if len(values["HEDGEFUND_TSDB_PASSWORD"]) < 16:
    print("HEDGEFUND_TSDB_PASSWORD must be at least 16 characters", file=sys.stderr)
    raise SystemExit(1)
if not re.fullmatch(r"[A-Za-z0-9._~-]+", values["HEDGEFUND_TSDB_PASSWORD"]):
    print("HEDGEFUND_TSDB_PASSWORD must be URL-safe", file=sys.stderr)
    raise SystemExit(1)
supabase = urlsplit(values["SUPABASE_URL"])
if supabase.scheme != "https" or not supabase.hostname:
    print("SUPABASE_URL must be an HTTPS URL", file=sys.stderr)
    raise SystemExit(1)

# The current Supabase project signs user access tokens asymmetrically.  A
# publishable/legacy anon key is needed only for the legacy HS256 `/user`
# verification path, so it is optional.  When neither public browser key is
# available, prove before deployment that the actual Auth JWKS endpoint has a
# usable public ES256 or RS256 signing key.  Never accept an octet/shared key.
publishable_key = values.get("SUPABASE_PUBLISHABLE_KEY", "").strip()
anon_key = values.get("SUPABASE_ANON_KEY", "").strip()
if any(key.startswith("sb_secret_") for key in (publishable_key, anon_key) if key):
    print("Supabase browser key settings must not contain a secret key", file=sys.stderr)
    raise SystemExit(1)

def _base64url_bytes(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        return None
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, TypeError):
        return None

def _valid_asymmetric_signing_jwk(key):
    if not isinstance(key, dict) or not isinstance(key.get("kid"), str):
        return False
    if not key["kid"].strip() or key.get("use", "sig") != "sig":
        return False
    key_ops = key.get("key_ops")
    if key_ops is not None and (
        not isinstance(key_ops, list) or "verify" not in key_ops or "sign" in key_ops
    ):
        return False
    if any(field in key for field in ("d", "p", "q", "dp", "dq", "qi", "oth")):
        return False
    if key.get("kty") == "EC" and key.get("alg") == "ES256":
        return (
            key.get("crv") == "P-256"
            and len(_base64url_bytes(key.get("x")) or b"") == 32
            and len(_base64url_bytes(key.get("y")) or b"") == 32
        )
    if key.get("kty") == "RSA" and key.get("alg") == "RS256":
        modulus = _base64url_bytes(key.get("n")) or b""
        exponent_bytes = _base64url_bytes(key.get("e")) or b""
        exponent = int.from_bytes(exponent_bytes, "big") if exponent_bytes else 0
        return len(modulus) >= 256 and 3 <= exponent <= 0xFFFFFFFF and exponent % 2 == 1
    return False

def _validate_jwks_document(document):
    if not isinstance(document, dict):
        return False
    keys = document.get("keys")
    return (
        isinstance(keys, list)
        and 1 <= len(keys) <= 64
        and any(_valid_asymmetric_signing_jwk(key) for key in keys)
    )

if not publishable_key and not anon_key:
    configured_jwks = values.get("SUPABASE_AUTH_JWKS_URL", "").strip()
    configured_issuer = values.get("SUPABASE_AUTH_ISSUER", "").strip()
    default_issuer = configured_issuer or (
        values["SUPABASE_URL"].rstrip("/") + "/auth/v1"
    )
    jwks_url = configured_jwks or (
        default_issuer.rstrip("/") + "/.well-known/jwks.json"
    )
    parsed_jwks = urlsplit(jwks_url)
    if (
        parsed_jwks.scheme != "https"
        or not parsed_jwks.hostname
        or parsed_jwks.username is not None
        or parsed_jwks.password is not None
        or parsed_jwks.query
        or parsed_jwks.fragment
    ):
        print("Supabase JWKS URL must be a credential-free HTTPS URL", file=sys.stderr)
        raise SystemExit(1)

    class _NoRedirects(HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    try:
        request = Request(
            jwks_url,
            headers={"Accept": "application/json", "User-Agent": "hedgefund-deployer/1"},
            method="GET",
        )
        with build_opener(_NoRedirects()).open(request, timeout=5) as response:
            if response.status != 200:
                raise ValueError("unexpected JWKS status")
            raw_jwks = response.read(262145)
        if len(raw_jwks) > 262144:
            raise ValueError("JWKS response is too large")
        jwks_document = json.loads(raw_jwks.decode("utf-8"))
        if not _validate_jwks_document(jwks_document):
            raise ValueError("JWKS has no supported asymmetric signing key")
    except Exception:  # noqa: BLE001 - never disclose upstream URL/body/details
        print(
            "Supabase JWKS preflight requires a public ES256 or RS256 signing key",
            file=sys.stderr,
        )
        raise SystemExit(1)
def _valid_cors_allowlist(value):
    if not isinstance(value, str):
        return False
    if not value.strip():
        # Backend-only: no Access-Control-Allow-Origin is emitted and browser
        # preflights are rejected. Authentication remains independently
        # mandatory for every non-health BFF route.
        return True
    origins = value.split(",")
    if any(not part.strip() for part in origins):
        return False
    for part in origins:
        origin = part.strip()
        try:
            parsed = urlsplit(origin)
            parsed.port
        except ValueError:
            return False
        if (
            "*" in origin
            or "\\" in origin
            or any(ord(character) < 33 or ord(character) > 126 for character in origin)
            or parsed.scheme.casefold() != "https"
            or not parsed.netloc
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or "%" in parsed.netloc
            or parsed.netloc.endswith(":")
        ):
            return False
    return True

if not _valid_cors_allowlist(values.get("PORTFOLIO_CORS_ALLOW_ORIGINS", "")):
    print(
        "PORTFOLIO_CORS_ALLOW_ORIGINS must be empty or contain exact HTTPS origins",
        file=sys.stderr,
    )
    raise SystemExit(1)
control_name = values.get("HEDGEFUND_CONTROL_DB_NAME", "control")
if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", control_name):
    print("HEDGEFUND_CONTROL_DB_NAME is invalid", file=sys.stderr)
    raise SystemExit(1)
reference_mode = values.get("LS_REFERENCE_ENV", "LIVE").upper()
if reference_mode not in {"LIVE", "PAPER"}:
    print("LS_REFERENCE_ENV must be LIVE or PAPER", file=sys.stderr)
    raise SystemExit(1)
if reference_mode == "PAPER" and not all(
    values.get(key) for key in ("LS_APP_KEY_PAPER", "LS_APP_SECRET_KEY_PAPER")
):
    print("LS_REFERENCE_ENV=PAPER requires PAPER LS credentials", file=sys.stderr)
    raise SystemExit(1)
ls_rest = urlsplit(values["LS_REST_BASE_URL"])
if ls_rest.scheme != "https" or not ls_rest.hostname:
    print("LS_REST_BASE_URL must be an HTTPS URL", file=sys.stderr)
    raise SystemExit(1)
PY
then
  die "release runtime environment validation failed"
fi

compose_release() {
  local release_path="$1"
  shift
  docker compose \
    --project-name "$PROJECT_NAME" \
    --env-file "$RUNTIME_ENV" \
    -f "$release_path/docker-compose.yml" \
    -f "$release_path/deploy/aws/docker-compose.paper-order.yml" \
    "$@"
}

external_pull_service_plan() {
  # Emit only external images used by this bounded deployment. The caller
  # independently checks Docker's local image store before pulling, because
  # Compose treats mutable tags as refreshable even under `--policy missing`.
  python3 -c '
import json
import sys

services = json.load(sys.stdin).get("services", {})
for name in ("redis", "timescaledb", "trading-hermes"):
    service = services.get(name) or {}
    image = service.get("image")
    if service.get("build") or not image:
        raise SystemExit(f"invalid external deployment service: {name}")
    print(f"{name}\t{image}")
'
}

container_state() {
  docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$1" 2>/dev/null || true
}

wait_container() {
  local container_name="$1" timeout_seconds="$2" elapsed=0 state
  while ((elapsed < timeout_seconds)); do
    state="$(container_state "$container_name")"
    case "$state" in
      healthy|running) return 0 ;;
      unhealthy|exited|dead) return 1 ;;
    esac
    sleep 5
    elapsed=$((elapsed + 5))
  done
  return 1
}

wait_http_ready() {
  local container_name="$1" port="$2" path="$3" timeout_seconds="$4" elapsed=0
  while ((elapsed < timeout_seconds)); do
    if docker exec "$container_name" python -c \
      "import urllib.request; r=urllib.request.urlopen('http://127.0.0.1:${port}${path}', timeout=4); raise SystemExit(0 if r.status == 200 else 1)" \
      >/dev/null 2>&1; then
      return 0
    fi
    state="$(container_state "$container_name")"
    case "$state" in exited|dead|unhealthy) return 1 ;; esac
    sleep 5
    elapsed=$((elapsed + 5))
  done
  return 1
}

smoke_trading_paper_order_mcp() {
  # Exercise the profile that Trading Hermes actually loaded, including its
  # rendered Bearer header and MCP tools/list exchange.  Suppress all Hermes
  # output because even masked credentials do not belong in deployment logs.
  timeout 60 docker exec -u hermes hedgefund-trading-hermes sh -eu -c '
    umask 077
    result="$(mktemp)"
    trap '\''rm -f -- "$result"'\'' EXIT HUP INT TERM
    hermes mcp test user-paper-order >"$result" 2>&1
    grep -Eq "Tools discovered:[[:space:]]*1([[:space:]]|$)" "$result"
    grep -Fq "process_user_paper_order" "$result"
  ' >/dev/null 2>&1
}

wait_hermes_gateway() {
  local container_name="$1" timeout_seconds="$2" elapsed=0 state
  while ((elapsed < timeout_seconds)); do
    if docker exec "$container_name" pgrep -f 'hermes gateway run --replace' \
      >/dev/null 2>&1; then
      return 0
    fi
    state="$(container_state "$container_name")"
    case "$state" in exited|dead|unhealthy) return 1 ;; esac
    sleep 5
    elapsed=$((elapsed + 5))
  done
  return 1
}

assert_release_owned_container() {
  local release_path="$1" container_name="$2" service_name="$3"
  local expected_files metadata actual_project actual_working_dir actual_files
  local actual_service actual_hash expected_hash
  expected_files="$release_path/docker-compose.yml,$release_path/deploy/aws/docker-compose.paper-order.yml"
  metadata="$(docker inspect --format '{{index .Config.Labels "com.docker.compose.project"}}|{{index .Config.Labels "com.docker.compose.project.working_dir"}}|{{index .Config.Labels "com.docker.compose.project.config_files"}}|{{index .Config.Labels "com.docker.compose.service"}}|{{index .Config.Labels "com.docker.compose.config-hash"}}' "$container_name")" \
    || return 1
  IFS='|' read -r actual_project actual_working_dir actual_files actual_service actual_hash <<<"$metadata"
  expected_hash="$(
    compose_release "$release_path" config --hash "$service_name" \
      | awk -v service="$service_name" '$1 == service { print $2 }'
  )"
  [[ -n "$expected_hash" ]] || return 1
  [[ "$actual_project" == "$PROJECT_NAME" ]] || return 1
  [[ "$actual_working_dir" == "$release_path" ]] || return 1
  [[ "$actual_files" == "$expected_files" ]] || return 1
  [[ "$actual_service" == "$service_name" ]] || return 1
  [[ "$actual_hash" == "$expected_hash" ]] || return 1
}

smoke_ceo_discord_ingress() {
  # Check only presence and the non-secret internal route. Never emit the
  # bearer credential: deployment output must remain safe to retain.
  docker exec hedgefund-ceo-hermes sh -eu -c '
    test -n "${CEO_DISCORD_INGRESS_API_KEY:-}"
    test "${HGFINANCE_DISCORD_INGRESS_URL:-}" = "http://portfolio-bff:8000/ui/ceo/ingress"
  ' >/dev/null 2>&1 || return 1
  docker exec hedgefund-portfolio-bff sh -eu -c '
    test -n "${CEO_DISCORD_INGRESS_API_KEY:-}"
    test -n "${DISCORD_ACTOR_MAP:-}"
  ' >/dev/null 2>&1 || return 1
  # Exercise the exact authenticated private hop without creating a directive:
  # an empty object must reach BFF validation and be rejected as 422.
  docker exec hedgefund-ceo-hermes python -c '
import os
import urllib.error
import urllib.request

request = urllib.request.Request(
    os.environ["HGFINANCE_DISCORD_INGRESS_URL"],
    data=b"{}",
    headers={
        "Authorization": "Bearer " + os.environ["CEO_DISCORD_INGRESS_API_KEY"],
        "Content-Type": "application/json",
    },
    method="POST",
)
try:
    urllib.request.urlopen(request, timeout=10)
except urllib.error.HTTPError as exc:
    raise SystemExit(0 if exc.code == 422 else 1) from None
raise SystemExit(1)
  ' >/dev/null 2>&1 || return 1
}

capture_rollback_images() {
  local spec container_name service_name image_id image_ref protected_tag previous_commit
  local expected_images
  [[ -n "$PREVIOUS_RELEASE" ]] || return 0
  previous_commit="${PREVIOUS_RELEASE##*/}"
  [[ "$previous_commit" =~ ^[0-9a-f]{40,64}$ ]] || return 1
  compose_release "$PREVIOUS_RELEASE" config --quiet || return 1
  # Rebuild the four local order-path images from the exact previous worktree.
  # This also repairs a mutable tag that an out-of-band legacy Compose command
  # may have overwritten. The external Trading Hermes image is instead taken
  # from its release-owned running container below.
  compose_release "$PREVIOUS_RELEASE" build \
    ls-realtime trading-api paper-order-orchestrator-mcp portfolio-bff ceo-hermes \
    || return 1
  expected_images="$(compose_release "$PREVIOUS_RELEASE" config --images)" || return 1
  assert_release_owned_container \
    "$PREVIOUS_RELEASE" hedgefund-trading-hermes trading-hermes || return 1
  for spec in "${MANAGED_RELEASE_SERVICE_SPECS[@]}"; do
    container_name="${spec%%:*}"
    service_name="${spec#*:}"
    docker inspect "$container_name" >/dev/null 2>&1 || return 1
    image_ref="$(docker inspect --format '{{.Config.Image}}' "$container_name")" || return 1
    grep -Fxq -- "$image_ref" <<<"$expected_images" || return 1
    if [[ "$service_name" == "trading-hermes" ]]; then
      image_id="$(docker inspect --format '{{.Image}}' "$container_name")" || return 1
    else
      image_id="$(docker image inspect --format '{{.Id}}' "$image_ref")" || return 1
    fi
    [[ "$image_id" =~ ^sha256:[0-9a-f]{64}$ ]] || return 1
    [[ -n "$image_ref" && "$image_ref" != *@sha256:* ]] || return 1
    docker image inspect "$image_id" >/dev/null 2>&1 || return 1
    protected_tag="hgfinance-rollback/${container_name#hedgefund-}:$previous_commit"
    docker image tag "$image_id" "$protected_tag" || return 1
    ROLLBACK_IMAGE_REF_BY_CONTAINER["$container_name"]="$image_ref"
    ROLLBACK_IMAGE_TAG_BY_CONTAINER["$container_name"]="$protected_tag"
  done
  ROLLBACK_IMAGES_CAPTURED=1
}

restore_rollback_images() {
  local spec container_name image_ref protected_tag
  ((ROLLBACK_IMAGES_CAPTURED == 1)) || return 0
  for spec in "${MANAGED_RELEASE_SERVICE_SPECS[@]}"; do
    container_name="${spec%%:*}"
    image_ref="${ROLLBACK_IMAGE_REF_BY_CONTAINER[$container_name]:-}"
    protected_tag="${ROLLBACK_IMAGE_TAG_BY_CONTAINER[$container_name]:-}"
    [[ -n "$image_ref" && -n "$protected_tag" ]] || return 1
    docker image inspect "$protected_tag" >/dev/null 2>&1 || return 1
    docker image tag "$protected_tag" "$image_ref" || return 1
  done
}

remove_rollback_image_tags() {
  local spec container_name protected_tag
  ((ROLLBACK_IMAGES_CAPTURED == 1)) || return 0
  for spec in "${MANAGED_RELEASE_SERVICE_SPECS[@]}"; do
    container_name="${spec%%:*}"
    protected_tag="${ROLLBACK_IMAGE_TAG_BY_CONTAINER[$container_name]:-}"
    [[ -z "$protected_tag" ]] || docker image rm "$protected_tag" >/dev/null 2>&1 || true
  done
  ROLLBACK_IMAGES_CAPTURED=0
}

stop_order_hermes() {
  local container_name
  for container_name in hedgefund-ceo-hermes hedgefund-trading-hermes; do
    if [[ "$(docker inspect --format '{{.State.Running}}' "$container_name" 2>/dev/null || true)" == "true" ]]; then
      docker stop --time 20 "$container_name" >/dev/null || return 1
    fi
  done
}

activate_release_services() {
  local release_path="$1"
  stop_order_hermes || return 1
  # Keep every unrelated team service and image untouched. Only shared Redis,
  # the LS market-data reader and the five PAPER order-path services belong to
  # this bounded release switch.
  compose_release "$release_path" up -d --no-deps redis || return 1
  wait_container hedgefund-timescaledb 240 || return 1
  wait_container hedgefund-redis 180 || return 1
  compose_release "$release_path" up -d --no-deps --force-recreate ls-realtime || return 1
  wait_container hedgefund-ls-realtime 180 || return 1
  assert_release_owned_container "$release_path" hedgefund-ls-realtime ls-realtime || return 1

  # Establish the deterministic backend chain before either gateway can read
  # a Discord message or dispatch an order card.
  compose_release "$release_path" up -d --no-deps --force-recreate trading-api || return 1
  wait_container hedgefund-trading-api 240 || return 1
  compose_release "$release_path" up -d --no-deps --force-recreate \
    paper-order-orchestrator-mcp portfolio-bff || return 1
  wait_container hedgefund-paper-order-orchestrator-mcp 240 || return 1
  wait_http_ready hedgefund-portfolio-bff 8000 /health/ready 240 || return 1
  wait_http_ready hedgefund-accounting-api 8000 /health/ready 180 || return 1

  assert_release_owned_container "$release_path" hedgefund-trading-api trading-api || return 1
  assert_release_owned_container "$release_path" hedgefund-paper-order-orchestrator-mcp paper-order-orchestrator-mcp || return 1
  assert_release_owned_container "$release_path" hedgefund-portfolio-bff portfolio-bff || return 1

  compose_release "$release_path" up -d --no-deps --force-recreate \
    ceo-hermes trading-hermes || return 1
  wait_container hedgefund-ceo-hermes 180 || return 1
  wait_container hedgefund-trading-hermes 180 || return 1
  wait_hermes_gateway hedgefund-ceo-hermes 180 || return 1
  wait_hermes_gateway hedgefund-trading-hermes 180 || return 1
  assert_release_owned_container "$release_path" hedgefund-ceo-hermes ceo-hermes || return 1
  assert_release_owned_container "$release_path" hedgefund-trading-hermes trading-hermes || return 1
  smoke_ceo_discord_ingress || return 1
  smoke_trading_paper_order_mcp || return 1
}

backup_private_databases() {
  local backup_root="$RELEASES_ROOT/backups/$RELEASE_COMMIT"
  local database_name temporary target market_backed_up=0
  install -d -m 700 -- "$backup_root"
  for database_name in market "${HEDGEFUND_CONTROL_DB_NAME_FOR_BACKUP:-control}"; do
    if ! docker exec hedgefund-timescaledb psql -U postgres -d market -Atqc \
      "select 1 from pg_database where datname = '$database_name' and datallowconn" \
      2>/dev/null | grep -qx 1; then
      continue
    fi
    target="$backup_root/$database_name.dump"
    if [[ -s "$target" ]]; then
      [[ "$database_name" == "market" ]] && market_backed_up=1
      continue
    fi
    temporary="$backup_root/.$database_name.dump.tmp.$$"
    if ! docker exec hedgefund-timescaledb pg_dump -U postgres -d "$database_name" \
      --format=custom --no-owner --no-privileges >"$temporary"; then
      rm -f -- "$temporary"
      return 1
    fi
    chmod 600 "$temporary"
    [[ -s "$temporary" ]] || { rm -f -- "$temporary"; return 1; }
    mv -f -- "$temporary" "$target"
    [[ "$database_name" == "market" ]] && market_backed_up=1
  done
  ((market_backed_up == 1))
}

rollback_release() {
  local original_code="$1"
  local image_restore_failed=0
  local profile_restore_failed=0
  local profile_restored=0
  local profile_restart_failed=0
  local release_services_restored=0
  local profile_container rollback_link previous_commit
  trap - ERR INT TERM
  set +e
  if [[ "$PROFILE_INSTALL_ACTIVE" == "1" && -n "$PROFILE_BACKUP_DIR" \
    && -f "$PROFILE_BACKUP_DIR/manifest.json" ]]; then
    python3 "$RELEASE/scripts/aws_install_hermes_profiles.py" restore \
      --runtime-root "$PROFILE_RUNTIME_ROOT" \
      --backup-dir "$PROFILE_BACKUP_DIR" >/dev/null 2>&1 \
      && profile_restored=1 \
      || profile_restore_failed=1
  fi
  restore_rollback_images >/dev/null 2>&1 || image_restore_failed=1
  if [[ "$SWITCH_STARTED" == "1" && -n "$PREVIOUS_RELEASE" ]]; then
    say "Release health failed; restoring the previous release (database migrations stay additive)..."
    if ((image_restore_failed == 0)); then
      compose_release "$PREVIOUS_RELEASE" config --quiet >/dev/null 2>&1 \
        && activate_release_services "$PREVIOUS_RELEASE" >/dev/null 2>&1 \
        && release_services_restored=1 \
        || image_restore_failed=1
      if ((release_services_restored == 1)); then
        rollback_link="$RELEASES_ROOT/current.rollback.$$"
        previous_commit="${PREVIOUS_RELEASE##*/}"
        ln -s "$PREVIOUS_RELEASE" "$rollback_link" \
          && mv -Tf -- "$rollback_link" "$CURRENT_LINK" \
          && printf '%s\n' "$previous_commit" >"$RELEASES_ROOT/state/current-commit" \
          || image_restore_failed=1
      fi
    fi
  elif [[ "$SWITCH_STARTED" == "1" ]]; then
    printf '%s\n' "ERROR: first deployment failed after service switch; no prior release exists" >&2
  fi
  if ((profile_restored == 1 && release_services_restored == 0 && SWITCH_STARTED == 0)); then
    # A running Hermes process may retain its startup config even after the
    # host files are restored. Restart only the two profile owners so the
    # restored files, existing credentials and durable state are reloaded.
    for profile_container in hedgefund-ceo-hermes hedgefund-trading-hermes; do
      if docker inspect "$profile_container" >/dev/null 2>&1; then
        docker restart "$profile_container" >/dev/null 2>&1 \
          && wait_container "$profile_container" 180 \
          || profile_restart_failed=1
      fi
    done
  fi
  if ((profile_restore_failed == 1)); then
    printf '%s\n' "ERROR: previous Hermes runtime profiles require manual restoration" >&2
  fi
  if ((image_restore_failed == 1)); then
    printf '%s\n' "ERROR: previous release images or services require manual restoration" >&2
  fi
  if ((profile_restart_failed == 1)); then
    printf '%s\n' "ERROR: a Hermes container did not reload its restored profile" >&2
  fi
  printf 'ERROR: deployment aborted (exit %s)\n' "$original_code" >&2
  exit "$original_code"
}
trap 'rollback_release "$?"' ERR
trap 'rollback_release 130' INT
trap 'rollback_release 143' TERM

say "Protecting the currently running order-path images for rollback..."
capture_rollback_images
say "Validating and building release $RELEASE_COMMIT..."
compose_release "$RELEASE" config --quiet
compose_release "$RELEASE" --profile deployment build --pull \
  ls-realtime trading-api paper-order-orchestrator-mcp portfolio-bff ceo-hermes \
  database-bootstrap reference-bootstrap
EXTERNAL_PULL_PLAN="$(
  compose_release "$RELEASE" --profile deployment config --format json \
    | external_pull_service_plan
)" || die "could not determine external Compose image pull plan"
if [[ -n "$EXTERNAL_PULL_PLAN" ]]; then
  EXTERNAL_PULL_SERVICES=()
  while IFS=$'\t' read -r external_service external_image; do
    [[ -n "$external_service" && -n "$external_image" ]] \
      || die "invalid external Compose image pull plan"
    if ! docker image inspect "$external_image" >/dev/null 2>&1; then
      EXTERNAL_PULL_SERVICES+=("$external_service")
    fi
  done <<<"$EXTERNAL_PULL_PLAN"
  if ((${#EXTERNAL_PULL_SERVICES[@]} > 0)); then
    compose_release "$RELEASE" pull --policy missing "${EXTERNAL_PULL_SERVICES[@]}"
  fi
fi

DATABASE_CONTAINER_EXISTED=0
if docker inspect hedgefund-timescaledb >/dev/null 2>&1; then
  DATABASE_CONTAINER_EXISTED=1
fi

# Back up the currently running database before Compose is allowed to
# reconcile its image, environment or container. A stopped existing database
# is a fail-closed operator condition; starting it with the new release first
# would defeat the purpose of a pre-change backup.
if ((BACKUP_BEFORE_MIGRATION == 1 && DATABASE_CONTAINER_EXISTED == 1)); then
  [[ "$(docker inspect --format '{{.State.Running}}' hedgefund-timescaledb 2>/dev/null)" == "true" ]] \
    || die "existing TimescaleDB container is not running; pre-migration backup cannot proceed"
  # Read the non-secret database name only; the password/DSNs are never
  # sourced into the shell or passed on a command line.
  HEDGEFUND_CONTROL_DB_NAME_FOR_BACKUP="$(python3 - "$RUNTIME_ENV" <<'PY'
import re
import sys
from pathlib import Path
value = "control"
for raw in Path(sys.argv[1]).read_text(encoding="utf-8-sig").splitlines():
    match = re.match(r"^\s*(?:export\s+)?HEDGEFUND_CONTROL_DB_NAME\s*=\s*(.*)$", raw)
    if match:
        value = match.group(1).strip().strip("\"'") or "control"
print(value)
PY
)"
  export HEDGEFUND_CONTROL_DB_NAME_FOR_BACKUP
  say "Creating protected pre-migration database backups..."
  backup_private_databases
elif ((BACKUP_BEFORE_MIGRATION == 1)); then
  say "No existing TimescaleDB container; skipping the empty first-deploy backup."
fi

# Only after an existing database is protected (or the explicit backup skip / first
# deployment applies) may the new release reconcile the durable DB container.
compose_release "$RELEASE" up -d --no-deps timescaledb
wait_container hedgefund-timescaledb 240

bootstrap_command=(python scripts/aws_database_bootstrap.py)
if ((ADOPT_EXISTING_MARKET == 1)); then
  bootstrap_command+=(--adopt-existing-market)
fi
if ((SEED_PAPER_PRINCIPAL == 1)); then
  bootstrap_command+=(--seed-paper-principal)
fi
if ((TOP_UP_PAPER_CASH == 1)); then
  bootstrap_command+=(--top-up-paper-cash)
fi
compose_release "$RELEASE" --profile deployment run --rm --no-deps \
  database-bootstrap "${bootstrap_command[@]}"

# A fresh private control database has no instrument aliases or trading
# calendar.  Provision them from read-only LS market-data calls and fail before
# the service switch unless 005930, today's reviewed KRX session, PAPER
# OWNER+TRADER scope and positive cash are all usable.
say "Provisioning and auditing PAPER order reference data..."
compose_release "$RELEASE" --profile deployment run --rm --no-deps \
  reference-bootstrap

# The release worktree, never the dirty legacy checkout, owns only the marked
# CEO/Trading SOUL sections and Trading's user-paper-order MCP entry. Merge
# those fragments before switching containers. The helper backs up only the
# three affected runtime files, preserves all host integrations and durable
# auth/memory/state, renders the private MCP credential without logging it,
# and uses atomic replace.
install -d -m 700 -- "$RELEASES_ROOT/profile-backups"
PROFILE_BACKUP_DIR="$(mktemp -d "$RELEASES_ROOT/profile-backups/$RELEASE_COMMIT.XXXXXX")"
PROFILE_INSTALL_ACTIVE=1
say "Merging release-owned CEO and Trading Hermes profile fragments..."
python3 "$RELEASE/scripts/aws_install_hermes_profiles.py" install \
  --release-root "$RELEASE" \
  --runtime-env "$RUNTIME_ENV" \
  --runtime-root "$PROFILE_RUNTIME_ROOT" \
  --backup-dir "$PROFILE_BACKUP_DIR" >/dev/null

SWITCH_STARTED=1
say "Activating deterministic backends before the two Hermes gateways..."
activate_release_services "$RELEASE"

link_temp="$RELEASES_ROOT/current.tmp.$$"
ln -s "$RELEASE" "$link_temp"
mv -Tf -- "$link_temp" "$CURRENT_LINK"
printf '%s\n' "$RELEASE_COMMIT" >"$RELEASES_ROOT/state/current-commit"
SWITCH_STARTED=0
PROFILE_INSTALL_ACTIVE=0
remove_rollback_image_tags
trap - ERR INT TERM
say "PAPER release activated: $RELEASE_COMMIT"
