# Local mock-investment authentication contract

`hgfinance` is currently a local mock-investment application. It is not a
production deployment and it does not implement a browser login, account
session, external identity provider, or user JWT flow.

The supported path is:

1. `npm run dev` starts the `ai-office` frontend without a login route or auth
   provider.
2. `npm run bff` starts the local BFF with `PORTFOLIO_AUTH_MODE=fixture`,
   `PORTFOLIO_AUTH_REQUIRED=false`, and `PORTFOLIO_LIVE_MODE=fixture`.
3. The frontend uses the fixed local demo identity
   `00000000-0000-4000-8000-00000000cec0` and passes it as `X-User-Id` when a
   seeded paper book is needed. `DISCORD_ACTOR_MAP` may override the display
   binding for a local actor; it is not public-user authentication.

Supabase Auth login/session/JWT integration is intentionally not implemented
and must not be reintroduced through a package, route, provider, environment
variable, deployment setting, or documentation. Supabase database migrations
and private operational data references are separate data-plane concerns; they
do not authorize a browser user.

The fixture-only guard rejects any non-`fixture` portfolio auth mode. Local
mock runs also disable live LS account reads, so an unavailable broker endpoint
cannot turn the frontend into a red error state. AWS/EB deployment settings are
legacy references and are not part of the supported mock-investment workflow.
