# VK → Telegram Poster

<p align="center">
  <img src="static/logo.png" alt="VK to Telegram Poster logo" width="180">
</p>

Сервис переносит новые посты из сообществ ВКонтакте в Telegram-канал: работает по cron,
настраивается через веб-панель, состояние хранит в файлах (без базы данных).

**Версия:** 1.1.14 · **Точка входа для агентов:** [AGENTS.md](./AGENTS.md)

| Документ | О чём |
|---|---|
| [ARCHITECTURE.md](./ARCHITECTURE.md) | модули, потоки данных, форматы состояния, контракты API |
| [STATE.md](./STATE.md) | что сделано, что в работе, что сломано, чего не проверяли |
| [ROADMAP.md](./ROADMAP.md) | ближайшие и среднесрочные планы |
| [DECISIONS.md](./DECISIONS.md) | журнал решений (ADR) и почему так |
| [CHANGELOG.md](./CHANGELOG.md) | хронология версий |

## Что умеет

- забирает новые посты из VK (`wall.get`) и публикует их в Telegram-канал;
- несколько сообществ в одном канале, у каждого — свои типы контента (текст/фото/видео/аудио/ссылки);
- расписание — одно общее cron-выражение для всех сообществ;
- пауза для отдельного сообщества с возобновлением «только новые посты»;
- дозаливка при добавлении сообщества: только новые / последние N постов / за последние D дней;
- удаление сообщества из панели с подтверждением; состояние в кэше сохраняется, поэтому повторное добавление
  продолжает с прежнего baseline;
- защита от дублей (в т.ч. по оригиналу репоста и опционально семантическая проверка через ИИ), доставка упавших публикаций с повторами;
- настройка через веб-панель, секреты — только в `.env`;
- состояние и логи — обычные файлы, без БД.

## Стек

Python 3.11 · FastAPI + uvicorn · requests · PyYAML · croniter
Фронтенд: ванильный HTML/CSS/JS в `static/`, без сборки и npm.
Упаковка: multi-stage Docker, публикация образов в GHCR через GitHub Actions.

## Быстрый старт (готовый образ)

```bash
docker compose up -d
```

1. Открыть `http://localhost:8222`
2. Создать `data/.env` рядом с конфигом и прописать `VK_API_TOKEN`, `TELEGRAM_BOT_TOKEN` (и `LLM_API_KEY`, если включаете ИИ-проверку дублей)
3. В панели указать Telegram-канал, добавить хотя бы одно сообщество, нажать «Сохранить»
4. Перезапустить контейнер: секреты читаются из `.env` при старте

Если `data/config.yaml` отсутствует, контейнер создаст его с настройками по умолчанию (`entrypoint.sh`).

## Запуск из исходников (разработка)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp config/config.example.yaml config/config.yaml
cp .env.example config/.env     # секреты рядом с конфигом (см. CONFIG_PATH)
CONFIG_PATH=config/config.yaml RUN_MODE=once python -m src.main   # один цикл публикации
uvicorn src.web:app --host 0.0.0.0 --port 8222                     # веб-панель
```

`config/config.example.yaml` — пример конфига для ручного запуска из исходников.

## Переменные окружения и `.env`

Все секреты лежат в файле `.env`, расположенном рядом с `CONFIG_PATH` (в докере — монтируемый каталог `data`). Приложение само читает `KEY=VALUE` строки; уже заданные в окружении переменные имеют приоритет. Токены из `config.yaml` больше не читаются вообще — их нужно перенести в `.env`.

| Переменная | Где читается | Смысл |
|---|---|---|
| `CONFIG_PATH` | `src/main.py`, `src/web.py`, `entrypoint.sh` | путь к YAML-конфигу, по умолчанию `data/config.yaml` |
| `RUN_MODE` | `src/main.py`, `entrypoint.sh` | `scheduled` (по умолчанию) или `once` |
| `PORT` | `entrypoint.sh` | порт веб-панели, по умолчанию `8222` |
| `TZ` | `src/logger.py` | таймзона логов/расписания, по умолчанию `Europe/Moscow` |
| `VK_API_TOKEN` | `src/envfile.py`, `src/main.py`, `src/web.py` | VK API token (секрет) |
| `TELEGRAM_BOT_TOKEN` | `src/envfile.py`, `src/main.py` | токен Telegram-бота (секрет) |
| `LLM_API_KEY` | `src/envfile.py`, `src/pipeline.py` | ключ OpenAI-совместимого API для семантической проверки дублей |

Пример `.env` — в файле `.env.example`.

## Где лежат данные

| Путь | Что это |
|---|---|
| `data/config.yaml` | конфигурация (пишется и панелью, и вручную); секретов в нём нет |
| `data/.env` | секреты: `VK_API_TOKEN`, `TELEGRAM_BOT_TOKEN`, `LLM_API_KEY` (в git не попадает) |
| `data/cache.json` | состояние пайплайна: посты, baseline сообществ, кэш owner id |
| `data/backfill.json` | заявки на дозаливку/возобновление, их пишет веб и потребляет планировщик |
| `data/avatars.json` | кэш имён и аватаров сообществ |
| `data/logs/poster.log` | файловые логи (ротация + retention) |

Каталог `data/` монтируется как volume и переживает перезапуск контейнера; `.env` лежит там же, рядом с конфигом.

## Проверка изменений

```bash
python -m unittest discover -s tests    # 100 тестов
node --check static/script.js           # синтаксис фронтенда
```

## Структура репозитория

```
src/        код сервиса (см. ARCHITECTURE.md)
static/     веб-панель: index.html, script.js, style.css, logo.png
tests/      юнит-тесты (два файла)
config/     пример конфига для запуска из исходников
.env.example  пример файла секретов (копируется в data/.env)
data/       runtime-состояние (в git не попадает, кроме .gitignore)
.github/workflows/publish.yml   сборка образов в GHCR
Dockerfile, entrypoint.sh, docker-compose*.yml, VERSION
```

## Принципиальные ограничения

- нет базы данных и не планируется;
- одно общее расписание на все сообщества;
- планировщик и веб-панель работают в одном контейнере двумя процессами;
- тестовое покрытие точечное (юнит-тесты ядра), end-to-end прогонов нет;
- удаление сообщества не чистит его состояние в `data/cache.json` — это осознанно.

Полный список известных проблем и ограничений — в [STATE.md](./STATE.md).
