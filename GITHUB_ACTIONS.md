# GitHub Actions

`.github/workflows/daily-nasdaq-cafe.yml` runs the collector every day at
07:17 Japan time. It can also be started manually from the Actions tab with
`today` or a specific `YYYY-MM-DD` date.

Each run uploads only the safe handoff files below for 14 days:

- `source_pack.md`
- `source_pack.json`
- `prompt_input.md`
- `CHATGPT_HANDOFF_YYYY-MM-DD.md`

Raw article HTML/PDF/text, full-text handoff files, local caches, and
credentials are intentionally excluded from artifacts and commits.

## Recommended configuration

In **Settings -> Secrets and variables -> Actions**, add these repository
secrets in priority order:

1. `TAVILY_API_KEY` - search and article extraction fallback
2. `FRED_API_KEY` - US interest-rate and macroeconomic data
3. `SERPAPI_API_KEY` - supplementary web/news search (optional)
4. `FMP_API_KEY` - supplementary market/company news (optional)

Add `SEC_USER_AGENT` as a repository **variable**, not a secret. It is not an
API key; use an identifiable contact value such as:

```text
nasdaq-cafe/1.0 your-email@example.com
```

Do not add `LONGBRIDGE_APP_KEY`, `LONGBRIDGE_APP_SECRET`, or
`LONGBRIDGE_ACCESS_TOKEN`. The current GitHub-hosted workflow has neither the
Longbridge CLI nor an enabled SDK fetch path, so those credentials would not be
used.

Missing optional keys are recorded in `missing_data` and do not stop the run.

## Public repository note

GitHub may disable scheduled workflows in a public repository after 60 days
without repository activity. Manual runs remain available from the Actions
tab, and making the repository private avoids exposing the source and Actions
artifacts to the public.
