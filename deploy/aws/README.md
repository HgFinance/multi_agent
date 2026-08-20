# AWS EC2 PAPER-order runtime

AWS production uses one canonical checkout:

`/home/ubuntu/hgfinance`

Detached deployment worktrees are retired. Do not recreate
`/home/ubuntu/hgfinance-releases` and do not restore the retired
detached-release deployer.

The AWS runtime Compose model is the canonical repository root compose file
combined with `deploy/aws/docker-compose.paper-order.yml`.

## Runtime boundary

- Hosted Supabase is identity-only. `SUPABASE_URL` selects the Auth issuer;
  ES256/RS256 access tokens are verified against its public JWKS endpoint.
  A hosted Supabase database URL is never used by an application container.
- The private `timescaledb` container owns two distinct PostgreSQL databases:
  `control` for operational/domain state and `market` for price,
  tick/quote and microstructure data.  The development host publication on
  `0.0.0.0:5434` is removed; DB administration goes through SSM plus
  `docker exec`, not a public EC2 port.
- Every application service that had a `DATABASE_URL` is overridden to a
  non-superuser private `control` login.  The same generic runtime login is
  used for market collectors/readers, with only `market` schema table,
  sequence and function privileges.  Only market-data consumers receive that
  second DSN.
- The order orchestrator, Trading and Accounting mutation paths use three
  separate `NOINHERIT` logins.  Each login can `SET ROLE` to exactly one
  corresponding NOLOGIN role (`svc_order_orchestrator`, `svc_trading_api`, or
  `svc_accounting_ledger`).  The PostgreSQL superuser DSNs are present only in
  the two deployment bootstrap jobs and the administrator-controlled backup.
  The mixed-purpose BFF keeps its generic DSN for identity/reference reads and
  receives `ORDER_ORCHESTRATOR_DATABASE_URL` only for the critical order
  repository. Trading API, directive worker and outbox relay use only the
  Trading login; Accounting API, ledger consumer and close scheduler use only
  the Accounting login. Those two DSNs select their sole capability role at
  connection startup for code paths that do not open a role-scoped repository.
- The generic login inherits the bounded `service_role` compatibility grants:
  required noncritical domain DML, read-only reference/accounting data and
  `SELECT execution.market_snapshots`. It has no user-order/directive,
  intent, reservation, broker-order/fill, outbox or accounting mutation grant. Existing
  Quant/Audit/QA workers may explicitly reduce to their existing scoped roles;
  none can reduce to an order, Trading, or Accounting critical role.
- `portfolio-bff` is fixed to production `supabase_jwt` authentication with
  required authentication and an empty fixture grant list.  Because this EC2
  deployment is currently backend-only, its CORS allowlist may be empty: no
  browser origin receives an allow-origin header and browser preflights are
  rejected.
- Discord ingress stays on the private Compose network. `ceo-hermes` posts to
  `http://portfolio-bff:8000/ui/ceo/ingress`, and only those two services
  receive the dedicated `CEO_DISCORD_INGRESS_API_KEY` bearer credential.
- Trading API, directive worker and accounting are fixed to durable PAPER
  execution.  This overlay provides no switch to a live broker adapter.
- The root development `timescaledb.cpuset=22,23` pin is removed for EC2;
  the root CPU quota remains in force.  The AWS overlay also pins the verified
  PG17 Timescale image digest so a deployment cannot silently upgrade the
  database under a mutable tag; changing `HEDGEFUND_TIMESCALE_IMAGE` is an
  explicit database-upgrade operation.

## One-time preparation

The source `.env` must contain valid values for:

- `HEDGEFUND_TSDB_PASSWORD` (URL-safe, at least 16 characters);
- `SUPABASE_URL`;
- `LS_APP_KEY`, `LS_APP_SECRET_KEY`, and `LS_REST_BASE_URL` for read-only KRX
  instrument-master and calendar evidence calls (these credentials are not
  passed to Trading and cannot change its hard `paper` broker adapter);
- `DISCORD_ACTOR_MAP` with at least one valid
  `discord_user_id:user_id:fund_id` binding whose user and fund match
  `PAPER_SEED_USER_ID` and `PAPER_SEED_FUND_ID` (or their documented defaults).
  Deployment checks this relationship without printing any identifier or map;
- the four distinct, at-least-32-character service credentials
  `MCP_TRADING_ORDER_API_KEY`, `TRADING_SERVICE_AUTH_SECRET`,
  `TRADING_INTERNAL_SERVICE_AUTH_SECRET`, and
  `CEO_DISCORD_INGRESS_API_KEY`;
- six distinct, URL-safe, at-least-32-character private database credentials
  `HEDGEFUND_RUNTIME_DB_PASSWORD`, `HEDGEFUND_ORDER_DB_PASSWORD`,
  `HEDGEFUND_TRADING_DB_PASSWORD`, and
  `HEDGEFUND_ACCOUNTING_DB_PASSWORD`,
  `HEDGEFUND_CONDITIONAL_ORCHESTRATOR_DB_PASSWORD`, and
  `HEDGEFUND_CONDITIONAL_WORKER_DB_PASSWORD`. These six and the four service
  credentials must be ten different values. The two conditional-rule logins
  cannot assume each other's role and neither can write USER_DIRECTIVE rows.

`PORTFOLIO_CORS_ALLOW_ORIGINS` is optional while no browser UI is deployed.
Leave it absent or empty to deny every browser cross-origin read.  After a UI
is deployed, set a comma-separated list of its exact HTTPS origins (scheme,
host and optional port only).  Wildcards, HTTP, credentials, paths, queries,
fragments and empty entries inside a non-empty list stop deployment.

Docker Compose 2.24.4 or newer is required because the overlay uses the
official `!override` merge tag to remove the development database port.

`SUPABASE_PUBLISHABLE_KEY` and legacy `SUPABASE_ANON_KEY` are optional for the
current asymmetric-token project.  If both are absent, deployment fetches the
configured JWKS URL, the configured Auth issuer's JWKS URL, or by default
`SUPABASE_URL/auth/v1/.well-known/jwks.json`) without redirects and requires a
public P-256/ES256 or at-least-2048-bit RSA/RS256 verification key. Symmetric
`oct`, private-key material, unsupported algorithms, malformed/oversized JSON
and an unreachable endpoint all stop deployment before databases are touched.
A public key remains necessary only if legacy HS256 access tokens must be
verified through Supabase Auth `/user`; never put an `sb_secret_` or service
role key in either browser-key setting.

`scripts/configure_paper_order_env.py --runtime aws` remains the
credential configuration utility. Detached-release `runtime.env` state under
`/home/ubuntu/hgfinance-releases` is retired and must not be recreated.

`.env.example` is the repository's only tracked environment template. Do not
maintain a second `.env.aws.template`: it drifts from Compose and creates
duplicate assignments. AWS-specific values belong only in the private
mode-0600 `runtime.env` described above.

The Timescale password is both PostgreSQL's literal password and a component of
an internal PostgreSQL URL.  It must therefore use only URL-safe
letters/digits/`._~-`; rotate a password containing reserved characters before
the first deployment.  Do not percent-encode only the `.env` value because the
container would then store the encoded text as the literal server password.

## Deploy

The detached-release deployment workflow has been retired.

Do not:

- recreate `/home/ubuntu/hgfinance-releases`;
- create detached deployment worktrees;
- restore `scripts/aws_deploy_paper_order_release.sh`;
- operate production from a secondary checkout.

The canonical repository is `/home/ubuntu/hgfinance`.

Before any production-mutating Compose operation, verify the intended Compose
files and confirm that existing containers use
`/home/ubuntu/hgfinance` as `com.docker.compose.project.working_dir`.

Do not run `docker compose down`, remove production volumes, reset live
databases, or restart the model/vLLM as part of ordinary application
deployment.

## PAPER principal provisioned by default

After migrations, the bootstrap idempotently provisions:

| Item | Value |
|---|---|
| Supabase subject | `00000000-0000-4000-8000-00000000cec0` |
| PAPER fund | `5c26db42-ce83-4daf-b1dc-c81680c13a6c` / `ACC01-PAPER` / KRW |
| PAPER book | `07d913de-9a5b-4cf5-b893-31a625445761` / `MAIN` |
| Memberships | active `OWNER` and `TRADER` |
| Initial cash floor | KRW 1,000,000,000 |

The cash is backed by a balanced capital-seed journal and the complete ledger
account chart.  A later deployment never silently refills spent cash.  If the
available balance is below the configured floor, deployment stops; an explicit
PAPER-only refill requires `--top-up-paper-cash`.  `PAPER_SEED_*` values can be
set in the private runtime environment when the hosted Auth subject or test
capital changes.

To skip this test principal entirely, use `--no-seed-paper-principal`.
`--skip-database-backup` also exists for an operator who has already taken and
verified an EBS/RDS-equivalent snapshot; skipping the default dump is an
explicit risk acknowledgement.

## Reference/calendar renewal boundary

The deployment reference job performs market-data reads only; it never submits
an order.  The first run stores the bounded observed calendar before the
complete declared version.  Later runs re-check new observations but do not
install a newer partial version, keeping legacy latest-version readers from
losing today's row.  Instrument and calendar writes are idempotent.

The reviewed declaration currently ends on 2026-12-31.  A 2027 deployment is
intentionally blocked until the repository holiday/special-session declaration
is reviewed and extended; weekdays are never guessed into executable sessions.
