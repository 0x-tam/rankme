# Connect Search Console and Google Analytics

RankMe can read Search Console and GA4 data through your own Google account. This is separate from Codex: article writing and cover generation continue to use your ChatGPT subscription. Connecting Google does not enable an AI API billing fallback.

## One-time Google project setup

1. Open [Google Cloud Console](https://console.cloud.google.com/) and create or select a project you control.
2. Enable **Google Search Console API**, **Google Analytics Data API**, and **Google Analytics Admin API** in that project. The Admin API lists available GA4 properties; the Data API reads reports.
3. Configure the Google Auth Platform branding, audience, and contact information. For an ordinary personal Google account, choose **External**. Use **Internal** only when the app and users qualify under your Google Workspace organization.
4. Add these read-only scopes to the consent configuration:
   - `https://www.googleapis.com/auth/webmasters.readonly`
   - `https://www.googleapis.com/auth/analytics.readonly`
5. Create an OAuth client with application type **Desktop app**. Copy its client ID and, when provided, client secret into RankMe's Google connection settings. Do not choose a web, Android, or iOS client. Do not paste credentials into chats, source code, article fields, or client notes.
6. Start Google sign-in from RankMe while the local app is running. Complete consent in your normal browser. Google returns to the local loopback callback selected by RankMe. Return to RankMe after authorization.
7. Select the exact Search Console property and optional numeric GA4 property. Your Google account must already have access to those properties. A Search Console domain property looks like `sc-domain:example.com`; a URL-prefix property includes its exact protocol and prefix.

The desktop flow uses a local HTTP loopback callback, an expiring one-time state, and PKCE with S256. Google documents loopback callbacks for desktop applications in its [installed-app OAuth guide](https://developers.google.com/identity/protocols/oauth2/native-app). The callback is handled by the local RankMe server; there is no public callback server or copy-and-paste authorization-code flow.

## Testing versus ongoing personal use

For initial setup, an External app in **Testing** must list your Google account as a test user. With the Search Console and Analytics scopes, Google issues Testing refresh tokens that expire after **seven days**. This would require weekly reconnection and is unsuitable for unattended ongoing sync. The exemption for basic identity-only scopes does not cover these scopes. [Google's refresh-token expiration documentation](https://developers.google.com/identity/protocols/oauth2)

For ongoing personal use, review the project's audience settings and move the OAuth application to **In production/Published** when appropriate. Publishing OAuth configuration does not publish your local RankMe application or make its data public. Google's personal-use exception permits eligible apps with fewer than 100 users to operate without completing verification; an unverified-app warning and user cap may still apply. Only proceed through that warning for your own identified Google project and the expected read-only scopes. Workspace administrators can still block access, and Google may require additional steps depending on project configuration. [Personal-use verification exception](https://support.google.com/cloud/answer/13464323), [OAuth application states](https://developers.google.com/identity/protocols/oauth2/production-readiness/overview)

After changing from Testing, reconnect RankMe to obtain a new authorization. Refresh tokens can still expire or be revoked for other reasons; RankMe surfaces those failures instead of inventing data.

## What RankMe reads

- Two adjacent 28-day Search Console periods ending three days before the sync date, using finalized web-search data.
- Site-level clicks, impressions, click-through rate, and average position, queried separately from detail rows.
- Up to 5,000 top query rows and 5,000 top page rows per period. Anonymized or omitted queries and row limits mean those rows must not be summed as site totals. [Search Analytics query reference](https://developers.google.com/webmaster-tools/v1/searchanalytics/query)
- Optional GA4 Organic Search sessions and key events for the same calendar dates. Key events in organic sessions are not proof that an article caused a conversion. Search Console uses Pacific dates; GA4 uses its property time zone, so cross-product totals are not directly comparable. [GA4 reporting guide](https://developers.google.com/analytics/devguides/reporting/data/v1/basics)

If both products fail, the sync raises an error so the application can preserve the previous successful snapshot. If only one product succeeds, the result identifies the unavailable portion. Recommendations describe measured changes or investigation opportunities, not ranking guarantees or proven causes.

## Credentials and disconnecting

OAuth configuration and tokens are stored in `data/google-secrets.json` with owner-only permissions (`0600`). The file is separate from RankMe's client database and normal database backup payload. It must not be uploaded, committed, shared, or included in manually copied project archives. API responses exposed to the dashboard never contain tokens or the client secret.

Disconnect removes local access and refresh tokens and attempts revocation at Google. If remote revocation cannot be confirmed, RankMe reports that fact; you can also remove the app in your [Google account's third-party connections](https://myaccount.google.com/connections).

## Validation status

The implementation has mocked tests for PKCE, one-time/expired state, loopback validation, private credential storage, partial scopes, refresh and revoked tokens, safe errors, bounded pagination, report filtering, and protection against empty failed snapshots. **Google authorization and live private report retrieval have not been tested without your own OAuth project and consent.** Those steps remain necessary to validate the connection to your properties.
