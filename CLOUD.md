# Hosted RankMe

The dashboard runs at `https://getrankme.vercel.app`. Vercel serves only the built
static assets and forwards `/api/*` to the Neon Function. The Function checks
RankMe's single-owner passkey session before exposing workspace data or queuing
work. Neon Managed Auth is provisioned separately; it does not grant access to
RankMe, and email/password accounts cannot bypass the passkey gate.

The Mac remains the execution host for the existing Python engine, Codex login,
Google connection, and website publishing configuration. An outbound HTTPS worker
uploads a filtered workspace snapshot and fetches approved commands. A disconnected
Mac leaves the last snapshot readable and new commands queued. It must be awake
with RankMe running to execute work. Credentials for Codex and Google remain local;
article content, reports, and client profiles are stored in the private cloud database.

## First owner and backup passkeys

Use Node 22.20 or newer and the ignored, owner-only `.env.local` file. It must hold
the production database URLs, `RANKME_PUBLIC_ORIGIN`, `RANKME_COOKIE_SECRET`, and
`RANKME_WORKER_TOKEN`. Never put these values in Git or frontend environment variables.

After deployment, create a ten-minute, single-use invitation:

```sh
node --env-file=.env.local --import tsx scripts/cloud-admin.ts bootstrap --output /private/path/rankme-setup.txt
```

Open the URL in that private file in a browser, select **Create my passkey**, and
complete the device's biometric/PIN/security-key prompt. Delete the invitation file
after use. The invitation is in the URL fragment and is removed from browser history
by the page. There is no public signup endpoint or password/email recovery fallback.
Once enrolled, add a backup passkey in Settings. Adding/removing passkeys requires
fresh passkey verification, and the final passkey cannot be removed.

If the owner is away from a device with an existing passkey, an operator with direct
database credentials can issue a temporary link for that same owner:

```sh
node --env-file=.env.local --import tsx scripts/cloud-admin.ts invite-passkey --output /private/path/rankme-passkey-link.txt
```

Open the link on the new device and select **Add passkey on this device**. The link
expires after ten minutes and works once; issuing another link immediately revokes
the previous one. The secret is removed from browser history before the page makes
a request. This adds a credential to the existing owner and keeps the existing
passkeys active. Delete the private link file after use.

The local `localhost:8787` workspace has separate passkeys. Use the local launcher's
explicit `--enroll` flow for local administration; the cloud passkey is scoped to
`getrankme.vercel.app`. Changing the public domain requires planning new enrollment.

## Local worker

The running Python service reads `cloud-bridge.json` from its actual data directory.
The file must be a regular file owned by the current user with mode `0600` and only
two fields: `origin` (the Neon Function's HTTPS origin) and `token` (matching the
server's private `RANKME_WORKER_TOKEN`). Do not paste that token into browser code.
The service still listens only on loopback; no tunnel or inbound router port is needed.

Start RankMe using `Start RankMe.command`. The engine starts once an owner is enrolled
locally or in the cloud. The worker records each command before executing it and saves
its result before acknowledging it. A crash with uncertain execution is marked
`needs_review`; it is not silently repeated. Review the local job state before retrying.

The cloud cannot change executable paths, publishing folders/commands, or Google
credentials. Configure those in the local workspace. Cover downloads remain local.
The hosted backup is a filtered snapshot, not a full recovery backup of the Mac.

## Deploying changes

```sh
npm ci
npm run typecheck
node scripts/package-vercel.mjs
node --env-file=.env.local --import tsx scripts/cloud-admin.ts migrate
neon deploy --env .env.local
vercel deploy --cwd .vercel-stage --prod --yes --logs
```

Test database migrations on an isolated Neon branch before applying them to production.
Migrations create versioned tables in `rankme_cloud`; they do not migrate the local
SQLite database or overwrite Neon Managed Auth tables. The Vercel packaging command
rebuilds the public assets into an isolated, ignored `.vercel-stage` directory. It
contains only five public files, the routing and security configuration, and the
Vercel project link. Its build and install commands are empty, so the remote build
uses the prepared `public` directory without repository dependencies. Deploy from
that stage, never from the repository root.

Session tokens are opaque, stored as hashes, and sent in Secure/HttpOnly/SameSite
cookies. Sessions expire after eight hours and after thirty idle minutes; passive
dashboard polling does not extend that time. All browser mutations require the
configured Origin and session CSRF value. Worker tokens grant snapshot/queue access
and must be rotated on both sides if exposed. Protect the Mac account and its backups;
passkeys do not encrypt local disk or prevent a compromised operating-system account.
