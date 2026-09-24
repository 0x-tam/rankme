# Backlink research and verification

RankMe can use Codex web research to discover up to eight relevant editorial prospects per run. It then fetches each source through its public-only crawler, checks that the supporting excerpt actually appears on the page, and produces an outreach draft. It never sends messages, submits forms, buys links, or arranges exchanges.

`discover_prospects(runner, client, progress=None)` returns a list containing `id`, `source_url`, `name`, `reason`, `contact_url`, `draft`, `status`, `evidence`, `verification`, and `checked_at`. Status is `prospect` when the supporting excerpt is verified, `needs_review` when it cannot be confirmed, or `live` when the source already links to the client. Failed or unsafe source pages are omitted. Results are deduplicated and capped at eight. A contact URL is retained only when the fetched source links to it or is itself the contact page.

`evidence.verified` confirms that the short quoted text exists in the fetched page. It does not establish that the publisher accepts submissions, will reply, endorses the client, or will place a link. Suitability and outreach wording remain proposed AI judgments. Review a draft before sending it yourself.

`verify_backlink(source_url, target_url)` returns `status` (`live`, `missing`, or `error`), `live`, source and target URLs, `target_host`, matching `links`, `checked_at`, and a message. It requires an external HTTP 200 HTML page and an actual anchor href pointing to the target host. A textual domain mention does not count. `www` and non-`www` hosts are equivalent; arbitrary subdomains and lookalike suffixes are not. Each matching link includes `url`, `anchor`, `rel`, `nofollow`, `sponsored`, and `ugc`.

A live link may carry `nofollow`, `sponsored`, or `ugc`. The result does not predict ranking value or establish Google indexing. Verification reads fetched HTML and does not execute JavaScript, so links inserted only by browser scripts may not be detected. A timeout, blocked page, unsafe redirect, non-HTML response, or unavailable source reports an error instead of inventing evidence.
