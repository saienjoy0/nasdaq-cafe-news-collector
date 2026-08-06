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
5. `LONGBRIDGE_CLI_AUTH_B64` - Base64 of a ZIP archive containing the Longbridge `openapi` OAuth directory
6. `LONGBRIDGE_SECRET_ROTATOR_TOKEN` - fine-grained PAT limited to this repository with `Secrets: Read and write`

Add `SEC_USER_AGENT` as a repository **variable**, not a secret. It is not an
API key; use an identifiable contact value such as:

```text
nasdaq-cafe/1.0 your-email@example.com
```

Missing optional data-source keys are recorded in `missing_data`. If
`LONGBRIDGE_CLI_AUTH_B64` is absent, the run continues and records Longbridge as
missing. If a configured Longbridge OAuth session is invalid, bound to a live
account, lacks the US OpenAPI quote package, or cannot reach an OpenAPI endpoint,
the run fails before collection.

## Longbridge OAuth setup

The workflow uses the official Longbridge CLI and OAuth session created by:

```powershell
longbridge auth login
longbridge auth status --format json
longbridge check --format json
```

The approved account channel is `lb_papertrading`. The workflow rejects a live
account token before any market-data collection. The collector itself only
allows the Longbridge commands `auth status` and `quote`; order, account,
position, portfolio, and trade commands remain blocked.

The CLI stores the actual OAuth token below the `tokens/<client_id>` directory.
The whole `openapi` directory must therefore be archived; the `cli-auth` marker
alone is not sufficient.

On Windows, create the repository secret without printing the OAuth data:

```powershell
$source = "$env:USERPROFILE\.longbridge\openapi"
$zip = "$env:TEMP\longbridge-openapi-auth.zip"

if (-not (Test-Path "$source\tokens")) {
    throw "Longbridge token directory was not found: $source\tokens"
}
if (Test-Path $zip) {
    Remove-Item $zip -Force
}

tar.exe -a -c -f $zip -C $source .
$encoded = [Convert]::ToBase64String([System.IO.File]::ReadAllBytes($zip))
Set-Clipboard -Value $encoded
Remove-Item $zip -Force
Write-Host "Longbridge OAuth archive was copied to the clipboard."
```

Store the clipboard value as `LONGBRIDGE_CLI_AUTH_B64`.

The scheduled runner restores the archive to `$HOME/.longbridge/openapi`,
requires at least one file under `tokens/<client_id>`, validates the token and
paper account, and executes quote-only collection. It hashes the complete OAuth
directory before and after collection. If Longbridge refreshes or rewrites the
token files, the workflow creates a new ZIP archive and updates
`LONGBRIDGE_CLI_AUTH_B64` using `LONGBRIDGE_SECRET_ROTATOR_TOKEN`.
Secret values are never uploaded as artifacts or committed.

Do not add `LONGBRIDGE_APP_KEY`, `LONGBRIDGE_APP_SECRET`, or
`LONGBRIDGE_ACCESS_TOKEN`; this workflow uses OAuth, not the legacy API-key
credential path.

## Public repository note

GitHub may disable scheduled workflows in a public repository after 60 days
without repository activity. Manual runs remain available from the Actions tab,
and making the repository private avoids exposing the source and Actions
artifacts to the public.
