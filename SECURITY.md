# Security model

RankMe is designed for one trusted user on a local computer. It binds to loopback and must not be exposed through a public proxy or port forwarding. There is no multi-user authentication or cloud tenant boundary. A website record's ownership checks help prevent cross-site workflow mistakes; they do not make this application a multi-tenant SaaS.

## Enforced boundaries

- HTTP mutations require the current random session token. Host, Origin, and fetch-site checks reject cross-origin access; restrictive CSP and output escaping protect the local UI. The OAuth callback has a narrow exception with one-use state and PKCE.
- Public fetches validate URLs before parsing, allow HTTP(S) public destinations only, validate all resolved addresses, connect to the validated address, verify TLS using the original hostname, and repeat validation after redirects. Private, loopback, multicast and unsafe IPv6 transition destinations are rejected. Response size, redirects, page count, and crawl duration are bounded.
- Crawling respects the existing robots policy. Fetched pages, research text, model responses, and article content are untrusted data. They cannot change configured publishing commands, accounts, scheduling limits, or credentials.
- AI child processes receive a limited environment instead of inherited cloud, GitHub, CMS, SSH-agent or API credentials. Prompts contain selected business/content fields, not publishing configuration. Jobs retain existing ephemeral/read-only execution, disabled shell tools, strict structured output, source checks and independent editorial review.
- Google token requests use approved HTTPS hosts and ports; header-control characters are rejected. Credentials are outside database backups and AI prompts. Reads use a non-following file descriptor, reject symlinks, hardlinks, FIFOs and nonregular files, and enforce size limits. The existing local secret file uses restrictive permissions.
- Opportunity and refresh references are checked against the owning website. IDs include the website identity. Dependent work creation is transactional; queue operations are serialized within the local process.
- Publishing derives paths from configured projects, verifies project containment and ownership, rejects unsafe content and symbolic-link targets, validates builds, stages only relevant files, and preserves unrelated work. Safe refresh checks actual original bytes, settings, slug and publication identity, with backups and failure rollback. Entity-encoded and control-obfuscated unsafe Markdown schemes are rejected.

## What is still trusted or limited

Build/deployment commands are explicitly configured local executable argument arrays and run with the user's privileges. They are not a sandbox for malicious repositories. RankMe's read-only Codex sandbox is not a guarantee of filesystem confidentiality; strong isolation for hostile data or multi-user hosting requires a separate operating-system identity/container and scoped authentication environment.

Local SQLite content, reports, backups and the permission-protected Google secret file are not encrypted by RankMe. Protect the OS account and disk/backups. Local attackers already able to write application repositories or the data directory are outside the remote-site threat model. Some filesystem operations have local race limitations. DNS resolution uses the system resolver and is not governed by a strict application wall-clock deadline.

Exact quotations and reachable citations establish provenance, not truth. Prompt instructions and schema checks reduce risk but do not prove immunity to prompt injection. Publishing continues to require deterministic validation plus editorial/source review. Arbitrary external sites are not authorized to tell RankMe to send messages, share local files, change permissions, or execute commands.

No outreach transport, paid-link acquisition, broad link-exchange network, production cloud deployment, or permission expansion is installed by this change. Future external-action adapters need explicit scoped authorization, deduplication, campaign limits, cancellation/suppression, and auditable outcomes before unattended use.

## Relevant tests

`tests/test_security_hardening.py`, `tests/test_research.py`, `tests/test_oauth_http.py`, `tests/test_publisher.py`, `tests/test_refresh_publisher.py`, `tests/test_autopilot.py`, `tests/test_visibility_research.py`, and `tests/test_answer_probes.py` cover these boundaries with controlled fixtures. This is regression testing and code review, not an independent penetration-test certification.
