# AGENTS.md

## Project Overview

This repository is a small VK-to-Telegram reposting service.

- `src.main` runs the posting job once or on a cron-like loop.
- `src.web` serves the web UI and config API.
- `src.pipeline` contains the main reposting workflow.
- `src.vk_client` fetches and normalizes VK posts.
- `src.tg_client` publishes content to Telegram.
- `src.config` is the source of truth for config parsing/serialization.
- `src.cache` stores deduplication state and per-community `last_seen`.

The project intentionally has no database. Runtime state is stored in files under `data/` and `logs/`.

## Runtime Model

The normal container runtime starts two processes from `entrypoint.sh`:

1. `python -m src.main` in background
2. `uvicorn src.web:app` in foreground

Default port is `8222`.

Important environment variables:

- `CONFIG_PATH` defaults to `data/config.yaml`
- `RUN_MODE` is `scheduled` or `once`
- `PORT` defaults to `8222`
- `VK_API_TOKEN` can override `vk.token` from YAML
- `TELEGRAM_BOT_TOKEN` can override `telegram.bot_token` from YAML
- `TZ` affects logging timestamps and defaults to `Europe/Moscow`

If `data/config.yaml` is missing, `entrypoint.sh` creates it with built-in defaults.

## Repo Layout

- `src/`: application code
- `config/config.example.yaml`: developer-only example config for manual runs from source
- `static/`: UI assets and logo
- `.github/workflows/publish.yml`: builds and pushes `ghcr.io/kolx0zhik/vk_to_tg_poster:latest`
- `Dockerfile`: multi-stage Python 3.11 image
- `docker-compose.yml`: local container run with mounted `data`
- `tests/`: currently almost empty; do not assume meaningful automated coverage exists

## Architecture Notes

### Config

Configuration is YAML-backed and parsed into dataclasses in `src.config`.

- Preserve the current schema shape unless the task explicitly requires config changes.
- Keep env-var override behavior for VK and Telegram tokens.
- Keep `config_to_dict` and `save_config_dict` in sync with parser changes.
- UI validation in `src.web` uses Pydantic models and should remain aligned with dataclass config parsing.
- Keep `log_retention_days` and other logging-related general settings aligned between `src.config` and `src.web`.

### Posting Flow

Core flow:

1. Load config
2. Resolve VK community identifier
3. Fetch recent posts from VK
4. Filter by `last_seen`, blocked keywords, allowed content types, and dedup cache
5. Publish to Telegram
6. Update dedup cache and `last_seen`

Important invariants:

- Order matters: newer VK posts are fetched first, but publishing is done oldest-first.
- Dedup uses original repost source ids when available via `copy_history`.
- `last_seen` advances even when a post is skipped as duplicate, to avoid replay loops.
- Missing tokens/channel should not crash the scheduler; the run is skipped with a warning.

### Telegram Behavior

`src.tg_client` contains a lot of product behavior. Treat it as intentional unless the task says otherwise.

- Single photo may include caption.
- Multiple photos are sent as media group, then text separately.
- Long photo captions are split into separate text messages.
- Some videos are sent as links instead of uploaded video files.
- Link button back to the VK post is part of expected behavior.
- Telegram 429 handling already retries once based on `retry_after`.

### Web UI

`src.web` is not just a static page. It:

- reads and writes the YAML config
- validates payloads with Pydantic
- can call VK APIs to resolve names and refresh avatars
- persists avatar cache in `data/avatars.json`

Be careful with any change that touches both `src.web` and `src.config`; they must stay compatible.

## Safe Change Guidelines

- Follow the existing Python style and keep code straightforward.
- Prefer extending current modules over introducing new abstractions unless complexity clearly demands it.
- Keep filesystem-based persistence working inside Docker with mounted volumes.
- Preserve UTF-8 handling for logs, YAML, and JSON files.
- Keep Russian user-facing messages consistent with the current project tone.
- Treat `config/config.example.yaml` as a developer convenience for manual runs from source; update it when schema changes affect that workflow.

## Risky Areas

Take extra care and verify changes when touching:

- `src.pipeline`: filtering, ordering, dedup, and `last_seen` advancement
- `src.tg_client`: caption limits, HTML escaping, media grouping, rate-limit retry
- `src.vk_client`: attachment parsing and repost source extraction
- `src.web`: config validation, avatar refresh, and API-facing schema changes
- `src.logger`: duplicate handlers, timezone behavior, log cleanup, and the compact file-log contract

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
CONFIG_PATH=data/config.yaml RUN_MODE=once python -m src.main
uvicorn src.web:app --host 0.0.0.0 --port 8222
docker compose up --build
```

If you change config or web validation, also check that:

- the web app can load config without crashing
- runtime-created directories and files still behave correctly

If you cannot run end-to-end API checks because real VK/Telegram tokens are unavailable, say that explicitly instead of guessing.

## Current Project Constraints

These constraints are part of the current product, not accidental limitations:

- no database
- one shared cron schedule for all communities
- minimal test coverage
- dedup and progress tracking stored in JSON
- each attachment type may result in separate Telegram messages
- scheduler and web UI run in the same container

Do not silently “improve” these constraints unless the task asks for a product or architecture change.

## Guidance For Future Agents

- Start by reading `README.md`, `src.main`, `src.web`, `src.pipeline`, and `src.config`.
- Prefer minimal, targeted changes over broad refactors.
- When behavior changes, update docs or config examples in the same task.
- Watch for hidden coupling between YAML schema, web validation, and runtime code.
- Be explicit about what you verified and what you could not verify.
