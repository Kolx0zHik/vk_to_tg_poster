# VK → Telegram Poster

<p align="center">
  <img src="static/logo.png" alt="VK to Telegram Poster logo" width="180">
</p>

Сервис для автоматического переноса новых постов из сообществ ВКонтакте в Telegram-канал.

Проект запускается в одном контейнере и делает сразу две вещи:

- в фоне крутит планировщик публикаций
- поднимает веб-интерфейс для настройки `config.yaml`

Основной способ запуска: `docker compose`.

## Что делает проект

`VK → Telegram Poster` регулярно проверяет указанные VK-сообщества, отбирает новые записи и публикует их в Telegram.

Поддерживаются:

- текстовые посты
- фото
- видео
- аудио
- ссылки
- несколько сообществ в одной конфигурации
- фильтрация по стоп-словам
- защита от дублей
- хранение состояния без базы данных

Сервис хранит рабочее состояние в файлах:

- `config/config.yaml` — настройки
- `data/cache.json` — кэш уже обработанных постов и информация о том, до какого места сервис уже просмотрел записи в каждом сообществе
- `logs/poster.log` — логи

## Как это работает

На старте контейнера:

1. Проверяется наличие `config/config.yaml`
2. Если файла нет, он создается из `config/config.example.yaml`
3. Запускается планировщик `python -m src.main`
4. Поднимается веб-интерфейс `uvicorn src.web:app`

По умолчанию веб-интерфейс доступен на `http://localhost:8222`.

## Быстрый старт через Docker Compose

Это основной и рекомендуемый способ запуска.

### 1. Клонируйте репозиторий

```bash
git clone https://github.com/kolx0zhik/vk_to_tg_poster.git
cd vk_to_tg_poster
```

### 2. Подготовьте конфиг

Если хотите настроить все заранее вручную:

```bash
cp config/config.example.yaml config/config.yaml
```

Если не сделать этого шага, контейнер сам создаст `config/config.yaml` из примера при первом запуске.

### 3. Укажите токены и канал

Для стандартного запуска через текущий `docker-compose.yml` проще всего записать токены прямо в `config/config.yaml`.

Важно понимать:

- приложение поддерживает `VK_API_TOKEN` и `TELEGRAM_BOT_TOKEN`
- но текущий `docker-compose.yml` по умолчанию прокидывает в контейнер только `TZ`
- поэтому при запуске без правок Compose-файла надежнее хранить токены в YAML-конфиге

Если хотите использовать `.env`, в текущем виде его имеет смысл использовать в первую очередь для `TZ`.

Пример:

```env
TZ=Europe/Moscow
```

### 4. Отредактируйте `config/config.yaml`

Минимальный пример:

```yaml
general:
  cron: "*/15 * * * *"
  vk_api_version: "5.199"
  posts_limit: 10
  cache_file: data/cache.json
  log_file: logs/poster.log
  log_level: INFO
  log_rotation:
    max_bytes: 10485760
    backup_count: 5
  blocked_keywords: []
  refresh_avatars: true
  log_retention_days: 2

vk:
  token: "vk_service_or_user_token"

telegram:
  channel_id: "@your_channel"
  bot_token: "1234567890:telegram_bot_token"

communities:
  - id: "club123456789"
    name: "Мое сообщество"
    active: true
    content_types:
      text: true
      photo: true
      video: true
      audio: true
      link: true
```

Важно:

- `vk.token` может быть пустым, если используется `VK_API_TOKEN`
- `telegram.bot_token` может быть пустым, если используется `TELEGRAM_BOT_TOKEN`
- `telegram.channel_id` нужно задать обязательно
- `communities[].id` можно указывать как `club123`, `public123`, `-123`, `id123` или `https://vk.com/...`

Если хотите именно env-переменные в Docker Compose, добавьте их в секцию `environment` сервиса `vk2tg`.

### 5. Запустите сервис

```bash
docker compose up -d --build
```

После запуска будут доступны:

- веб-интерфейс: `http://localhost:8222`
- API конфигурации: `http://localhost:8222/api/config`

### 6. Посмотрите логи

```bash
docker compose logs -f
```

Остановить сервис:

```bash
docker compose down
```

## Что нужно подготовить перед запуском

### Telegram

Нужно:

- создать бота через [@BotFather](https://t.me/BotFather)
- добавить бота в канал
- выдать ему права администратора
- указать `telegram.channel_id`

Обычно `channel_id` — это:

- `@channel_name`
- или числовой идентификатор канала

### VK

Нужен токен VK API, с которым приложение сможет читать посты публичных страниц или сообществ.

Токен можно передать:

- через `VK_API_TOKEN`
- или через `vk.token` в `config/config.yaml`

## Веб-интерфейс

Веб-интерфейс нужен не только для просмотра.

Он умеет:

- читать текущий YAML-конфиг
- валидировать настройки перед сохранением
- редактировать список сообществ
- обновлять данные по сообществам через VK API
- кэшировать аватары в `data/avatars.json`

Если `config/config.yaml` отсутствует или поврежден, интерфейс пытается опереться на `config/config.example.yaml`.

## Режимы запуска

По умолчанию контейнер работает в режиме `scheduled`, то есть запускает публикацию по cron-расписанию из конфига.

Поддерживаются режимы:

- `RUN_MODE=scheduled` — обычная работа по расписанию
- `RUN_MODE=once` — один однократный запуск без цикла

Пример разового запуска через Compose:

```bash
docker compose run --rm -e RUN_MODE=once vk2tg
```

## Переменные окружения

Основные переменные:

- `CONFIG_PATH` — путь к конфигу, по умолчанию `config/config.yaml`
- `RUN_MODE` — `scheduled` или `once`
- `PORT` — порт веб-интерфейса, по умолчанию `8222`
- `TZ` — часовой пояс контейнера, по умолчанию `Europe/Moscow`
- `VK_API_TOKEN` — токен VK API, имеет приоритет над `vk.token`
- `TELEGRAM_BOT_TOKEN` — токен Telegram-бота, имеет приоритет над `telegram.bot_token`

## Docker Compose: что именно запускается

Файл [`docker-compose.yml`](./docker-compose.yml) поднимает один сервис `vk2tg`, который:

- собирается из локального `Dockerfile`
- пробрасывает порт `8222`
- монтирует директории `config`, `logs` и `data`
- перезапускается автоматически через `restart: unless-stopped`

Это значит, что ваши настройки, кэш и логи не теряются при пересоздании контейнера.

## Альтернативный запуск через Dockerfile

Если `docker compose` не нужен, образ можно собрать и запустить вручную.

### Сборка образа

```bash
docker build -t vk_to_tg_poster .
```

### Запуск контейнера

```bash
docker run -d \
  --name vk_to_tg_poster \
  -p 8222:8222 \
  -e TZ=Europe/Moscow \
  -e VK_API_TOKEN=vk_service_or_user_token \
  -e TELEGRAM_BOT_TOKEN=1234567890:telegram_bot_token \
  -v "$(pwd)/config:/app/config" \
  -v "$(pwd)/logs:/app/logs" \
  -v "$(pwd)/data:/app/data" \
  vk_to_tg_poster
```

Но для этого проекта предпочтителен именно `docker compose`: так проще управлять томами, обновлениями и перезапуском.

## Обновление

```bash
git pull
docker compose up -d --build
```

## Структура проекта

- `src/main.py` — планировщик и точка входа фонового процесса
- `src/web.py` — веб-интерфейс и API конфигурации
- `src/pipeline.py` — основной пайплайн обработки постов
- `src/vk_client.py` — работа с VK API
- `src/tg_client.py` — отправка сообщений в Telegram
- `src/config.py` — парсинг и сохранение YAML-конфига
- `src/cache.py` — кэш дублей и `last_seen`
- `config/config.example.yaml` — пример конфига
- `entrypoint.sh` — запуск планировщика и веб-интерфейса в одном контейнере

## Полезно знать

- Проект не использует базу данных
- Все состояние хранится в файлах
- Новые посты публикуются в правильном порядке: от старых к новым
- Дубли отсеиваются по кэшу
- Если токены или канал не заданы, планировщик не падает, а пропускает запуск с предупреждением в логах

## Локальный запуск без Docker

Этот вариант не основной, но возможен для разработки:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp config/config.example.yaml config/config.yaml
CONFIG_PATH=config/config.yaml RUN_MODE=once python -m src.main
uvicorn src.web:app --host 0.0.0.0 --port 8222
```

## Публикуемый образ

В репозитории есть workflow публикации контейнера в GHCR:

- образ публикуется как `ghcr.io/kolx0zhik/vk_to_tg_poster:latest`

Если вы просто хотите пользоваться проектом локально, достаточно `docker compose up -d --build`.
