# AWS EC2 PAPER-order release deployment

This deployment keeps the existing `/home/ubuntu/hgfinance` checkout entirely
read-only.  A dedicated bare repository creates detached release worktrees
under `/home/ubuntu/hgfinance-releases/worktrees`, and Compose always receives
the explicit project name `hedgefund`, the repository root compose file and
`deploy/aws/docker-compose.paper-order.yml`.

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

`scripts/configure_paper_order_env.py --runtime aws` can atomically
create/preserve all ten service/database credentials without printing their
values.  Invalid, short, duplicate, placeholder or non-URL-safe database
credentials are independently rotated.  The deployer imports
the existing `.env` once into the mode-0600 file
`/home/ubuntu/hgfinance-releases/state/runtime.env` and runs the configurator
there, so the dirty source checkout is not changed.  To deliberately import
later source `.env` edits, use `--refresh-runtime-env`.

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

The first activation has no previous release to restore, so it requires an
explicit acknowledgement:

```bash
bash scripts/aws_deploy_paper_order_release.sh \
  --ref main \
  --allow-first-deploy
```

Later releases need only:

```bash
bash /home/ubuntu/hgfinance-releases/current/scripts/aws_deploy_paper_order_release.sh \
  --ref main
```

The current deployer fetches the requested commit and, before changing runtime
state, hands control to that target worktree's deploy script. This one-time
self-handoff ensures a release can fix its own deployment gates instead of
being deployed by stale logic from the previous release.

On this host, never run `docker compose up`, `build`, `restart` or `pull` from
`/home/ubuntu/hgfinance`.  That legacy checkout is not a deployment root: even
one service-specific invocation can silently replace a release-owned container
with a model that lacks the AWS overlay (including the private Discord ingress
contract).  Operational Compose commands must use the release deployer above;
inspect-only commands should also use both files and the private runtime env.

The deployment sequence is fail-closed:

1. fetch into the dedicated bare repository and create a detached worktree;
2. validate the secret contract and merged Compose model without printing it;
3. build only the LS realtime reader, bounded PAPER order-path services and two
   one-shot database jobs before touching running services; when a previous
   release exists, first rebuild its local managed images from that exact
   worktree and protect all prior image IDs (including external Trading
   Hermes) under private rollback tags, so mutable Compose tags cannot defeat
   rollback. Existing external images are never refreshed merely because a
   mutable tag exists;
4. when the Timescale container already exists, require it to be running and
   write mode-0600 custom-format dumps to
   `hgfinance-releases/backups/<commit>/` **before** any Compose reconciliation;
   a first deployment with no container skips this empty/initial backup;
5. start/reuse only `timescaledb`, create `control`, replay all 88 Supabase
   migrations there, replay all 8 Timescale migrations in `market` with
   per-file atomic history, and idempotently provision/audit the six
   non-superuser runtime logins;
6. read the LS KRX instrument master, cross-check the repository-reviewed 2026
   exchange calendar against observed daily bars, and fail unless the full
   active-stock catalog, unique `005930` mapping, applicable REGULAR session,
   PAPER OWNER+TRADER scope and positive KRW cash are present;
7. from the detached release, atomically merge only Trading's
   `mcp_servers.user-paper-order` entry and the marked direct-user PAPER
   sections in CEO/Trading `SOUL.md` into `/home/ubuntu/.hermes/profiles`,
   preserving host-only integrations and rendering the Trading MCP Bearer from
   private `runtime.env` without logging it;
8. stop the CEO and Trading Hermes gateways, leave unrelated team services and
   their image tags untouched, then recreate and verify LS realtime, Trading
   API, and MCP+BFF in that order; only after those deterministic backends are
   ready may both Hermes gateways be recreated;
9. require those six managed containers' Compose project, working directory, config
   files and config hash to match the detached release; also require the CEO
   and BFF to have the private Discord ingress contract and require an
   authenticated empty-object probe to reach BFF validation as HTTP 422,
   without printing its bearer credential or creating a directive;
10. inside the running Trading Hermes container, require an authenticated MCP
   tools/list exchange that discovers exactly `process_user_paper_order`;
11. update the `current` symlink only after all gates pass.

If a post-switch health gate fails, the protected prior image IDs are restored
to their original Compose tags and the same ordered backend-before-Hermes
activation is force-recreated from the previous worktree. Ownership, ingress
and MCP discovery are rechecked before rollback is considered usable. Database
migrations are additive and are not automatically reversed; the protected
pre-migration dumps remain available for an operator-controlled restore. The deployer never runs
`git pull`, `git reset`, `git clean`, `docker compose down`, or volume removal
against the legacy checkout or live data.

Each deployment also keeps a mode-0700 profile backup below
`hgfinance-releases/profile-backups/`.  It contains only Trading `config.yaml`
and the two managed `SOUL.md` files.  A failure before or after the service
switch restores those prior files before rollback.  CEO `config.yaml`,
host-only Trading settings and MCP servers, profile credentials (`auth.json`),
memories, sessions, logs, state and shared Kanban files are never replaced or
backed up by this step.

## Existing 61-day market database

A market schema containing data but no `hgfinance_migrations` history stops the
deployment.  Do not replay `001_initial_market_data.sql` blindly: it contains
non-idempotent table creation.  For the known transferred 61-day database, use:

```bash
bash scripts/aws_deploy_paper_order_release.sh \
  --ref main \
  --adopt-existing-market
```

Adoption writes history only after all expected relations, feature columns,
v4/v5 constraints and bounded Timescale compression jobs pass.  A partial or
drifted schema remains blocked.  Subsequent migrations replay normally.

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
