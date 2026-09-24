# Архитектура

Документ описывает, как сервис устроен сейчас (версия 1.1.14, ветка `feature/semantic-dedup`). Принятые решения и их причины — в
[DECISIONS.md](./DECISIONS.md); текущее состояние и известные проблемы — в [STATE.md](./STATE.md).

## 1. Общая схема

Один контейнер, два процесса (`entrypoint.sh`):

```
                    ┌──────────────── docker container ────────────────┐
   cron (внутри) ──▶│ python -m src.main      (src/main.py)           │
                    │   цикл: config → VK → фильтры → Telegram        │
                    │                                    │            │
                    │                                    ▼            │
   браузер ────────▶│ python -m uvicorn src.web:app  (src/web.py)     │
                    │   веб-панель + API конфига                      │
                    └───────────────┬──────────────┬───────────────────┘
                                    ▼              ▼
                            data/config.yaml   data/*.json (volume)
```

- `src.main` — планировщик: собственный цикл на `croniter` (cron из конфига перечитывается каждый тик).
- `src.web` — FastAPI: отдаёт `static/` и API; конфиг читает/пишет сам, состояние публикаций не трогает.
- Связь процессов — только через файлы. `data/cache.json` принадлежит планировщику, веб в него не пишет (см. `data/backfill.json`).

## 2. Модули

| Модуль | Ответственность | Ключевые сущности |
|---|---|---|
| `src/main.py` | запуск, режимы `once`/`scheduled`, цикл по cron | `run_job`, `run_with_scheduler`, `main` |
| `src/pipeline.py` | рабочий процесс публикации | `process_communities`, `_resolve_owner_id`, `_fetch_recent`, `_record_fetched`, `_publish_pending`, `_apply_backfill`, `_dedup_check` |
| `src/vk_client.py` | VK API: посты, разбор вложений, разрешение screen name | `VKClient.fetch_posts`, `resolve_screen_name`, троттлинг `VK_REQUEST_INTERVAL=0.34`, ретраи `VK_MAX_RETRIES=2` |
| `src/tg_client.py` | доставка в Telegram | `TelegramClient.send_post` и `send_text/photo/video/audio/media_group/link` |
| `src/cache.py` | состояние публикаций (JSON, schema v2) | `Cache.record_post`, `pending_posts`, `mark_published/skipped/failed`, `published_candidates`, `prune_published_text`, `set_baseline`, `get/set_owner_id` |
| `src/dedup.py` | семантическая проверка дублей через LLM | `SemanticDedup.check`, `DedupResult`, `DedupError` (OpenAI-совместимый `/chat/completions`) |
| `src/backfill.py` | заявки на дозаливку и возобновление | `BackfillRequests`, `compute_baseline`, `requests_path_for` |
| `src/config.py` | схема и (де)сериализация YAML | `load_config`, `parse_config_dict`, `config_to_dict`, `save_config_dict`, `ConfigError`, `SemanticDedupSettings`, `LLMSettings` |
| `src/envfile.py` | загрузка секретов из `.env` рядом с конфигом | `load_env_file`, `env_file_path`, `IGNORED_KEYS` (TZ/LLM_DEBUG_LOG игнорируются) |
| `src/web.py` | веб-панель и API | эндпоинты ниже, `_load_ui_config`, `_fetch_vk_info`, `_normalize_owner_id` |
| `src/vk_ids.py` | нормализация VK-ссылок и id без сети | `normalize_community_key`, `parse_owner_id`, `normalize_display_id` |
| `src/logger.py` | логи, маскирование секретов, retention, часовой пояс из конфига | `configure_logging`, `apply_timezone`, `redact_secrets`, `RedactingFormatter`, `CompactFileFormatter` |
| `src/models.py` | доменные модели | `Post` (`dedup_key`, `vk_link`), `Attachment` |
| `src/version.py` | версия из файла `VERSION` | `get_version` |

## 3. Поток публикации (`src/pipeline.py`)

Для каждого сообщества из конфига:

1. `community.active == False` → пропуск **до любых запросов к VK**, в лог `info` «на паузе».
2. `_resolve_owner_id` — из строки конфига в числовой owner id: локальный парсер (`parse_owner_id`) → кэш `owner_ids` → VK `utils.resolveScreenName` (negative для групп).
3. `_apply_backfill` — если в `data/backfill.json` есть заявка, догружает посты нужного окна, считает baseline и **перезаписывает baseline сообщества** (см. §5), затем удаляет заявку. Ошибка VK → заявка остаётся на следующий запуск.
4. `_fetch_recent` — страницы постов, максимум `MAX_FETCH_PAGES=5`, размер страницы `min(10, posts_limit)`, пока не поймает известные посты.
5. `_record_fetched` — посты в порядке «старые → новые» пишутся в кэш: `new` / `known` / `baseline` (пропущен как уже пройденный).
6. `_publish_pending` — публикация не более `posts_limit` постов за цикл, старые первыми; заблокированные словами и запрещёнными типами помечаются `skipped`; ошибки → `pending` с повтором, после `PENDING_MAX_ATTEMPTS=5` → `dead`.
   - при включённой семантической проверке (`general.semantic_dedup.enabled`) и заданном системном промпте (`llm.prompt`) каждый пост с непустым текстом сравнивается с пулом опубликованных постов за окно (`cache.published_candidates`). User-message строит код (`_build_user_prompt`, см. ADR-021): новый пост и пронумерованный список кандидатов `{date, text}` в пределах `CANDIDATES_CHAR_BUDGET`, где кандидаты ограничиваются по границам целых записей, а не сырым обрезанием JSON. Системный промпт берётся только из `llm.prompt` и содержит одни критерии дубля — контракт ответа (`{"is_duplicate", "reason", "matched"}`) задаёт код. `matched` — номер кандидата (1-based), пайплайн маппит его обратно на исходный пост для лога. Встроенного промпта по умолчанию нет: пустой `llm.prompt` полностью отключает проверку (с предупреждением в лог), посты публикуются как обычно. Вердикт «дубль» → `skipped` и счётчик `dedup_skipped`. Любая ошибка LLM — fail-open: пост публикуется как обычно.
7. Итоговая строка `info`: `fetched/new/published/known/blocked/skipped_by_type/dedup_skipped/failed/pending/backfill`.

Инварианты (не ломать):

- новые посты забираются «сверху», публикуются «снизу» (старые первыми);
- приоритет дедупликации — оригинал репоста (`copy_history` → `source_owner_id/source_post_id`);
- baseline/`last_seen` сдвигается даже для пропущенных постов, иначе цикл зациклится;
- отсутствие токенов/канала не роняет планировщик — запуск пропускается с предупреждением;
- семантическая проверка никогда не блокирует публикацию при сбое (fail-open) и не трогает посты без текста.

## 4. Доставка в Telegram (`src/tg_client.py`)

`send_post` раскладывает пост на сообщения по типам вложений:

| Случай | Что отправляется |
|---|---|
| ровно 1 фото | `sendPhoto` с caption (текст, урезанный до `CAPTION_LIMIT=1024`) + кнопка «Открыть пост в VK» |
| >1 фото | `sendMediaGroup` без caption, затем текст отдельным сообщением (обрезка до 1024) |
| видео | если VK отдаёт картинку `image`/`first_frame` — `sendPhoto` с превью, первой строкой `🎬 <название-ссылка>`, текстом поста и кнопкой; без картинки — текстовое сообщение с HTML-ссылкой |
| аудио с URL | `sendAudio`, caption обрезается до 1024; без URL — ссылка |
| ссылки | отдельные сообщения со ссылкой |
| текст | отдельным сообщением, если ещё не израсходован |

Длинный текст не разбивается на несколько сообщений, а обрезается так же, как caption: `_truncate_text`
режет тело до `CAPTION_LIMIT=1024` по границам абзаца → строки → предложения → слова и добавляет пометку
«Продолжение текста читайте в источнике». Предел один и тот же для caption и для `sendMessage`. Текст
сообщений всегда уходит в HTML-режиме (`_escape_html` у вызывающего), поэтому смещение дополнительно
отводится назад `_safe_offset`, чтобы не разорвать HTML-сущность или тег. При наличии статистики видео её
место резервируется заранее, кнопка «Открыть пост в VK» ставится на сообщение.

Фото скачиваются и загружаются файлом (`_download_media`, лимит `MAX_PHOTO_BYTES=10 МБ`) с откатом на
передачу по URL; видео и аудио уходят в Telegram по URL. Ответ 429 обрабатывается одним повтором по
`retry_after`.

## 5. Состояние (файлы в `data/`)

### `cache.json` (schema v2, принадлежит планировщику)

```json
{
  "meta": { "version": 2 },
  "posts": {
    "-123_456": { "status": "pending|published|skipped|dead", "owner_id": -123,
                  "post_id": 456, "date": 1757000000, "attempts": 0, "ts": 1757000001,
                  "payload": { "...Post..." } },
    "-123_457": { "status": "published", "owner_id": -123, "post_id": 457, "date": 1757000100,
                  "attempts": 0, "ts": 1757000101, "text": "текст поста для ИИ-проверки" }
  },
  "communities": { "-123": { "baseline_date": 1757000000, "baseline_post_id": 456 } },
  "owner_ids": { "club123": { "owner_id": -123, "ts": 1757000000 } },
  "archived": ["-100_1", "-100_2"]
}
```

- ключ поста — `Post.dedup_key` = `<owner>_<post>` (для репоста — оригинал), **глобально**, не по сообществам;
- `baseline` = «всё, что ≤ (date, post_id), уже обработано» — основа и миграции, и дозаливки, и паузы;
- записи со статусом `published` дополнительно хранят `text` (до `PublishedText.MAX_LEN=1000` символов) — это пул кандидатов для семантической проверки; `payload` остаётся только у `pending`, у `skipped/dead` его нет;
- **размер хранится ограниченным** (см. ADR-019): текст живёт в окне `window_days` (prune в начале каждого прогона, независимо от флага `semantic_dedup.enabled`) и не больше `Cache.TEXT_POOL_CAP=60` новейших постов; а терминальные `published/skipped/dead` старше `Cache.ARCHIVE_AFTER=30 дней` схлопываются в `archived` — список одних только ключей (tombstone). Глобальная память дедупликации сохраняется полностью: `archived` учитывается в `is_known`/`record_post`, повторно пост не уедет; `pending` не архивируется никогда;
- `published_candidates(since_ts, limit)` отдаёт опубликованные посты с текстом за окно (по умолчанию 20 новейших), `prune_published_text(window_days)` снимает текст у записей старше окна, не трогая ключи дедупликации; `_maintain()` (при загрузке) переносит устаревшие терминальные записи в `archived`;
- миграция со старой схемы (`dedup`/`last_seen`) делается на лету в `Cache._migrate_legacy`, перед перезаписью создаётся `cache.json.v1.bak`;
- запись атомарная: `*.tmp` + `fsync` + `os.replace`;
- `owner_ids` живут 30 дней (`OWNER_ID_TTL`), чистятся при загрузке.

### `.env` (принадлежит человеку, читается обоими процессами)

Секреты вне конфига: `VK_API_TOKEN`, `TELEGRAM_BOT_TOKEN`, `LLM_API_KEY`. Файл лежит рядом с `CONFIG_PATH`
(`env_file_path`), читается `load_env_file` при старте `src.main` и `src.web`; уже заданные переменные
окружения имеют приоритет. Токены из `config.yaml` не читаются вообще.

### `backfill.json` (принадлежит вебу, читает планировщик)

```json
{ "-123": { "mode": "none|posts|days", "value": 5, "ts": 1757000000 } }
```

`mode=none` — «только новые»: baseline ставится на самый свежий пост. Заявки старше 30 дней вычищаются.
Путь вычисляется как «рядом с `cache_file`» (`requests_path_for`) — так веб никогда не пишет `cache.json`.

### `avatars.json` (принадлежит вебу)

`{ "<key>": { "name": "...", "photo": "https://...", "fetched_at": 1757000000 } }`, где `key = normalize_display_id(value).lower()`.
TTL — 24 часа (`AVATAR_TTL_SECONDS`), обновляется флагом `general.refresh_avatars`.

## 6. Конфиг (`data/config.yaml`, схема в `src/config.py`)

```yaml
general:
  cron: "*/15 * * * *"          # одно расписание на все сообщества
  vk_api_version: "5.199"
  posts_limit: 10               # и размер страницы, и лимит публикаций за цикл
  cache_file: data/cache.json
  log_file: data/logs/poster.log
  log_level: INFO               # DEBUG/INFO/WARNING/ERROR/CRITICAL
  timezone: Europe/Moscow       # IANA-таймзона логов и расписания (была env TZ)
  log_rotation: { max_bytes: 10485760, backup_count: 5 }
  blocked_keywords: []          # фильтр по тексту и заголовкам вложений
  refresh_avatars: true
  log_retention_days: 2
  semantic_dedup:
    enabled: false              # ИИ-проверка дублей перед публикацией
    window_days: 4              # окно поиска кандидатов
    debug_log: false            # временный тумблер: писать вердикт «не дубль» по каждому посту
llm:                            # не секрет: base_url, модель и промпт задаются из панели
  base_url: "https://openrouter.ai/api/v1"
  model: "inclusionai/ling-3.0-flash-sante:free"
  prompt: ""                    # обязательный системный промпт; без него проверка выключена
vk: {}                          # секретов в конфиге нет
telegram: { channel_id: "" }    # токен бота — только в .env
communities:
  - id: "-232948281"            # числовой id, либо screen name — см. vk_ids
    name: "Суетологи"           # только для отображения
    active: true                # false = пауза, сообщество не опрашивается
    content_types: { text: true, photo: true, video: true, audio: false, link: true }
```

- запись конфига атомарная (`save_config_dict`), ошибки разбора — `ConfigError` с человекочитаемым текстом;
- секретов в конфиге нет: `VK_API_TOKEN`, `TELEGRAM_BOT_TOKEN`, `LLM_API_KEY` читаются только из `.env`/окружения;
- в `.env` держатся только секреты: все настройки (таймзона `general.timezone`, отладочный `general.semantic_dedup.debug_log`)
  живут здесь и правятся из панели; legacy-переменные `TZ` и `LLM_DEBUG_LOG` больше не читаются (`envfile.IGNORED_KEYS`),
  часовой пояс применяется через `apply_timezone` (`src/logger.py`) при старте и перечитывается в цикле планировщика;
- ключ LLM (`LLM_API_KEY`) в панели не редактируется — только `base_url`, `model` и системный промпт (`prompt`);
- проверка дублей выключена по умолчанию и включается тумблером в модалке «ИИ-проверка»; системный промпт обязателен:
  пустой `llm.prompt` при включённом тумблере отключает проверку (pipeline пишет предупреждение, `POST /api/config`
  отвечает 400); при включении без ключа/URL/модели проверка пропускается (fail-open);
- `POST /api/config` нормализует `communities[].id` через `normalize_display_id` и отклоняет дубли;
- код по умолчанию считает `audio: true` (`ContentTypes`), интерфейс записывает `audio: false` — это осознанный
  разнобой, см. [STATE.md](./STATE.md).

## 7. HTTP API (`src/web.py`)

| Метод | Путь | Назначение | Тело/ответ |
|---|---|---|---|
| GET | `/` | веб-панель | `static/index.html` |
| GET | `/api/config` | конфиг для UI | `general` (в т.ч. `semantic_dedup`), `llm.{base_url,model,prompt}`, `vk.token_set`, `telegram.{channel_id,bot_token_set}`, `llm_api_key_set`, `communities[]`, `avatar_cache{}`, `version` |
| POST | `/api/config` | сохранить конфиг | `SaveRequest` (`general` — в т.ч. `timezone` и `semantic_dedup.debug_log`, `telegram.channel_id`, `llm`, `communities`); секреты не принимаются, id нормализуются, дубли → 400, неизвестная таймзона → 422 |
| DELETE | `/api/community/{community_id}` | удалить сообщество из конфига | id нормализуется (`_normalize_owner_id`), запись удаляется и конфиг сохраняется сразу; 404 — сообщества нет, 400 — ошибка разбора; ответ `{ok, deleted_id}`; `cache.json` не трогается |
| GET | `/api/community_info?value=` | имя/аватар сообщества | `{id, name, photo}`; нужен VK-токен, кэш 24 ч, при сбое — `{id: value, name: "", photo: null}` |
| POST | `/api/backfill` | заявка на дозаливку | `{id, mode: none|posts|days, value}`; `posts` ≤ 100, `days` ≤ 365 |
| GET | `/api/logs?lines=N` | хвост лога | `{lines: [...]}` с замаскированными секретами |

Валидация — Pydantic-модели (`GeneralModel`, `SemanticDedupModel`, `LLMModel`, `CommunityModel`, `SaveRequest`, `BackfillModel`), они должны
оставаться синхронными с dataclass-схемой `src/config.py`.

## 8. Веб-панель (`static/`)

Одна страница, ванильный JS, состояние в объекте `state`:

- шапка: кнопки «ИИ-проверка» и «Логи» — обе открывают модальные окна; модалки «Токены» больше нет;
- «ИИ-проверка» — тумблер включения, `base_url`, `model`, окно сравнения (`window_days`), тумблер подробного
  лога (`debug_log`) и системный промпт (`prompt`, обязателен при включённой проверке: без него проверка не
  работает); подсказка, что ключ `LLM_API_KEY` задаётся в `.env`;
- «Основные настройки» — отдельная карточка с общей кнопкой «Сохранить»; там же поле «Telegram канал» и
  поле «Часовой пояс» (`timezone`);
- «Отслеживаемые группы» — панель «список + настройки»: слева поиск и список (без ID и без ссылок),
  справа статус сегментом «Активно/На паузе», типы контента иконками, ссылка на сообщество в заголовке;
- «Только новые» из модалки добавления и снятие с паузы отправляют `POST /api/backfill`;
- добавление сообщества — модальное окно со живой проверкой ссылки, пресетами (посты 3/7/10/20/50,
  дни 1/2/3/5/7) и выбором типов контента; сохраняется сразу, без общей кнопки;
- удаление сообщества — модалка подтверждения в том же стиле с предупреждением «Это действие нельзя
  отменить»; подтверждение вызывает `DELETE /api/community/{id}`, затем перезагружает конфиг; закрывается по
  Esc и клику по фону, как остальные модалки;
- утилита `.hidden` объявлена как `display: none !important` — иначе `display` из компонентных правил
  перебивает скрытие (грабли, на которые уже наступали).

## 9. Сборка и публикация

`.github/workflows/publish.yml`, образ `ghcr.io/kolx0zhik/vk_to_tg_poster`:

| Событие | Теги образа |
|---|---|
| push в `main` | `latest`, `sha-<short>` |
| push в `feature/**` или `fixes/**` | `test`, `sha-<short>` |
| git-тег `v*` | `vX.Y.Z` |
| ручной запуск | указанный вручную тег |

Dockerfile многоступенчатый: зависимости ставятся в builder и копируются в `python:3.11-slim`, код и
`VERSION` копируются в `/app`, запуск через `entrypoint.sh`.

## 10. Подводные камни

- Веб не должен писать `cache.json`: планировщик перезаписывает его постоянно, любая запись «извне»
  может откатить прогресс и привести к повторной публикации. Только `backfill.json`.
- `record_post` дедуплицирует **глобально**: один и тот же пост из двух сообществ будет опубликован
  один раз (второе увидит его как `known`).
- Удаление сообщества не чистит `posts`/`communities` в кэше; повторное добавление продолжит с
  сохранённого baseline (посты из прошлого заново не уедут).
- Ограничения Telegram: и caption, и текст сообщения обрезаются до 1024 с пометкой о продолжении;
  альбом больше 10 медиа по-прежнему не разбивается — см. STATE.
- Секреты маскируются только на уровне форматтеров логов и `/api/logs`; в файле, который писался до
  1.0.0, токен мог остаться — см. [STATE.md](./STATE.md).
- Секреты живут только в `.env`/окружении; не добавляйте `token`/`bot_token` обратно в `config.yaml` и не
  пишите их из веба. Приложение читает `.env` рядом с конфигом, явные env-переменные важнее файла.
  В `.env` — только секреты: настройки (таймзона, отладочный тумблер ИИ-проверки) держим в `config.yaml`,
  legacy-переменные `TZ`/`LLM_DEBUG_LOG` молча игнорируются при загрузке.
- ИИ-проверка дублей — совещательная: она только помечает пост `skipped`. При недоступном LLM, пустых
  `base_url`/`model`, пустом `llm.prompt` или отсутствии `LLM_API_KEY` публикация продолжается как обычно
  (fail-open; пустой промпт отключает проверку целиком). Пул
  кандидатов берётся из `text`, сохранённого при публикации; текст старше `window_days` вычищается в начале
  каждого прогона (и при выключенной проверке), а всего текстов хранится не больше `TEXT_POOL_CAP` —
  подробности и про `archived`-tombstones см. §5 и ADR-019.
  - Временный тумблер `general.semantic_dedup.debug_log` (настраивается в панели; раньше был env `LLM_DEBUG_LOG`):
    на INFO пишет вердикт «не дубль»
    с причиной и факт пустого пула кандидатов по каждому посту; дубли логируются единственной строкой
    `_publish_pending` независимо от тумблера, а сырой ответ LLM пишется только когда он не разбирается
    как JSON (до 500 символов). Без тумблера — тишина, как раньше.
