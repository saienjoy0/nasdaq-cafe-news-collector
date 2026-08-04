# 朝のNASDAQカフェ 情報収集基盤

市場データ、マクロ、ニュースURL、記事原文を収集し、ChatGPTへ渡す根拠パックと監査可能なRaw Archiveを生成します。Codex/Python側ではニュース要約、市場解釈、値動き理由の断定、YouTube台本、投資助言を作成しません。

## 日次実行

```powershell
python -m nasdaq_cafe.run collect --date today
```

従来のコマンドも互換です。

```powershell
python -m nasdaq_cafe.run --date today
```

同じ日付のraw cacheを再取得する場合だけ `--refresh` を付けます。

```powershell
python -m nasdaq_cafe.run collect --date 2026-07-10 --refresh
```

個別URLと失敗再試行:

```powershell
python -m nasdaq_cafe.run fetch-url "https://example.com/article" --date 2026-07-10
python -m nasdaq_cafe.run retry-failed --date 2026-07-10
```

## Raw Archive

日付ごとに次を生成します。

```text
output/YYYY-MM-DD/raw/manifest.json
output/YYYY-MM-DD/raw/article_fulltext.json
output/YYYY-MM-DD/raw/articles/<document_id>.html
output/YYYY-MM-DD/raw/articles/<document_id>.pdf
output/YYYY-MM-DD/raw/articles/<document_id>.txt
output/YYYY-MM-DD/raw/articles/<document_id>.json
```

collectorが発見した記事URLは、関連度・review priority・handoff選定・GDELTのaccepted/rejectedに関係なくmanifestへ登録し、原則として全URLへ全文取得を試行します。成功本文は要約・抜粋・短縮せず保存します。失敗、block、paywall、抽出失敗もmanifestへ残します。

明示的な通信上限を設定した場合だけ取得件数を制限できます。上限を超えたURLは削除されず、`fulltext_status: not_attempted_limit` として残ります。

## 出力

通常出力:

```text
output/YYYY-MM-DD/source_pack.md
output/YYYY-MM-DD/source_pack.json
output/YYYY-MM-DD/prompt_input.md
output/YYYY-MM-DD/CHATGPT_HANDOFF_YYYY-MM-DD.md
```

全文入りの手動ChatGPT投入用:

```text
output/YYYY-MM-DD/CHATGPT_FULLTEXT_HANDOFF_YYYY-MM-DD.md
output/latest/chatgpt_fulltext_handoff.md
```

通常出力、RSS/Search raw、READMEには記事本文をコピーしません。

## 設定

`.env.example` を `.env` にコピーし、必要なkeyだけ設定します。key値をログや成果物へ表示しないでください。

- `FMP_API_KEY` が未設定の場合、FMPは `skipped` をrawへ保存して継続します。
- `SEC_USER_AGENT` が未設定の場合、SEC EDGARへは接続せず `skipped` を保存します。

Raw Archiveの主な保護設定:

- `NASDAQ_CAFE_MAX_FULLTEXT_ATTEMPTS=0`: direct URL取得件数。`0`は無制限。
- `NASDAQ_CAFE_FULLTEXT_WORKERS=6`: 並列取得数。
- `NASDAQ_CAFE_REQUEST_TIMEOUT_SECONDS=20`: URLごとのread timeout。
- `NASDAQ_CAFE_MAX_ARTICLE_BYTES=5000000`: 記事ごとの最大受信サイズ。
- `NASDAQ_CAFE_TAVILY_EXTRACT_BASIC_LIMIT=20`: basic fallback上限。
- `NASDAQ_CAFE_TAVILY_EXTRACT_ADVANCED_LIMIT=5`: advanced fallback上限。
- `NASDAQ_CAFE_FULLTEXT_DISALLOWED_DOMAINS=`: 明示的に取得禁止とするdomainのカンマ区切り。

## RSS取得経路

1. `vendor/finance-news-aggregator`
2. feedparser
3. requests + ElementTree

vendor repositoryは通常実行で更新せず、commit hashとfeedごとの経路を `raw/rss_news.json` に記録します。

## 禁止事項

- paywall、login、CAPTCHAの回避
- OpenAI API、n8n
- Trade API、Order API、Positions、Account balance、Portfolio、自動売買
- Codex/PythonによるYouTube台本、要約、投資推奨の生成
