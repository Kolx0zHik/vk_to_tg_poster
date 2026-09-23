# AGENTS.md

## Project Overview

This repository is a small VK-to-Telegram reposting service.

- `src.main` runs the posting job once or on a cron-like loop.
- `src.web` serves the web UI and config API.
- `src.pipeline` contains the main reposting workflow.
- `src.vk_client` fetches and normalizes VK posts.
- `src.tg_client` publishes content to Telegram.
- `src.config` is the source of truth for config parsing/serialization.
- `src.cache` stores deduplication state, per-community `baseline` and cached owner ids.
- `src.backfill` holds backfill/resume requests written by the web UI and consumed by the scheduler.
- `src.envfile` loads secrets from `.env` next to the config path.
- `src.dedup` performs the optional LLM-based semantic duplicate check (OpenAI-compatible API).

The project intentionally has no database. Runtime state is stored in files under `data/` and `logs/`.

Version: see `VERSION` (single source of truth, read by `src/version.py`).

## Documentation Map

Read these before changing behavior; keep them in sync with the code:

| File | Purpose | When to update |
|---|---|---|
| `README.md` | user-facing intro, stack, run and env reference | new env var, new run mode, changed user-facing feature |
| `ARCHITECTURE.md` | modules, data flows, state formats, API contract | new module, changed file format, new endpoint |
| `STATE.md` | current status: done / in progress / broken / not verified | after every significant change |
| `ROADMAP.md` | near/mid/far plans | when priorities change |
| `DECISIONS.md` | ADR log with context, alternatives, consequences | any architectural or product decision |
| `CHANGELOG.md` | version history | every released version |

Do not duplicate details across files: put facts in one place and link to it (`ARCHITECTURE.md` owns formats,
`STATE.md` owns status, `DECISIONS.md` owns rationale). Update `STATE.md` and `CHANGELOG.md` in the same
task as the code change, bump `VERSION`, and add an ADR when a decision constrains future work.

## Runtime Model

The normal container runtime starts two processes from `entrypoint.sh`:

1. `python -m src.main` in background
2. `uvicorn src.web:app` in foreground

Default port is `8222`.

Important environment variables:

- `CONFIG_PATH` defaults to `data/config.yaml`
- `RUN_MODE` is `scheduled` or `once`
- `PORT` defaults to `8222`
- `VK_API_TOKEN`, `TELEGRAM_BOT_TOKEN`, `LLM_API_KEY` — secrets, read only from the environment/`.env`
- `LLM_DEBUG_LOG` — temporary flag for testing semantic deduplication (1/true/yes/on)
- `TZ` affects logging timestamps and defaults to `Europe/Moscow`

Secrets live in a `.env` file next to `CONFIG_PATH` (in Docker the mounted `data/` directory), loaded by
`src/envfile.py`; explicit environment variables win. `config.yaml` never stores tokens, and tokens in an old
YAML are ignored. `.env.example` documents the keys; `.env` is gitignored.

If `data/config.yaml` is missing, `entrypoint.sh` creates it with built-in defaults.

## Repo Layout

- `src/`: application code
- `static/`: UI assets (index.html, script.js, style.css, logo.png)
- `config/config.example.yaml`: developer-only example config for manual runs from source
- `.env.example`: example secrets file, copied to `data/.env`
- `.github/workflows/publish.yml`: builds and pushes `ghcr.io/kolx0zhik/vk_to_tg_poster`
- `Dockerfile`: multi-stage Python 3.11 image
- `docker-compose.yml`: local container run with mounted `data`
- `docker-compose.dev.yml`: build from sources
- `tests/`: unit tests (two files); do not assume meaningful end-to-end coverage
- `README.md`, `ARCHITECTURE.md`, `STATE.md`, `ROADMAP.md`, `DECISIONS.md`, `CHANGELOG.md`: docs (see map above)

Known dead weight: none right now. Previously `src/scheduler.py`, the `apscheduler` dependency, an empty
`scripts/` directory, an empty `.codex` file and a stray `poster.log` in the repo root were removed in 1.1.9 —
do not reintroduce them.

## Architecture Notes

### Config

Configuration is YAML-backed and parsed into dataclasses in `src.config`.

- Preserve the current schema shape unless the task explicitly requires config changes.
- Secrets belong only in `.env`/environment (`VK_API_TOKEN`, `TELEGRAM_BOT_TOKEN`, `LLM_API_KEY`); never read
  them from or write them to `config.yaml`. Keep `src/envfile.py` loading behavior and `.env.example` in sync.
- Keep `config_to_dict` and `save_config_dict` in sync with parser changes.
- UI validation in `src.web` uses Pydantic models and should remain aligned with dataclass config parsing.
- Keep `log_retention_days` and other logging-related general settings aligned between `src.config` and `src.web`.
- `ContentTypes` defaults to `audio: true` in code while the UI writes `audio: false`; do not "fix" this
  silently — it is recorded in `STATE.md`.
- `general.semantic_dedup` (`enabled`, `window_days`) and `llm` (`base_url`, `model`, `prompt`) are non-secret
  and edited from the web UI; keep `src.config`, `src.web` models and `static/script.js` in sync. The system
  prompt is mandatory: there is no built-in default, an empty `llm.prompt` disables the LLM check entirely
  (pipeline logs a warning; `POST /api/config` rejects "enabled without prompt" with 400).

### Posting Flow

Core flow:

1. Load config
2. Resolve VK community identifier
3. Fetch recent posts from VK
4. Filter by baseline, blocked keywords, allowed content types, and dedup cache
5. Optionally run the LLM semantic duplicate check (posts with text only)
6. Publish to Telegram
7. Update dedup cache and baseline

Important invariants:

- Order matters: newer VK posts are fetched first, but publishing is done oldest-first.
- Dedup uses original repost source ids when available via `copy_history`.
- The per-community `baseline` advances even when a post is skipped as duplicate, to avoid replay loops.
- Missing tokens/channel should not crash the scheduler; the run is skipped with a warning.
- Per-community `baseline` in the cache doubles as the backfill boundary: posts above it are published, posts at or below it are marked skipped.
- The web UI writes backfill/pause-request state to `data/backfill.json` (next to `cache_file`); the scheduler consumes it into a cache baseline on the next run. A failed VK fetch keeps the request for the following run instead of dropping it.
- Inactive (`active: false`) communities must be skipped before any VK request.
- The semantic check is advisory and fail-open: any LLM/network/config error keeps the post publishable; posts
  without text are never checked. A duplicate verdict marks the post `skipped` (with `dedup_skipped` in stats).
  The user prompt (built in `src/dedup._build_user_prompt`) owns the data shape and the answer contract: the new
  post (text capped by `NEW_POST_TEXT_MAX`) plus a dated, 1-numbered candidate list (`{date, text}` each, total
  capped by `CANDIDATES_CHAR_BUDGET` — trailing candidates dropped whole, the last kept one cut short with a
  marker) and a line requiring strict JSON `is_duplicate`/`reason`/`matched`, where `matched` is the candidate
  number the pipeline maps back to its source post for logging. The configured `llm.prompt` carries only the
  dedup rules; it no longer has to spell out the JSON format (see ADR-021). The legacy
  `chat_id`/`message_id`/`date_unix`/`raw_text` payload from the n8n flow and the unused `matched_message_id` are
  gone.
An empty `llm.prompt` disables the check entirely (see above).

### State Files

| File | Owner | Notes |
|---|---|---|
| `data/config.yaml` | web UI + humans | atomic writes via `save_config_dict`; no secrets |
| `data/.env` | humans | secrets only, next to `CONFIG_PATH`; loaded by `src/envfile.py` |
| `data/cache.json` | **scheduler only** | schema v2; published posts keep a short `text` for the semantic pool |
| `data/backfill.json` | web UI (written), scheduler (consumed) | path derived via `requests_path_for(cache_file)` |
| `data/avatars.json` | web UI | 24h TTL cache of name/photo |

Never write `cache.json` from the web process: the scheduler rewrites it continuously and an outside write
can roll back progress and cause re-publishing.

### Telegram Behavior

`src.tg_client` contains a lot of product behavior. Treat it as intentional unless the task says otherwise.

- Single photo may include caption.
- Multiple photos are sent as media group, then text separately.
- Long photo captions and long message texts share one limit: truncated to `CAPTION_LIMIT` (1024) with the
  same continuation notice, on paragraph/line/sentence/word boundaries and without breaking HTML entities or
  tags.
- All message texts are sent with `parse_mode=HTML`; callers pass already escaped (`_escape_html`) bodies.
- Some videos are sent as links instead of uploaded video files.
- Photos are downloaded and uploaded as files (10 MB limit) with a fallback to URL delivery.
- Link button back to the VK post is part of expected behavior.
- Telegram 429 handling retries once based on `retry_after`.
- Not handled yet: albums over 10 photos, re-send without duplicates after a
  partial failure, video/audio uploaded as files. See `STATE.md`.

### Web UI

`src.web` is not just a static page. It:

- reads and writes the YAML config
- validates payloads with Pydantic
- can call VK APIs to resolve names and refresh avatars
- persists avatar cache in `data/avatars.json`
- writes backfill requests to `data/backfill.json`

UI conventions in `static/`:

- Plain HTML/CSS/JS, no build step, no npm, no external framework.
- All user-facing strings are Russian; keep the current tone.
- Tokens are never edited in the UI: they live in `.env`. The header has an "ИИ-проверка" modal
  (`llm.base_url`, `llm.model`, `llm.prompt`, `general.semantic_dedup.enabled/window_days`) and the Telegram
  channel field sits in the main settings card.
- The groups panel is master-detail: left list (search + names), right settings (status segment, content
  type icon toggles). No checkboxes, no raw community ids anywhere, community name links to VK.
- Adding a community happens in a modal (same style as the old tokens modal) with preset amount buttons and
  content type toggles; it saves immediately via `POST /api/config` + `POST /api/backfill`.
- Use the `escapeHtml` helper for any dynamic value rendered into HTML.
- Keep the `.hidden` utility as `display: none !important`; component rules with `display: flex/grid`
  otherwise override it (this bug already happened twice: `#toast` and the add-group form).
- Modals close on Escape and on backdrop click; keep that behavior for new modals.

Be careful with any change that touches both `src.web` and `src.config`; they must stay compatible.

## Safe Change Guidelines

- Follow the existing Python style and keep code straightforward.
- Prefer extending current modules over introducing new abstractions unless complexity clearly demands it.
- Keep filesystem-based persistence working inside Docker with mounted volumes.
- Preserve UTF-8 handling for logs, YAML, and JSON files.
- Keep Russian user-facing messages consistent with the current project tone.
- Treat `config/config.example.yaml` as a developer convenience for manual runs from source; update it when schema changes affect that workflow.
- New runtime state belongs in `data/` and must be added to `.gitignore`.
- Do not add dependencies without a concrete need; report the reason in the task summary.

## Forbidden Patterns

- Do not commit or push to `main`, tag releases, or publish images unless the user explicitly asks for that step.
- Do not touch the production container, its `data/` volume, or the real `config.yaml`; use a separate
  `CONFIG_PATH` under `/tmp` for verification runs.
- Never log, print, or commit secrets (VK/Telegram/GitHub tokens). Log errors through the redacting formatters.
- Never write `VK_API_TOKEN`/`TELEGRAM_BOT_TOKEN`/`LLM_API_KEY` into `config.yaml` or return them from the API;
  secrets live only in `.env`/environment (see `src/envfile.py`).
- Do not write `data/cache.json` from `src.web` (see "State Files").
- Do not silently change documented product constraints (no DB, one shared cron, per-attachment messages).
- Do not edit generated or runtime artifacts: `data/*.json`, `data/logs/*`, `*.log`.
- Do not use interactive git commands (`rebase -i`, force-push, `reset --hard`) or bypass hooks.
- Do not delete documentation sections without explaining why; docs are the shared memory of this project.

## Risky Areas

Take extra care and verify changes when touching:

- `src.pipeline`: filtering, ordering, dedup, and baseline advancement
- `src.tg_client`: caption limits, HTML escaping, media grouping, rate-limit retry
- `src.vk_client`: attachment parsing and repost source extraction
- `src.cache`: atomic persistence, statuses, migration from the legacy schema, stored semantic-pool text
- `src.backfill`: baseline computation and request lifecycle
- `src.web`: config validation, avatar refresh, and API-facing schema changes
- `src.logger`: duplicate handlers, timezone behavior, log cleanup, and the compact file-log contract
- `src.envfile`: `.env` loading order (explicit environment must win)
- `src.dedup`: prompt/JSON parsing, fail-open behavior, and never leaking the API key

### Logging

- File logs are intentionally compact: prefer summary lines and short error messages over verbose debug output.
- Avoid writing full tracebacks to the rotating log file in normal operation unless the task explicitly calls for deeper diagnostics.
- If you change logging behavior, preserve the distinction between compact file logs and more verbose troubleshooting output.

## Verification Expectations

Before claiming a change is complete, verify what is realistically possible in this repo.

Useful local commands:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

python -m unittest discover -s tests          # unit tests (100 as of 1.1.14)
node --check static/script.js                 # frontend syntax check

CONFIG_PATH=data/config.yaml RUN_MODE=once python -m src.main
uvicorn src.web:app --host 0.0.0.0 --port 8222
docker compose up --build
```

For UI changes, run the app against a throwaway config (`CONFIG_PATH=/tmp/...`) and check the page in a
browser at desktop and 390 px widths; assert that `document.body.scrollWidth` does not exceed the viewport.

If you change config or web validation, also check that:

- the web app can load config without crashing
- runtime-created directories and files still behave correctly
- secrets are read from `.env` next to `CONFIG_PATH` and are absent from `config.yaml` and `/api/config`

If you cannot run end-to-end API checks because real VK/Telegram tokens are unavailable, say that explicitly instead of guessing.

## Current Project Constraints

These constraints are part of the current product, not accidental limitations:

- no database
- one shared cron schedule for all communities
- minimal test coverage
- dedup and progress tracking stored in JSON
- each attachment type may result in separate Telegram messages
- dedup keys are global, so the same original post is never published twice
- deleting a community keeps its state in `cache.json`
- scheduler and web UI run in the same container

Do not silently "improve" these constraints unless the task asks for a product or architecture change.

## Subagent Workflow

For bugfix and feature tasks, prefer the following default subagent workflow when the work can be split safely:

- Subagent 1: investigate the problem, gather context, and localize the likely change area.
- Subagent 2: implement the agreed code changes.
- Subagent 3: check compatibility, regression risk, and verification gaps after implementation.
- Main agent: coordinate the work, collect findings, resolve ambiguous points, integrate results, and perform final validation.

Use this workflow as the default operating model for implementation tasks, not as a rigid requirement for every small change.

- If the task is too small, tightly coupled, or not worth parallelizing, the main agent may keep the work in one place.
- Even when not all three subagents are used, the main agent should still cover the same responsibilities logically: investigation, implementation, and regression review.
- Subagents should stay focused on their assigned role and avoid taking over final release responsibilities unless the main agent explicitly delegates that final step.

### Git And Deploy Responsibilities

- The main agent is responsible for final `git push` decisions and for triggering any branch or image update workflow.
- Subagents should not push branches or publish images on their own unless the main agent explicitly assigns that as a final handoff step.
- If changes are pushed to `main`, push `main` and update the `latest` image.
- If changes are pushed to any non-`main` branch, push that branch and update the `test` image.
- When deciding whether to push or publish, the main agent should confirm that the branch target matches the intended image tag before proceeding.
- Verify the current branch (`git branch --show-current`) before committing; accidentally committing to `main`
  already happened and produced an unreviewed release.

### Versioning And Tags

Every change that ships gets a patch bump in `VERSION` (`1.1.x` for fixes and tweaks, minor/major only when the
owner says so). Keep the sequence complete and never rewrite published history.

| Moment | `VERSION` | Image tags | Git tag |
|---|---|---|---|
| work in progress on a branch | bump is optional | `test`, `sha-<short>` | none |
| pushed to `main` (verified on the test stand) | final for that change | `latest`, `sha-<short>` | `vX.Y.Z` annotated tag on the `main` commit |
| hotfix that skips the test stand | bump | `latest` | `vX.Y.Z` annotated tag |

Rules:

- Create tags only for versions that reached `main`; test-branch iterations are not tagged
  (`v1.0.0`, `v1.1.0`, `v1.1.1`, `v1.1.8`, `v1.1.9` follow this; `1.1.2`–`1.1.7` were pre-release iterations).
- Tags are annotated (`git tag -a vX.Y.Z -m "..."`) and pushed with `git push origin vX.Y.Z` — pushing a tag
  triggers the `:vX.Y.Z` image build.
- Update `CHANGELOG.md` in the same commit as the bump, and `STATE.md` right after.
- Keep all branches up to date with `main` after a merge (`git merge --ff-only main` in each branch, then push).
- Never move or force-push an existing tag; a wrong release gets a new patch version instead.

The normal flow for a change is: branch (`feature/**`, `fixes/**`) → CI publishes `:test` → owner verifies on
the test stand → merge to `main` and push → CI publishes `:latest` → annotated tag `vX.Y.Z` and push it.

## Guidance For Future Agents

- Start by reading `README.md`, `ARCHITECTURE.md`, `STATE.md`, then `src.main`, `src.web`, `src.pipeline`, `src.config`.
- Prefer minimal, targeted changes over broad refactors.
- When behavior changes, update docs or config examples in the same task, plus `STATE.md`/`CHANGELOG.md`.
- Watch for hidden coupling between YAML schema, web validation, and runtime code.
- Be explicit about what you verified and what you could not verify.
