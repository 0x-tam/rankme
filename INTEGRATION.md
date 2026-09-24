# Connecting a coded website

RankMe writes content into an existing website project. The website must already render that format and directory as blog pages. Exporting a Markdown file does not add a blog renderer. Configure one client with a test project before enabling deployment.

## Connection fields

- `project_path`: absolute path to the website repository root.
- `content_dir`: project-relative directory, for example `src/content/blog` or `content/blog`.
- `format`: `md`, `mdx`, or `json`. MDX is deliberately restricted to inert Markdown: no braces, HTML/JSX, import statements, or exports.
- `mode`: `export` saves local files. `git` validates and commits the article.
- `build_command`: argument array, such as `["npm", "run", "build"]`. A JSON-encoded argument array is also accepted. Shell command strings are not accepted. Commands execute with your local user's permissions: configure only trusted project commands.
- `branch`: required in Git mode; it must match the checked-out branch.
- `remote`: Git remote name, default `origin`.
- `auto_publish`: false by default. When true, Git mode pushes the commit to the configured branch. An optional deployment command runs afterward.
- `deploy_command`: optional argument array for the project's deployment tool. It should be safe to retry. Export mode can deploy using this command without Git.
- `public_url_template`: full HTTP(S) URL containing `{slug}`, for example `https://example.com/blog/{slug}`.

Git mode requires a successful build command and a clean repository. Ignore build outputs in the website's `.gitignore`. RankMe stages only the generated article, never all project changes. If a build changes other tracked files, RankMe stops and leaves those changes for inspection. It does not reset the repository.

## Content schema

Markdown front matter contains `title`, `description`, `slug`, `date`, `rankme_id`, and `rankme_hash`. JSON output contains the article object plus `rankme_id` and `rankme_hash`. Article bodies must be Markdown without raw HTML or unsafe URL schemes. The website renderer must also sanitize HTML and URLs; content validation is not a replacement for renderer security.

RankMe refuses unrelated files at the target path. Identical article retries reuse existing content. Changed RankMe-owned content is backed up in the configured data directory's `revisions` folder before replacement. Git provides additional history in Git mode. A failed build restores the previous article file; it does not undo unrelated changes made by the build command.

## Deployment and verification

Without an enabled push or deployment command, a successful result is `exported`, not `published`. With deployment enabled, RankMe returns `verification_pending` until the public page returns HTTP 200 and includes the article title. It returns `published` only after that check succeeds. This verifies availability and expected text, not search indexing or ranking.

A failed push preserves the local article commit. Fix the remote connection and retry the same article: RankMe reuses the existing commit. A failed deployment may require rerunning an idempotent deployment command. Avoid manually editing generated content while a job is pending.

Live verification uses RankMe's public-only crawler with private-network protection. Localhost and private staging URLs cannot be verified with this adapter.

## Recovery guarantees

The engine passes its configured data directory to `publish_article(..., data_dir=...)`. Publication checkpoints and revision backups use that location. Checkpoints contain operation state and commit hashes, not connection credentials or command arguments. Command failure messages do not persist raw output because deployment tools can print secrets.

An interruption after writing or staging an unchanged RankMe-owned article can resume if it is the only changed file. An interruption after the commit reuses that commit. A completed Git push or deployment command is not repeated when the checkpoint confirms success. Changing the reviewed article or deployment configuration creates a separate operation.

A process can stop after an external deployment succeeds but before success is recorded. In that ambiguous case, RankMe blocks another deployment and requests manual verification. Inspect the external deployment first. The checkpoint in `publishing/` can then be resolved by recording `deployed: true` and `deployment_started: false`, or removed only when rerunning the configured deployment command is safe. Commands that explicitly report failure may be retried and must be idempotent. Exactly-once execution of arbitrary external commands cannot be guaranteed.

## Article covers

Articles with a reviewed cover publish the image and article together. The connection accepts:

- `image_dir`: project-relative image directory, default `public/images/articles`.
- `image_url_prefix`: root-relative public URL prefix, default `/images/articles`.

The site must serve `image_dir` at that URL prefix. For example, frameworks that serve `public/` at the site root generally match the defaults. RankMe does not configure the site's static asset routes automatically.

Markdown and MDX front matter use `image` for the public image URL and `imageAlt` for its alternative text. JSON uses the same fields and a `cover` object containing only `url`, `alt`, `sha256`, and `format`. It does not export the source path, generation prompt, or internal image review. The website's article template must render the `image` field and use `imageAlt` as its alt text.

The engine supplies `article.cover` with a source `path`, `alt`, `sha256`, and successful `review`. The source must be beneath the configured data directory's `covers/` folder, must match its reviewed hash, and must contain PNG, JPEG, or WebP data. The publisher requires an explicit `data_dir` when a cover is supplied. A missing cover remains supported for legacy adapter callers; the production engine enforces its required-cover policy.

Cover filenames include article identity and content hashes. Existing unrelated or modified image files are never reused. Ownership records are retained in the data directory's `publishing-assets/` folder. Updating a cover creates a new image file and preserves the older version. Include the full data directory when backing up so those ownership records remain available.

Git publishing stages and commits exactly the article and its current cover. A failed build restores both affected files and preserves older covers. Interrupted partial writes or staging can resume only when every changed file belongs to that exact operation and still matches its reviewed content. A build that changes the generated article or image is blocked before commit.
