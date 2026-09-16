# Архитектура

Документ описывает, как сервис устроен сейчас (версия 1.1.8). Принятые решения и их причины — в
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
| `src/pipeline.py` | рабочий процесс публикации | `process_communities`, `_resolve_owner_id`, `_fetch_recent`, `_record_fetched`, `_publish_pending`, `_apply_backfill` |
| `src/vk_client.py` | VK API: посты, разбор вложений, разрешение screen name | `VKClient.fetch_posts`, `resolve_screen_name`, троттлинг `VK_REQUEST_INTERVAL=0.34`, ретраи `VK_MAX_RETRIES=2` |
| `src/tg_client.py` | доставка в Telegram | `TelegramClient.send_post` и `send_text/photo/video/audio/media_group/link` |
| `src/cache.py` | состояние публикаций (JSON, schema v2) | `Cache.record_post`, `pending_posts`, `mark_published/skipped/failed`, `set_baseline`, `get/set_owner_id` |
| `src/backfill.py` | заявки на дозаливку и возобновление | `BackfillRequests`, `compute_baseline`, `requests_path_for` |
| `src/config.py` | схема и (де)сериализация YAML | `load_config`, `parse_config_dict`, `config_to_dict`, `save_config_dict`, `ConfigError` |
| `src/web.py` | веб-панель и API | эндпоинты ниже, `_load_ui_config`, `_fetch_vk_info`, `_normalize_owner_id` |
| `src/vk_ids.py` | нормализация VK-ссылок и id без сети | `normalize_community_key`, `parse_owner_id`, `normalize_display_id` |
| `src/logger.py` | логи, маскирование секретов, retention | `configure_logging`, `redact_secrets`, `RedactingFormatter`, `CompactFileFormatter` |
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
7. Итоговая строка `info`: `fetched/new/published/known/blocked/skipped_by_type/failed/pending/backfill`.

Инварианты (не ломать):

- новые посты забираются «сверху», публикуются «снизу» (старые первыми);
- приоритет дедупликации — оригинал репоста (`copy_history` → `source_owner_id/source_post_id`);
- baseline/`last_seen` сдвигается даже для пропущенных постов, иначе цикл зациклится;
- отсутствие токенов/канала не роняет планировщик — запуск пропускается с предупреждением.

## 4. Доставка в Telegram (`src/tg_client.py`)

`send_post` раскладывает пост на сообщения по типам вложений:

| Случай | Что отправляется |
|---|---|
| ровно 1 фото | `sendPhoto` с caption (текст, урезанный до `CAPTION_LIMIT=1024`) + кнопка «Открыть пост в VK» |
| >1 фото | `sendMediaGroup` без caption, затем текст отдельным сообщением (обрезка до 1024) |
| видео с `.mp4/.mov/.mkv` | `sendVideo` по URL, caption — текст (обрезка до 1024) и/или статистика |
| прочее видео | текстовое сообщение со ссылкой на VK-плеер |
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
                  "payload": { "...Post..." } }
  },
  "communities": { "-123": { "baseline_date": 1757000000, "baseline_post_id": 456 } },
  "owner_ids": { "club123": { "owner_id": -123, "ts": 1757000000 } }
}
```

- ключ поста — `Post.dedup_key` = `<owner>_<post>` (для репоста — оригинал), **глобально**, не по сообществам;
- `baseline` = «всё, что ≤ (date, post_id), уже обработано» — основа и миграции, и дозаливки, и паузы;
- записи со статусом `published/skipped/dead` не содержат `payload` (payload хранится только у `pending`);
- миграция со старой схемы (`dedup`/`last_seen`) делается на лету в `Cache._migrate_legacy`, перед перезаписью создаётся `cache.json.v1.bak`;
- запись атомарная: `*.tmp` + `fsync` + `os.replace`;
- `owner_ids` живут 30 дней (`OWNER_ID_TTL`), чистятся при загрузке.

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
  log_rotation: { max_bytes: 10485760, backup_count: 5 }
  blocked_keywords: []          # фильтр по тексту и заголовкам вложений
  refresh_avatars: true
  log_retention_days: 2
vk: { token: "" }               # перекрывается VK_API_TOKEN
telegram: { channel_id: "", bot_token: "" }   # bot_token перекрывается TELEGRAM_BOT_TOKEN
communities:
  - id: "-232948281"            # числовой id, либо screen name — см. vk_ids
    name: "Суетологи"           # только для отображения
    active: true                # false = пауза, сообщество не опрашивается
    content_types: { text: true, photo: true, video: true, audio: false, link: true }
```

- запись конфига атомарная (`save_config_dict`), ошибки разбора — `ConfigError` с человекочитаемым текстом;
- `POST /api/config` нормализует `communities[].id` через `normalize_display_id` и отклоняет дубли;
- код по умолчанию считает `audio: true` (`ContentTypes`), интерфейс записывает `audio: false` — это осознанный
  разнобой, см. [STATE.md](./STATE.md).

## 7. HTTP API (`src/web.py`)

| Метод | Путь | Назначение | Тело/ответ |
|---|---|---|---|
| GET | `/` | веб-панель | `static/index.html` |
| GET | `/api/config` | конфиг для UI | `general`, `vk.token_set`, `telegram.{channel_id,bot_token_set}`, `communities[]`, `avatar_cache{}`, `version` |
| POST | `/api/config` | сохранить конфиг | `SaveRequest`; пустой токен = «оставить текущий», id нормализуются, дубли → 400 |
| GET | `/api/community_info?value=` | имя/аватар сообщества | `{id, name, photo}`; нужен VK-токен, кэш 24 ч, при сбое — `{id: value, name: "", photo: null}` |
| POST | `/api/backfill` | заявка на дозаливку | `{id, mode: none|posts|days, value}`; `posts` ≤ 100, `days` ≤ 365 |
| GET | `/api/logs?lines=N` | хвост лога | `{lines: [...]}` с замаскированными секретами |

Валидация — Pydantic-модели (`GeneralModel`, `CommunityModel`, `SaveRequest`, `BackfillModel`), они должны
оставаться синхронными с dataclass-схемой `src/config.py`.

## 8. Веб-панель (`static/`)

Одна страница, ванильный JS, состояние в объекте `state`:

- шапка: кнопки «Токены» и «Логи» — обе открывают модальные окна;
- «Основные настройки» — отдельная карточка с общей кнопкой «Сохранить»;
- «Отслеживаемые группы» — панель «список + настройки»: слева поиск и список (без ID и без ссылок),
  справа статус сегментом «Активно/На паузе», типы контента иконками, ссылка на сообщество в заголовке;
- «Только новые» из модалки добавления и снятие с паузы отправляют `POST /api/backfill`;
- добавление сообщества — модальное окно со живой проверкой ссылки, пресетами (посты 3/7/10/20/50,
  дни 1/2/3/5/7) и выбором типов контента; сохраняется сразу, без общей кнопки;
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
