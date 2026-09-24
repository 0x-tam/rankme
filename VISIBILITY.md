# Visibility workflows

RankMe now connects its existing website inspection, Google metrics, content pipeline, publishing adapter, and backlink monitoring to a persistent opportunity queue. Open a website's **Visibility** tab to see the evidence, prepare work, and configure recurring runs. It is still a local single-user application, not a hosted multi-tenant service.

**Scope update:** outreach is excluded by request. Visibility automation does not research contacts, create outreach drafts, send messages, or run link-exchange campaigns. Link monitoring, lost-link detection, competitor links, and source analysis remain in scope. The pre-existing manual backlink workspace remains available; no new sending workflow is being added.

## What runs

An initial website inspection builds a report as soon as crawl evidence is available, even before the AI business profile finishes. The report is rebuilt after inspection, successful Google sync, backlink discovery, and backlink checks. Repeated runs preserve dismissed and completed opportunities. Opportunities no longer observed become resolved; previously resolved issues can reopen if seen again. Scores are prioritization heuristics, not forecasts or search-engine authority scores.

The report covers observed metadata/status/robots/canonical/heading issues, sampled internal links and orphan candidates, title-based topic clusters, query opportunities, actual query/page overlap when available, comparable-period click declines, and known lost links. Crawl coverage and missing data are visible. Similar titles or overlapping queries alone are not proof of harmful cannibalization.

**Research opportunities** uses the existing ChatGPT-authenticated Codex runner for bounded public research into customer questions, competitor weaknesses, comparisons, statistics pages, free tools, infographics, outdated guides, unlinked mentions, broken references, competitor referring pages, and citation sources. At most eight findings survive each run. Each must have a short exact quotation verified on a public page. The quotation establishes provenance; relevance, factual interpretation, and broken-link status still require further checking.

**Sample AI answers** asks up to three distinct questions drawn from article keywords, available search queries, and the selected subject. The target brand/profile is not injected into the answer prompt. RankMe measures brand mentions in the actual returned answer and checks returned citation URLs using its protected public fetcher. External cited sources can become citation-gap review opportunities. These are actual **Codex web-research samples**, not measurements of consumer ChatGPT, Claude, Gemini, or Perplexity. Source availability is not verification that every answer claim is supported. Queries containing a brand naturally affect mention results; inspect the recorded prompt before interpreting a sample.

## Turning opportunities into work

- Content opportunities create an evidence-backed planned article, which enters the existing research, review, cover generation, and publishing workflow.
- A decline for a currently published RankMe-owned page can create a separate refresh draft. Original records remain intact. The publisher preserves its publication identity and URL and requires the original file's bytes and configured connection to match the captured baseline. Manual edits, changed connections, missing files, stale competing drafts, and unsafe paths block replacement. Failed builds restore the previous file.
- Other opportunities create inspectable action briefs. A completed opportunity means its brief/draft was prepared; it does **not** mean a technical fix was deployed, outreach was sent, or a backlink was won. Prepared work links to its article or brief.
- Per-client JSON reports include current opportunity dispositions and prepared briefs. All records remain in the local SQLite database and normal JSON backup.

Automatic article production continues to use the existing confirmed profile, chosen subject, weekly schedule, and reviewed publishing connection. Enabling automatic brief creation does not independently authorize deployment. Only articles that pass the existing text, source, cover, and publishing checks may publish through a connection with automatic publishing enabled.

## Recurring controls

### Business goals and durable evidence

In Visibility, choose **Set conversion goal** and enter the exact GA4 event name and same-site landing URL. The goal type is a business label (signup, purchase, booking, or lead); it does not install tracking or mark an event as a GA4 key event. Authorize Google once, choose its properties, and sync. RankMe cannot infer a reliable business event from the public website URL alone.

The connector reads Organic Search sessions and occurrences of the selected event using separate requests so event filtering does not shrink the session denominator. Both are restricted to the exact landing path/query and hostname. Trailing slashes, query strings, and www/non-www hosts remain distinct. Events on other checkout domains are excluded. These are event occurrences, not unique customers, purchases verified against a payment system, revenue, or a query-level conversion attribution. A measured zero does not prove that instrumentation is working. GA4 sampling, thresholding, or row-loss metadata prevents experiment conclusions.

An exact goal-page opportunity gets a modest explained priority adjustment when recent selected-event evidence is available. Other pages receive no inferred conversion score. The score remains a heuristic, not an ROI forecast. New automated article briefs use the configured goal destination for their CTA.

Successful Google syncs, source research, and AI answer samples are saved transactionally with permanent snapshots. Goal changes, experiment transitions, publication attempts, and outcomes are also retained. History is append-only through the application, survives restarts, is included in normal backups, and is never trimmed with the short activity feed. Existing installations preserve the latest available old snapshot; older overwritten data cannot be recovered. The interface loads the latest 20 observations per website; **Download full history** and the report retain all entries. The on-disk database grows with retained evidence and should be backed up. AI questions and source availability can differ between runs; the history alone does not make samples comparable across providers or prompts.

### Controlled page changes

Use **Track a page change** immediately after making one external change today to the configured goal page. This records an observational experiment; it does not edit the site. A verified RankMe-owned refresh of the configured goal page starts observation automatically when a valid pre-publication baseline was captured. Exporting a draft or awaiting verification does not claim a live experiment. Delayed verification uses the captured baseline, not newer post-publication metrics. If evidence is unavailable, the change is recorded as unmeasured.

Only one observing experiment may exist for a client/page. While it runs, RankMe blocks another refresh of that page, including an already-prepared draft, and locks its goal/Google-property settings. Cancel tracking explicitly to change these settings; evidence remains in history. Other pages can continue. Changes made outside RankMe are not detectable automatically and can confound the result.

The baseline must end within seven days before the change. Google currently supplies equal 28-day windows ending three days ago. Evaluation excludes the change day in the GA4 property timezone; when timezone metadata is unavailable it also excludes the following UTC day. Conflicting property timezones prevent a comparison. Evaluation waits for a wholly post-change window, adequate organic sessions in both windows (100 by default), and baseline event occurrences (five by default). The minimum observation setting defaults to 14 days, but the current connector still requires its complete 28-day window. A descriptive event-per-session change of at least 20% is labelled improved or declined. Smaller changes, low samples, stale/missing metrics, and incompatible evidence remain observing until the 90-day horizon, then inconclusive at evaluation. This is not a randomized test or a significance test. Seasonality, tracking changes, and unrelated edits can explain a difference; no automatic rollback is inferred from the label.

UI settings use conservative defaults. The API also accepts bounded `minimum_days` (7–28), `min_sessions` (10–1,000,000), and `min_conversions` (1–100,000) with the goal. Goal edits reset omitted thresholds to defaults and require no active experiment. Evaluation occurs on successful Google syncs while RankMe runs; disconnected tracking cannot produce an outcome.

New clients receive weekly public auditing by default. Automatic research, answer sampling, new briefs, and refresh drafts are individually disabled until configured. Each visibility cycle permits one follow-up action by default, configurable from one to five; the interval is one to thirty days. Research (one run, up to eight findings) and answer sampling (one run, up to three questions) have separate interval controls and consume the shared Codex subscription allowance. These limits are not a dollar-denominated budget.

Failed attempts record their time so the scheduler does not retry on every tick. Failed actions count against the cycle cap. A manual retry retains the opportunity identity. Article/task/opportunity completion writes are transactional, avoiding duplicate drafts after an interrupted job. Failed external publication is never blindly repeated beyond the existing publishing checkpoint rules.

Global pause prevents queue starts. Per-client pause prevents new visibility scheduling. Disabling visibility stops scheduled visibility work from starting; an already running request can finish. The local server must be running and the computer awake. No cloud worker, login service, or 24/7 uptime is provisioned by this change.

## Connections and remaining scope

| Area | Available now | Remaining dependency/work |
|---|---|---|
| Keyword/customer research | Observed site queries and source-checked public research | Licensed market-wide volumes, difficulty, and SERP data |
| Search Console/GA4 | Existing OAuth integration plus true joint query/page rows | One-time Google authorization and property selection |
| AI visibility/citation gaps | Actual recorded Codex answer samples | Separate platform integrations and repeated comparable samples for broader measurement |
| Content production | Briefs, existing reviewed article pipeline, protected owned-page refresh | Publishing connection, approved business facts, compatible renderer |
| Technical fixes/internal links | Audits, candidate links, and action briefs; article generation already includes contextual links | General repository/CMS patch adapters with validation and rollback for arbitrary existing pages |
| Link analysis | Known-page checks, loss history, public competitor/source research | Full backlink/mention provider coverage |
| Outreach and link exchange | Excluded by request | No implementation planned in this scope |
| Free tools/statistics/infographics | Research and briefs; statistics topics can enter article production | Tool-building and standalone infographic production adapters, evidence/data review |
| Distribution | Research can inform further assets | Connected social/video channels and distribution workers |
| SaaS hosting | Local application only | Authentication, tenant isolation, encrypted secret service, billing, durable hosted workers, security review |

All 26 requested capabilities appear in the interface with their actual status. RankMe does not claim unsupported measurements or successful external actions. No sending account is connected, no outreach is sent, and no paid data provider is purchased by this change.

## Validation

Run `python3 -m unittest discover -s tests -v` and `node --check static/app.js`. Tests use temporary local stores/repos and controlled network/AI boundaries. They exercise persistence/restarts, budgets, pause/retry behavior, site ownership, source rejection, real query/page data shapes, probe isolation, safe refresh/rollback, SSRF boundaries, credentials, and existing workflows. A passing offline suite does not establish live Google/Codex availability or compatibility with every client website; calibrate a connected site before unattended publication.

Run `node tests/ui_measurement_smoke.js` for conversion/history/experiment rendering checks, including unavailable data and hostile strings. Measurement regression tests additionally cover immutable snapshots, retry consistency, property/goal isolation, delayed verification, one-change guards, equal periods, sample thresholds, and missing-versus-zero evidence.
