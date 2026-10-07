# Google AI Overview observations

RankMe's default collector opens ordinary Google Search in a local, isolated Chrome profile and reads the visible AI Overview. It uses [Playwright for Python](https://playwright.dev/python/docs/browsers) from the pinned project requirements and an installed Google Chrome. It needs no API key or paid search service. Reinstall the project requirements to add Playwright; the browser collector does not download a browser because it uses the installed Chrome. It never opens Google AI Mode (`udm=50`).

The free browser route makes at most six searches per UTC day across clients. Each search is reserved in a persistent local ledger before Chrome starts, and a private file lock serializes use of its profile. It keeps Chrome's sandbox enabled, uses no CAPTCHA solver or proxy, and stops with `blocked` if Google presents a challenge or consent page. It does not ask users to log in or reuse their personal Chrome profile. The browser may still be blocked by Google, or Google may change the page structure; uncertain extraction is `error`, not an invented absence.

The browser route currently supports desktop English Google Search with no exact location setting. Country and language (`gl`/`hl`) are requested hints; the actual physical location can still affect results. A nonempty exact location, mobile/tablet device, or non-English language is reported as unsupported. The record states this locale limitation. A visible AI Overview heading and substantive bounded container text are required for `present`. An ordinary loaded results page with organic results and no overview marker is required for `absent`. Browser observed citations are filtered public HTTP(S) links in that visible overview container; they are not independently fetched or checked for factual support.

These records are separate from RankMe's Codex answer probes. Each observation stores the query, requested country, language, location, and device, the time, a present/absent/error/blocked status, normalized answer text, and citation provenance. Manual browser observations can also be imported with dated evidence and remain identified separately.

## Optional SerpApi route

RankMe can also observe Google Search AI Overviews through SerpApi. Provider observations store SerpApi search IDs. A missing overview is recorded as absent only after a successful Google Search response. SerpApi may require a second request using a short-lived `page_token`; RankMe makes at most two requests per sample.

The integration stays blocked until the owner creates `ai_overviews_config.json` in the **actual RankMe data directory** (the directory passed to `--data-dir`, or the repository's ignored `data` directory by default). The file must be a regular file owned by the current user, have exactly one hard link, and allow access only to that owner (`0600`). Its format is:

```json
{
  "provider": "serpapi",
  "api_key": "YOUR_SERPAPI_KEY",
  "daily_call_cap": 10,
  "enabled": true
}
```

Set the cap to an explicit maximum number of SerpApi calls per UTC day, including token follow-ups. RankMe reserves each call in a local SQLite ledger before sending it. A crash can use part of the cap, but cannot make an uncounted call. No API call is made while the config is absent, insecure, disabled, or exhausted. The key is never included in observation history, summaries, logs, cloud snapshots, or process arguments. Keep the data directory private and do not put the key in this repository.

SerpApi currently documents AI Overview availability mainly for English searches in a limited set of countries. A requested cohort outside available coverage can yield an absent result, which only describes that sampled search. RankMe requests fresh, synchronous JSON results; it does not scrape Google directly or try to bypass a challenge. Citations are URLs reported by SerpApi, filtered to public HTTP(S) URL shapes, and are **not** independently fetched or checked for factual support.

Summaries use the most recent present or absent observation per UTC day for an exact client/query/location/country/language/device/source cohort. Browser, provider, and manual records form separate series. Seven distinct days from one source are required before RankMe treats citation frequency, retention, changes, and common terms as history. Any editorial suggestion is labeled as an inference from observed text and citations. Citation provenance is `browser_observed`, `provider_reported`, or `manually_reported`.

Provider documentation: [Google Search AI Overview result format](https://serpapi.com/ai-overview) and [token follow-up API](https://serpapi.com/google-ai-overview-api).
