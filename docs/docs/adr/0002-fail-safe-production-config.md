# 2. Fail-safe production configuration validation

- Status: Accepted
- Date: 2026-06-23

## Context

The defaults convenient for development (auth disabled, raw SQL endpoint on,
`CORS_ORIGINS=*`, docs exposed) are dangerous in production. Relying on operators
to remember to flip every switch is error-prone and fails silently.

## Decision

Add `validate_production_config()` (in `warp.config.settings`), called during app
startup. When `APP_ENV=production`, it raises `ConfigurationError` and refuses to
start if any unsafe condition holds:

- `auth.enabled` is `false`
- `settings.enable_raw_query` is `true`
- `CORS_ORIGINS` contains `*`

These options keep their developer-friendly defaults outside production. The
shipped image defaults to `APP_ENV=production`, so a misconfigured deployment
fails loudly at boot.

## Consequences

- Insecure production deployments fail fast and visibly instead of silently
  exposing the API.
- Operators must provide explicit, safe values (documented in
  `docs/docs/deployment.md` and `.env.example`).
- The check is centralized and unit-tested.

## Addendum (1.0.0)

The check now lives in `truejoin.application.settings.validate_production_config`, and
`ConfigurationError` in `truejoin.domain.gateway.errors`. It also covers the settings added
since: `auth.oidc`, `auth.ldap` (no plain `ldap://`, a `TRUEJOIN_SECRET_KEY` for sessions)
and `auth.users`.
