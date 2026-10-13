# Optional ChatSEO connection

RankMe's direct Google measurements, public website audits, AI Overview tracking, and evidence tools remain available independently. ChatSEO is an optional external provider, with its own account, plan, and credits. Its documented MCP access requires Pro or Ranker; the available capabilities are discovered from the connected account rather than assumed from the marketing catalog.

## Connect once on the worker Mac

1. Run the updated RankMe worker, open its local dashboard, and sign in with the owner's passkey.
2. Open a website's Visibility workspace and choose **Connect ChatSEO**. RankMe registers its own public OAuth client and opens ChatSEO's authorization screen. Sign in to the intended ChatSEO account and consent.
3. Return to RankMe, refresh ChatSEO sites and credits, and select the matching ChatSEO site. RankMe only accepts sites returned by that account. It does not create sites, connect Google on your behalf, or automatically switch organizations.
4. Use the hosted dashboard to read snapshots, refresh recommendations, manage opt-in settings, and submit requests through the authenticated local worker. Initial OAuth connection and reconnecting an expired grant must happen on the Mac. The hosted dashboard never receives a ChatSEO token.

This connection is separate from the Codex ChatSEO connector. Installing or signing into the Codex connector does not connect the deployed RankMe runtime. RankMe never extracts its credentials. No undocumented REST endpoints or manually pasted API keys are used.

Credentials live in the worker's private `chatseo-secrets.json`, mode 0600, outside the application database, backup payloads, cloud snapshots, browser, and prompts. OAuth uses PKCE S256, one-use state bound to the initiating owner session, and an exact loopback callback. Only fixed HTTPS ChatSEO endpoints are allowed; redirects are rejected. Disconnect deletes RankMe's local grants; remove application access in ChatSEO as well if remote revocation is desired.

RankMe identifies its HTTPS requests as `RankMe/1.0`; ChatSEO rejected the default Python client identifier during live registration on 2026-10-14. A successful registration is reused when connecting again. Connection errors identify application registration, sign-in completion, access renewal, or API access. A registration denial happens before account sign-in and does not establish a problem with your plan or organization. If a denial persists after retrying, share the failed step and HTTP status with ChatSEO support; never share the credential file or callback URL.

## Data and paid requests

Refresh reads the site list, credits, and the runtime tool catalog. Per-site sync reads recommendations and conversations only when a compatible site-filtered tool exists. Missing, rejected, or incompatible tools are shown as unavailable; RankMe does not invent success. Provider recommendations have a separate ChatSEO source and status and do not become local automated actions.

Paid requests start disabled. Enable them per website, set a daily request count (UTC, 1–10), and set the minimum remaining balance required before submission. Each request also needs an explicit acknowledgment of its unknown credit cost. These controls are **not a hard credit spending cap**: ChatSEO does not expose a per-request credit budget in the supported send-message contract. An external ChatSEO request can use the tools and integrations connected to that account; RankMe cannot enforce a provider-side read-only mode. Write the intended request accordingly. RankMe forwards that text exactly and does not add hidden instructions.

The request's durable ID is recorded before submission. Repeating the same request ID returns the existing run; changing its text is rejected. RankMe never automatically resends paid requests. A crash or timeout during submission becomes **uncertain** and blocks a new request for that website until the owner checks ChatSEO and explicitly acknowledges the outcome. This does not refund credits or remove the original request from the daily count. If a conversation ID was received, **Refresh response** reads the existing conversation and artifacts; it does not send another message.

Artifact import verifies the artifact against the recorded conversation and currently mapped site, fetches its content on the worker, and creates a held, unreviewed local article. Imports are deduplicated. Existing review, cover, and publication checks remain required; importing never publishes a page. No outreach, provider CMS publishing, account/site deletion, or arbitrary MCP tool route is exposed.

## Limits and verification

- Only the active ChatSEO organization is used. If it has no accessible sites, select the intended organization in ChatSEO and reconnect.
- Tool schemas and response shapes may differ by account or change over time. Incompatible operations fail closed and need integration maintenance.
- Responses are bounded to 2 MB with network/time limits. Shared snapshots retain up to 20 recent mapped runs/reports, truncate reply previews, and cap their combined serialized contribution; full responses remain in the private local database.
- Offline tests cover OAuth state/session/PKCE, credential file protections, protocol discovery, paid request accounting/deduplication/restart recovery, cloud command boundaries, site/artifact isolation, and draft-only imports. Authenticating a real account and submitting paid work require the owner's explicit actions; fixture tests do not prove those live flows.

Official references checked on 2026-10-14: [ChatSEO machine-readable guide](https://chatseo.app/llms-full.txt), [server card](https://chatseo.app/.well-known/mcp/server-card.json), [OAuth resource discovery](https://api.chatseo.app/.well-known/oauth-protected-resource/mcp), and [OpenID discovery](https://api.chatseo.app/.well-known/openid-configuration).
