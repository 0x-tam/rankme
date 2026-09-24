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
