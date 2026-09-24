# RankMe

## Visibility automation

The **Visibility** tab connects public audits, Google observations, source-checked opportunity research, actual Codex answer samples, prioritized work, and protected article refreshes. Configure recurring audits, research, answer sampling, and bounded brief/refresh preparation per website. All 26 requested visibility areas are listed with honest implementation and integration status. See [visibility workflows](VISIBILITY.md) for setup and remaining capabilities, [security boundaries](SECURITY.md), and the [full target scope](AUTOMATION_SCOPE.md).

This extension does not yet implement every part of that target scope: arbitrary technical edits/free-tool/infographic production need dedicated adapters, and other AI platforms and comprehensive keyword/backlink coverage require integrations. Outreach is excluded by request. The existing article generation and publishing controls remain in force.

RankMe is a local, single-user workspace for producing weekly articles for your clients' coded websites. Add a website, inspect the business, confirm its profile, choose a subject, and build a connected content plan. Codex researches, drafts, reviews, and revises articles. RankMe can export the result or publish it through a configured website project.

The interface, database, scheduling, and files run on your computer. AI work runs through the Codex CLI using your ChatGPT sign-in. This is not offline AI: relevant business information, source material, and article text are sent to OpenAI. Your subscription's Codex allowance applies and is shared with other Codex work. RankMe does not require an OpenAI API key and rejects API-key-only sign-in.

## Requirements

- macOS with Python 3.9 or later. RankMe has no third-party Python dependencies.
- A current Codex CLI with ChatGPT sign-in and sufficient subscription capacity.
- Internet access for research and publishing.
- Git and the website's normal build/deployment tools when using Git publishing.
- A website content renderer that supports the selected Markdown, MDX, or JSON format.

The launcher is designed for macOS. The Python application can also run directly on compatible systems, but Unix file locking is required, and the Finder launcher and its process validation are macOS-specific.

## Start RankMe

Double-click **Start RankMe.command**. The launcher starts the local service and opens `http://127.0.0.1:8787` in your browser. It reuses an existing RankMe session on that port. Closing the browser or launcher terminal does not stop the background service.

If macOS requests permission to open the downloaded command file, use Finder's **Open** action after inspecting the file. Alternatively, open a terminal in this folder and run:

```bash
python3 scripts/launch.py
```

For a foreground service with terminal output:

```bash
python3 run.py
```

RankMe binds only to `127.0.0.1`. Do not expose this application through a public proxy or port forwarding. It is intended for one trusted local user, not multiple accounts or a shared server.

## Connect your ChatGPT subscription

Install or update the Codex CLI using the [official Codex CLI documentation](https://developers.openai.com/codex/cli/). Then run:

```bash
codex login
codex login status
```

Choose ChatGPT sign-in in the login flow. The status command must report ChatGPT authentication. In RankMe, open **Settings** and check the AI connection. If Codex is installed outside your usual executable path, enter its absolute executable path in Settings.

The launcher includes common Homebrew and `~/.local/bin` locations in its executable search path. The model field can remain blank to use the CLI's supported default. The configured CLI must support the structured-output and isolation flags RankMe uses; older releases may need updating.

RankMe removes common API-key environment variables from child AI processes, isolates job files, and requests read-only execution with shell access disabled. It does not purchase credits, reset your allowance, or bypass subscription limits. Quota and authentication failures appear as failed jobs that can be retried after the underlying problem is fixed.

## First client

1. Select **Add website** and enter the client's public website URL. RankMe immediately queues an inspection.
2. Wait for the inspection to finish. RankMe reads accessible public pages, observes existing content, and builds a company profile with sources and unknowns.
3. Review the profile. Correct the name, services, audience, locations, tone, and missing facts. Confirm it once it accurately describes the company.
4. Choose one suggested subject or enter your own. RankMe researches a connected plan of up to twelve articles.
5. Generate a first article manually. Inspect its preview, sources, and review findings before connecting live deployment.
6. Configure publishing if the website project is ready. Start with local export and verify that the website renders the generated file correctly.
7. Set the next run date and enable the client's weekly schedule. Enable automatic publishing in its connection only when you want passing articles to deploy without individual approval.

Inspection is thorough within its configured page limit, but it cannot understand inaccessible pages, private business practices, or facts missing from the website. Do not treat an inferred profile as approved business truth until you confirm it. The crawler respects public-network restrictions and robots rules; JavaScript-only or blocked pages may require better public content or manual profile corrections.

## Weekly operation

Each client uses a seven-day cadence, averaging approximately 52 articles per year. The application needs to be running, the computer must be awake, and an internet connection must be available. No system login item or `launchd` service is installed automatically.

RankMe researches the selected subject, writes a draft, runs structural and AI review, and attempts a bounded repair when checks fail. Passing articles become ready or publish through the configured connection. Failed quality checks hold the article. Failed jobs remain visible for attention rather than triggering an uncontrolled retry loop.

The scheduler avoids a flood of missed articles after a long shutdown. It catches up conservatively and advances the schedule. A paused client does not start new scheduled work. The global pause in Settings stops the queue from starting new jobs; it does not forcibly interrupt a job already running.

Changing an article requires another successful review before publication. Keep an eye on the activity history and exception states even after the first client is calibrated. The system can reduce routine intervention; it cannot guarantee factual perfection, search rankings, indexing, leads, or sales.

## Connect a coded client website

The publishing connection contains the local website project path, article directory, content format, build command, and optional deployment settings. See [INTEGRATION.md](INTEGRATION.md) for the exact field contract and recovery behavior.

A typical connection for a website that already reads Markdown from `content/blog` uses:

```json
{
  "project_path": "/absolute/path/to/client-website",
  "content_dir": "content/blog",
  "format": "md",
  "mode": "git",
  "branch": "main",
  "remote": "origin",
  "build_command": ["npm", "run", "build"],
  "deploy_command": [],
  "public_url_template": "https://client.example/blog/{slug}",
  "auto_publish": false
}
```

The connection must match the website's actual renderer and hosting workflow. Pushing a Git branch only deploys a site when its host is configured to build from that branch. A standalone deployment CLI can instead be configured as an argument array. RankMe does not create hosting accounts, install website dependencies, invent repository access, or add a blog renderer automatically.

Build and deployment commands run with your local user's permissions. Configure only commands you trust. Commands are argument arrays, not shell scripts; operators such as `&&` are not interpreted. Git publishing requires the selected branch, a clean repository, and a successful build. It stages the article file only and never force-pushes or resets unrelated work.

Publication states have distinct meanings:

- **Ready:** review passed; the article has not been exported or deployed.
- **Exported:** content exists locally. This does not mean it is online.
- **Verification pending:** a publishing action completed, but the expected public page is not yet verified.
- **Published:** the public URL responded successfully and contained the expected title. This does not establish search indexing.
- **Held / Error:** checks or execution need attention.

With no connected project, a manual export saves into the configured data directory. Export alone cannot make content appear on a client website.

## Stop, restart, or change the port

To inspect the background service:

```bash
python3 scripts/launch.py --status
```

Pause automation and wait for any running job to finish, then stop it:

```bash
python3 scripts/launch.py --stop
```

The stop command validates the launcher record against the current session and process. It refuses to stop a running job. A foreground service started with `python3 run.py` should be stopped with **Ctrl+C** in its original terminal.

To restart, double-click the launcher again. Interrupted jobs are flagged for review. Publication checkpoints preserve completed operations and prevent common duplicate writes or deployments.

A custom port or data directory can be selected explicitly:

```bash
python3 scripts/launch.py --port 8788 --data-dir /absolute/path/to/rankme-data
python3 scripts/launch.py --port 8788 --data-dir /absolute/path/to/rankme-data --stop
```

Use the same arguments when stopping or checking that instance. An exclusive lock prevents two servers from using the same data directory; a single worker owns scheduling. When reusing a port, the launcher opens the RankMe instance already listening there rather than switching its data directory.

## Data and backups

By default, local data lives in the `data` folder beside this README:

- `rankme.sqlite3`: clients, articles, jobs, settings, and event history.
- `revisions/`: article snapshots and previous generated files.
- `publishing/`: checkpoints used to resume publishing safely.
- `exports/`: exported articles without a connected website project.
- `runs/`: temporary Codex working files; normal completed jobs remove their temporary directories.
- `server.log`: background service output.
- `launcher-PORT.json`: local process metadata. It stores a hash of the session token, not the token itself.

Use the dashboard's backup download for a readable JSON snapshot of database records and settings. That JSON does **not** contain website repositories, Git credentials, Codex authentication, revision files, deployment checkpoints, or all application files. It is not a complete disaster-recovery backup, and this version has no one-click JSON restore workflow.

For a complete local backup, stop RankMe and copy its entire data directory. Stopping first avoids an inconsistent SQLite copy while its WAL files are active. Back up client repositories separately. Restore the data directory while RankMe is stopped, and inspect any interrupted or pending publication before retrying it.

The database and backups are local files, not encrypted storage. Protect your macOS account and backups. Do not place tokens directly in deployment arguments or public URL templates. Prefer the deployment tool's normal credential store. Client content and configuration may appear in database snapshots.

## Testing and troubleshooting

Run the automated suite from this folder:

```bash
python3 -m unittest discover -s tests -v
```

The suite uses temporary files and repositories and mocked network/AI results. It does not establish real client-site compatibility or guarantee live Codex availability. Validate one complete client workflow with your account and actual publishing setup before scaling to more websites.

Common problems:

- **Codex unavailable:** check its executable path and run `codex login status` in your terminal.
- **Subscription limit reached:** wait for the allowance to reset, then retry the failed job.
- **Inspection incomplete:** check the site's public access, robots policy, and whether content requires browser JavaScript.
- **Article held:** inspect source and editorial findings, correct the input or draft, and run review again.
- **Git repository dirty:** preserve or commit your existing work before retrying. RankMe does not discard it.
- **Verification pending:** check the host's deployment status and public URL pattern. A queued host deployment may need more time.
- **Unknown deployment outcome:** inspect the external deployment before resolving its checkpoint. See the recovery section of INTEGRATION.md.
- **Port occupied:** use a different port. The launcher will not open an unrelated service as RankMe.
- **Startup failed:** inspect `data/server.log`, or run `python3 run.py` in a terminal for startup output.

## Current scope

This release provides public-site inspection, approved business profiles, subject planning, weekly article production, review gates, local export, configurable Git/command deployment, live-page verification, activity history, and local backup export.

Connected Google properties now refresh automatically, and the visibility workflow can prepare protected refresh drafts from observed performance declines. Paid keyword-volume data, complete backlink coverage, arbitrary site fixes, and broader AI-platform measurements still require integrations. Link exchange and outreach are excluded. See VISIBILITY.md for precise current capabilities and limitations.

## Project evidence

See [VALIDATION.md](VALIDATION.md) for the real subscription test and automated checks, and [RESEARCH.md](RESEARCH.md) for competitor findings and product rationale.

## Brand-matched 3D article covers

Every new article now receives a generated landscape cover after its text passes review. RankMe uses Codex's built-in image generation with the existing ChatGPT sign-in, followed by a separate visual review of the actual image. No image API key or API fallback is used. Subscription availability and usage limits still apply.

Public CSS color tokens provide initial palette suggestions. Review or edit these in the business profile's **Article cover images** fields; a visual direction records the website’s 3D shapes, materials, mood, and theme. If reliable colors cannot be detected, add them before creating covers. The generator creates polished 3D editorial illustrations using the saved website theme and brand palette. It selects concepts for each article’s audience: mature and reassuring for adult treatment topics, approachable and parent-facing for pediatric care. It avoids fake text, unrelated decorative props, toy-like adult imagery, fabricated staff or patients, and treatment-result claims. Generated scenes are illustrative, not photographs of the client's real premises.

Failed visual checks get one automatic revision and then hold the article. Each cover has descriptive alt text, its prompt, a file hash, and a review result. Changing article content, brand colors, visual direction, audience, or tone requires a new matching cover. Existing unpublished drafts can use **Generate cover**; **Regenerate cover** creates a new version.

The article preview displays the actual cover. Download the article and cover separately, or publish them together. Configure **Cover image folder** and **Public image path** in the publishing connection. Defaults are `public/images/articles` and `/images/articles`. Your website's renderer must display the generated `image` and `imageAlt` frontmatter fields (or the corresponding JSON fields). The Git adapter commits both files and restores both after a failed build. Keep `data/covers/` and `data/publishing-assets/` with complete backups.

## SEO data and backlink opportunities

Open a client and choose **SEO & links**. Connect your own Google Desktop OAuth project, authorize read-only access, and choose the matching Search Console property plus optional GA4 property. The app reads two adjacent 28-day periods, preserves dated evidence, and uses a bounded selection of observed queries and pages when creating future content plans. Existing plans are not silently rewritten. See [Google setup](GOOGLE-SETUP.md) for exact account steps and data limits.

Connected properties refresh daily while the local server runs, unless automatic Google sync or the workspace is paused. Backlink research uses the signed-in Codex subscription, verifies source pages, and saves personalized outreach drafts. You can also track a known external page. Known pages are checked weekly while the server runs; latest observed link status remains separate from outreach status. No messages are sent or reciprocal network links placed. See [backlink research](BACKLINKS.md).

This does not provide a market-wide keyword-volume database, a full backlink index, guaranteed rankings, or acquired placements. Google metrics require account authorization; outreach drafts require a real editorial relationship to become links. OAuth secrets stay outside database backups and AI prompts.
