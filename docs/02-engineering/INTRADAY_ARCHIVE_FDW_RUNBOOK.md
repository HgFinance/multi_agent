# Intraday archive FDW runbook

This runbook connects the market database to the read-only intraday archive.
It is intentionally an explicit deployment step: neither credentials nor FDW
DDL belong in a Docker image, Compose file, or Git repository.

## Contract

The bootstrap job requires two process environment variables:

- `TIMESCALE_DATABASE_URL`: target market database. Its current database user
  owns the FDW objects and receives the user mapping.
- `INTRADAY_ARCHIVE_DATABASE_URL`: source archive PostgreSQL URL, including the
  least-privilege source role and password. The source role needs `USAGE` on
  `public` and `SELECT` on `public.quotes` and `public.ticks` only.

The resulting target objects are:

- extension `postgres_fdw`;
- schema `ext_src`;
- server `trading_src`, with source host, port, database name and
  `fetch_size=50000`;
- a mapping from the target `CURRENT_USER` to the source role carried by the
  source URL;
- `ext_src.quotes` and `ext_src.ticks`, imported from source schema `public`.

Do not print either variable, pass it as a command-line argument, save it in an
`.env` file, or enable shell tracing. Inject both at process start from the
deployment secret provider. Credential rotation is a deployment operation, not
a source-code change.

## Initial deployment

Use an administrative one-shot job whose network policy can reach both
databases. The target role must be permitted to create `postgres_fdw`, a schema,
a foreign server, and its own user mapping. Runtime workers do not need the
source URL after this job succeeds.

1. Inject both required variables from AWS Secrets Manager (AWS) or a local
   password manager (developer machine).
2. Audit the current state. On an empty target this correctly fails and reports
   the *kinds* of missing objects, never their values:

   ```text
   python scripts/bootstrap_intraday_archive_fdw.py --check
   ```

3. Apply only missing objects:

   ```text
   python scripts/bootstrap_intraday_archive_fdw.py
   ```

4. Require a clean post-deployment preflight:

   ```text
   python scripts/bootstrap_intraday_archive_fdw.py --check
   ```

The command exits `0` only after direct source validation and bounded reads
through both foreign tables succeed. Expected configuration drift exits `2`.
Unexpected driver errors are redacted and exit `3`.

## AWS execution

Run the script as an ephemeral administration task in the same VPC/security
group path as the target market database. Inject both URLs directly from AWS
Secrets Manager into that task. The task needs outbound TCP access to the source
archive and target database; neither database should be exposed publicly just
for bootstrapping.

Keep the bootstrap task separate from the long-running factory service. Its IAM
role may read only the two database secrets, and its database role may manage
only the FDW contract. After deployment, retain `TIMESCALE_DATABASE_URL` in the
factory worker and remove `INTRADAY_ARCHIVE_DATABASE_URL` from the worker
environment.

Migration
`supabase/migrations/20260818001400_intraday_completed_second_dataset.sql` is
a separate control-database migration that provides the quant dataset manifest
and completed-session ledger contract. Apply it before factory replay so the
resolver can identify the externally archived observations; it does not create
or configure the FDW objects in this runbook.

## Local execution

Run the same three commands from the repository virtual environment. The
target URL normally points to the local market database port, while the source
URL points to the archive host reachable from the developer machine. Inject
both values into the current process without committing them. If the archive
is reachable only inside Docker networking, run an ephemeral administration
container attached to both relevant networks and inject the variables there.

No Compose change is required by this runbook. This avoids persisting archive
credentials in service definitions and keeps local and AWS behavior identical.

## Drift and rotation

The default apply and `--check` modes fail closed when an existing server,
mapping, or foreign table differs from the declared contract. Inspect the cause
before making changes. For an intentional endpoint or credential rotation, run:

```text
python scripts/bootstrap_intraday_archive_fdw.py --reconfigure
python scripts/bootstrap_intraday_archive_fdw.py --check
```

`--reconfigure` may alter server options and the current user's mapping, or
re-import an already foreign table. It never drops a local table occupying
`ext_src.quotes` or `ext_src.ticks`; that conflict requires manual review.

## Coverage semantics

Preflight verifies the complete L10 quote schema, tick trade/OFI columns,
Timescale chunk metadata spanning at least 60 calendar days, a bounded row read
from each newest source chunk, and the same bounded read through each FDW table.
It never performs an unbounded raw-row count or whole-archive min/max scan.

This is an infrastructure coverage guard. The experiment resolver remains the
scientific authority for the exact 61-session replay window, stock-universe
eligibility, dataset identity, and minimum observed sessions. Passing this
bootstrap does not claim that an alpha result exists.
