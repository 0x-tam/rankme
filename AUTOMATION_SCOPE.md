# RankMe visibility automation — integration scope

Status: the existing application has been retrieved from https://github.com/0x-tam/rankme and extended on `codex/visibility-automation`. This remains the full target scope, not a claim that every integration is operational. See [VISIBILITY.md](VISIBILITY.md) for implemented behavior, setup, tested boundaries, and remaining work, and [SECURITY.md](SECURITY.md) for the security model.

Latest user scope change: skip outreach. The original numbered list below is retained for traceability, but contact research, outreach drafts/sending, reporter/listicle pitches and link-exchange campaigns are excluded. Backlink monitoring, lost-link detection and competitor/source analysis remain included.

## Product contract

Preserve the existing add-website flow. A website URL starts public discovery, crawling, business inference, competitor discovery, and an initial evidence-backed opportunity queue. Connect private analytics, publishing, data providers, and a sending account once to enable the corresponding ongoing workflows. Expose connection failures and unavailable measurements honestly.

The recurring loop is research → cluster → brief → produce → validate → publish → measure → refresh → expand. Production requires persistent workers and scheduling independent of an open chat session. Jobs must have retries, idempotency, dependency tracking, per-site budgets, and inspectable execution history.

## Requested capability coverage

| # | Capability | Proposed automated behavior |
|---|---|---|
| 1 | Keyword research | Combine customer questions, reviews, forums, first-party search queries, and licensed search data; classify intent and retain evidence. |
| 2 | Search Console | Authorized property connection, incremental query/page imports, historical baselines, and actionable performance changes. |
| 3 | AI mentions | Repeat defined buyer-intent prompts across supported provider integrations; record model, date, locale, responses, and brand mentions. Distinguish API observations from consumer-app results. |
| 4 | Citation gaps | Compare cited sources and competitors with site coverage; preserve citation URLs and supporting response evidence. |
| 5 | Opportunity scoring | Explainable prioritization using business fit, intent, demand, achievable impact, confidence, effort, and cost. Missing data remains missing. |
| 6 | Topical authority | Topic/entity coverage maps, pillar pages, supporting clusters, and coverage gaps. Treat the score as an internal heuristic. |
| 7 | Content briefs | Evidence, audience, intent, page type, outline, product facts, differentiation, source requirements, and linking plan. |
| 8 | LLM structure | Direct answers, descriptive headings, useful tables, cited facts, entity clarity, and appropriate structured data matching visible content. |
| 9 | Internal links | Build the site link graph; suggest or apply contextual links; validate destinations and detect orphaned pages. |
| 10 | Cannibalization | Flag overlapping intent and competing query/page pairs; distinguish useful coverage from actual conflict before consolidation. |
| 11 | Content refresh | Prioritize declining traffic, near-winning pages, high impressions/low clicks, stale facts, and changed intent. |
| 12 | Technical audits | Crawl status codes, redirects, canonical tags, indexability, sitemaps, metadata, structured data, and available performance signals; repair through supported publishing adapters. |
| 13 | Link outreach | Relevant prospect discovery, deduplication, evidence-based pitches, bounded follow-ups, and outcome tracking. Sending requires configured campaign authorization. |
| 14 | Link exchange | Track relevant editorial partnerships; do not run indiscriminate reciprocal-link schemes. |
| 15 | SEO reporting | Report search performance, sampled AI visibility, content changes, links, conversions when connected, cost, and unresolved blockers. |
| 16 | Lost links | Compare backlink snapshots, verify losses, diagnose changed targets, and prepare reclamation actions. |
| 17 | Unlinked mentions | Verify brand references and missing links, qualify publishers, and prepare relevant requests. |
| 18 | Free tools | Identify useful calculators/checkers/templates; build and test tools within supported site integration capabilities. |
| 19 | Listicle outreach | Discover relevant lists, verify fit and omissions, and prepare factual inclusion pitches. |
| 20 | Competitor links | Analyze competitor referring pages to discover editorial opportunities; independently qualify each prospect. |
| 21 | Broken link building | Verify dead references, find a genuinely suitable replacement, and prepare a contextual pitch. |
| 22 | Outdated guides | Detect stale owned guides for refresh and relevant external guides for correction outreach. |
| 23 | Statistics pages | Publish traceable figures with sources, dates, methodology, and a refresh schedule; never fabricate statistics. |
| 24 | Reporter outreach | Match accessible reporter requests to verified expertise; prepare sourced responses without invented quotes or credentials. |
| 25 | Infographics | Generate accessible graphics from validated information, with citations and accompanying HTML text. |
| 26 | Links | Maintain a unified internal/external link inventory with provenance, status history, attributes, and prospect outcomes. |

## Content and distribution

Mine repeated problems by frequency, purchase intent, and product relevance. Choose page types based on the query. Map cluster links before publishing. Use verified product facts, original examples, screenshots, and supported comparisons. Refresh existing opportunities alongside new content. Repurpose validated research into appropriate landing pages, support content, social drafts, and video scripts; external distribution depends on connected channels and configured permissions.

## Integration architecture to adapt to the existing stack

- Connectors: crawler, search/keyword data, Search Console, AI answer providers, backlink/mention data, CMS or repository publishing, analytics, and outreach mail.
- Shared records: sites, connections, source evidence, crawl snapshots, query observations, AI runs/citations, topics, opportunities, briefs, content versions, link observations, prospects, campaigns, jobs, and reports.
- Execution: durable per-site queue, concurrency limits, retry/backoff, budget caps, scheduled refresh, and isolation between customer sites.
- Publishing: supported adapters, preview/version history, quality validation, remote verification, and rollback. Avoid overwriting concurrent manual changes.
- Outreach: explicit sender/campaign configuration, contact deduplication, suppression and opt-out handling, sending limits, and stop-on-reply behavior. No outreach is sent as part of this scoping step.
- Input handling: public-site URL validation, SSRF protection including redirect/DNS checks, bounded crawl scope, and untrusted-source isolation from agent instructions.
- Secrets: encrypted storage and server-side use; never expose tokens to frontend logs or content generation.

## Definition of done

1. Adding a real website creates persisted discovery jobs and evidence-backed opportunities.
2. Authorized connections supply actual observations; missing or failed providers show a clear blocked/degraded status.
3. A supported publishing integration can create and refresh a real page, verify its remote state, and restore its prior version.
4. Internal-link edits avoid broken targets; overlapping content is detected before publication.
5. Scheduled work resumes after process restarts without duplicate publishing or sending.
6. Authorized outreach can be tested with a controlled recipient and cannot duplicate sends or contact suppressed recipients.
7. Reports link metrics and recommendations to timestamped evidence and distinguish observations from estimates.
8. End-to-end tests cover job retries, tenant boundaries, credential failures, unsafe URLs, budgets, publication conflicts, and rollback.

## Outcome boundaries

RankMe can automate the work and measure outcomes. It cannot guarantee rankings, indexing, AI citations, earned links, publisher replies, or sales. Third-party API coverage, paid data, and hosting costs require explicit configuration. Search Console access requires OAuth authorization; a public URL alone is insufficient.

References checked: https://developers.google.com/webmaster-tools/v1/how-tos/authorizing and https://developers.google.com/search/docs/essentials/spam-policies . Google identifies excessive link exchanges and scaled low-value content as spam risks, so automation must optimize usefulness rather than volume alone.
