# GitHub Actions

`.github/workflows/daily-nasdaq-cafe.yml` runs the collector every day at
07:17 Japan time. It can also be started manually from the Actions tab with
`today` or a specific `YYYY-MM-DD` date.

Each run uploads only the safe handoff files below for 14 days:

- `source_pack.md`
- `source_pack.json`
- `prompt_input.md`
- `CHATGPT_HANDOFF_YYYY-MM-DD.md`

Raw article HTML/PDF/text, full-text handoff files, local caches, OAuth files,
and credentials are intentionally excluded from artifacts and commits.

## Recommended configuration

In **Settings -> Secrets and variables -> Actions**, add these repository
secrets in priority order:

1. `TAVILY_API_KEY` - search and article extraction fallback
2. `FRED_API_KEY` - US interest-rate and macroeconomic data
3. `SERPAPI_API_KEY` - supplementary web/news search (optional)
4. `FMP_API_KEY` - supplementary market/company news (optional)
5. `LONGBRIDGE_OAUTH_CLIENT_ID` - portable OAuth public-client ID
6. `LONGBRIDGE_OAUTH_REFRESH_TOKEN` - portable OAuth refresh token
7. `LONGBRIDGE_SECRET_ROTATOR_TOKEN` - fine-grained PAT limited to this repository with `Secrets: Read and write`

Add `SEC_USER_AGENT` as a repository **variable**, not a secret. It is not an
API key; use an identifiable contact value such as:

```text
nasdaq-cafe/1.0 your-email@example.com
```

Missing optional data-source keys are recorded in `missing_data`. If both
portable Longbridge OAuth secrets are absent, the run continues and records
Longbridge as missing. A partial, invalid, live-account, permission-deficient,
or unreachable Longbridge configuration fails before collection.

## Longbridge portable OAuth setup

Do not copy the local Longbridge CLI `cli-auth` file into GitHub. Longbridge CLI
v0.26.0 encrypts that file with a machine-derived key, so a file created on a
Windows PC cannot be decrypted by a GitHub-hosted Linux runner.

Run the repository bootstrap script once from PowerShell instead:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\bootstrap_longbridge_portable_oauth.ps1
```

The script performs the official OAuth 2.0 public-client flow with PKCE, opens
the Longbridge authorization page, receives the localhost callback, exchanges
the one-time authorization code, and writes these values directly through
GitHub CLI without displaying them:

- `LONGBRIDGE_OAUTH_CLIENT_ID`
- `LONGBRIDGE_OAUTH_REFRESH_TOKEN`

Prerequisites:

```powershell
gh auth status
```

The existing `LONGBRIDGE_SECRET_ROTATOR_TOKEN` remains required. The old
`LONGBRIDGE_CLI_AUTH_B64` secret is unused and the bootstrap script removes it.

## Scheduled refresh sequence

Every run follows this order:

1. Exchange `LONGBRIDGE_OAUTH_REFRESH_TOKEN` at the official OAuth token endpoint.
2. Immediately persist the returned refresh token back to the repository secret.
3. Create a plaintext CLI compatibility session only inside the ephemeral runner.
4. Validate `lb_papertrading`, `US_QBBO_OpenAPI`, token status, and connectivity.
5. Execute the quote-only collector.
6. Delete temporary OAuth material with an `always()` cleanup step.

Persisting the rotated refresh token before validation and collection avoids
losing a single-use replacement token if a later step fails. Workflow
concurrency is serialized so two jobs cannot refresh the same token at once.

The collector itself permits only `auth status` and `quote`. Order, account,
position, portfolio, balance, and trading commands remain blocked in code.

Do not add `LONGBRIDGE_APP_KEY`, `LONGBRIDGE_APP_SECRET`, or
`LONGBRIDGE_ACCESS_TOKEN`; this workflow uses OAuth 2.0, not the legacy API-key
credential path.

## Public repository note

GitHub may disable scheduled workflows in a public repository after 60 days
without repository activity. Manual runs remain available from the Actions tab,
and making the repository private avoids exposing the source and Actions
artifacts to the public.
