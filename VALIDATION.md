# RankMe validation

## Live subscription integration — 22 September 2026

The research workflow ran against public RankPill pages using the existing ChatGPT-authenticated Codex CLI, version `codex-cli 0.145.0`. No OpenAI API key or API fallback was used. Codex selected its default model; the model identity was not recorded and is not inferred here. These were explicitly isolated test jobs, not production client records.

The completed sequence was:

1. Crawl three public pages: the homepage, pricing page, and a service-industry page. The crawler checked robots and bounded sitemap discovery. This limited crawl was chosen for integration testing; it is not an exhaustive company audit.
2. Generate a strict-schema business profile with 15 source-backed facts, 8 subject suggestions, and explicit unknowns distinguishing first-party positioning from independently verified performance.
3. Generate exactly 12 article briefs about “Choosing automated SEO software.”
4. Research and write “How to Choose Automated SEO Software: A Practical Buyer’s Guide.” The first draft passed structural checks: 1,284 words, 8 sources, and 9 claim-ledger entries.
5. Run independent review. The first review scored 96 but mixed an editorial correction, positive observations, and optional enhancements into its issues list. The conservative publication gate correctly held the draft.
6. Revise once. The revised article passed structural checks: 1,065 words, 8 sources, and 8 claim-ledger entries. The older review prompt again returned optional enhancements as issues despite finding no unresolved material factual problems.
7. Fix the review contract: only required publication corrections belong in `issues`; optional observations belong in `summary`. Add a deterministic requirement for a completed Codex `web_search` event in planning, drafting, and review jobs.
8. Re-review the revised article with the corrected production prompt and recorded-web-research gate. Final result: **passed, 94/100, no issues**. The review reported that all eight cited URLs resolved and materially supported the associated claims.

The final demonstration files are in [examples/live-check](examples/live-check/README.md). No raw CLI logs or private client data are included.

## Safety and reliability checks

The complete Python test suite passed: **66 tests** on 22 September 2026, including 18 research-specific tests.

Research tests cover public-address DNS validation, DNS-pinned connections, private and external redirects, robots restrictions, sitemap discovery, Unicode paths, response bounds, strict schemas, subscription-rate errors, API-key environment removal, private client configuration filtering, bounded article history, and recorded web-research requirements. The crawler uses size limits and request/crawl time budgets; it tries a bounded set of validated public addresses to handle unavailable IPv6 routes.

Browser checks also verified the empty dashboard, profile confirmation, suggested subject selection, queued plan, article preview, saved edits, review invalidation, Markdown tables, and disabled generation when no planned articles remain. The test browser reported no console errors. These checks used isolated test data.

The launcher passed real start, existing-session reuse, status, stop, and restart checks. The finished server is running on localhost. A second server using the same data directory is rejected by an exclusive process lock.

## Scope and limitations

- No demonstration article was published. No client repository, hosting account, or production website was changed.
- This test proves one complete research-to-reviewed-draft run. It does not prove every website, deployment platform, subject, or future model output will work.
- Company inspection reads public HTML. JavaScript-only, authenticated, inaccessible, and uncrawled pages remain unknown.
- The app must run with ordinary network access and access to the user's Codex session state. This development sandbox required tool-level escalation for those operations.
- AI review is fallible. The demonstrated article passed the implemented structural and evidence-review gates; factual perfection and search performance are not guaranteed.
- Live publishing requires a configured, authorized client repository and deployment connection. That connection must be validated separately for the real website.
- Subscription usage limits apply. Scheduled work requires the local machine and RankMe process to be running.

## Cover image extension — 22 September 2026

The updated automated suite passes 98 tests. A real cover for Lumident's existing numbness-after-treatment article was generated through the ChatGPT-authenticated Codex CLI's built-in image tool and independently inspected using image input. The final 1536 × 1024 PNG passed visual review without issues and was also viewed manually. The image is attached to the unpublished local article. No deployment occurred.

The palette was extracted from Lumident's public CSS semantic tokens: primary #CA2126, secondary #636569, warm accent #E1DCD6, and dark neutral #18181B. The generated still life uses restrained photographic styling and subtle accents. Image generation quality and brand matching remain subject to model variation; review is a quality gate, not a guarantee.

Tests cover image signatures/dimensions, file bounds and source restrictions, visual-review repair and hold, stale article/brand bindings, cover-only retry, protected image delivery, dual-file Git publication, rollback, recovery, and safe frontmatter/JSON image references.

## 3D art direction correction

The user selected website-matched 3D illustration instead of photographic still life. The live website's Adults & Cosmetics service icons were inspected: ivory tooth forms, rounded red elements, warm blush/ivory surfaces, and soft shadows. The client visual direction now records those observations. Generation and visual review require 3D imagery and incorporate the business audience, brand voice, and article intent. Audience and tone changes invalidate the cover binding.

A replacement 1536×1024 cover for the numbness article passed independent visual review and manual inspection, and was attached locally. The original file remains in version history. No publication occurred. Updated suite: 99 tests passed.

## SEO data and backlink integration — 2026-09-22

- Full offline suite: 140 tests passed; JavaScript syntax check passed.
- OAuth tests cover PKCE/state, callback origin exception, exact loopback Host, normal API CSRF defenses, secret-file permissions, safe errors, refresh/revocation and partial scopes.
- Data tests cover bounded report pagination, distinct site totals, Organic Search filtering, preserving reports after total sync failure, daily retry throttling, pause behavior and separate weekly link monitoring.
- Backlink tests cover real href detection, rel attributes, source evidence, URL safety, deduplication, job ownership and preserving history after a live link disappears.
- Browser inspected clean empty state, populated metrics and backlink review using an isolated labeled fixture on port 8791. Fixture server stopped; no fixture metrics entered in client data.
- Production Lumident discovery completed through signed-in Codex: six prospects saved, five source excerpts verified and one flagged for review. No matching backlinks observed at those fetched pages; no outreach sent. Drafts use operator-role placeholders and conditional clinical-review language.
- Google credentials have not been supplied. Real OAuth consent and private Search Console/GA4 retrieval remain untested until the user completes GOOGLE-SETUP.md. No Google metrics are invented or shown as connected.
