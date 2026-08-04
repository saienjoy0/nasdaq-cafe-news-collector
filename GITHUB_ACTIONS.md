# GitHub Actions

`.github/workflows/daily-nasdaq-cafe.yml` runs the collector automatically at
07:00 Japan time every day. It can also be started manually from the Actions
tab with `today` or a specific `YYYY-MM-DD` date.

Each run uploads `output/YYYY-MM-DD/` as an artifact. The repository does not
commit daily outputs, article full text, or local credentials.

## Repository secrets

Add the following optional secrets in **Settings → Secrets and variables →
Actions**:

- `FRED_API_KEY`
- `FMP_API_KEY`
- `SEC_USER_AGENT`
- `SERPAPI_API_KEY`
- `TAVILY_API_KEY`
- `LONGBRIDGE_APP_KEY`
- `LONGBRIDGE_APP_SECRET`
- `LONGBRIDGE_ACCESS_TOKEN`

Missing keys are recorded in `missing_data` and do not stop the collection.
