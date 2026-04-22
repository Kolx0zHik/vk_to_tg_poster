# VK → Telegram Poster

<p align="center">
  <img src="static/logo.png" alt="VK to Telegram Poster logo" width="180">
</p>

Сервис для автоматического переноса новых постов из сообществ ВКонтакте в Telegram-канал.

## Что умеет

- забирает новые посты из VK и публикует их в Telegram
- работает по расписанию
- настраивается через web-интерфейс
- поддерживает несколько сообществ
- защищает от дублей
- хранит состояние в обычных файлах, без базы данных

## Быстрый старт

Основной способ запуска:

```bash
docker compose up -d
```

После запуска:

1. Откройте `http://localhost:8222`
2. Заполните настройки в web-интерфейсе
3. Сохраните конфиг

Если `config/config.yaml` отсутствует, контейнер создаст его автоматически из `config/config.example.yaml`.

## Что нужно указать в интерфейсе

Минимально нужны:

- VK token
- Telegram bot token
- Telegram channel ID
- хотя бы одно VK-сообщество
- расписание запуска

## Где хранятся данные

- `config/` — конфигурация
- `data/` — служебные данные и кэш обработанных постов
- `logs/` — логи

Все эти директории подключаются как volumes и сохраняются между перезапусками контейнера.

## Варианты запуска

### Docker Compose

Основной пользовательский сценарий:

```bash
docker compose up -d
```

Файл [`docker-compose.yml`](./docker-compose.yml) использует готовый образ:

- `ghcr.io/kolx0zhik/vk_to_tg_poster:latest`

### Сборка из исходников

Если хотите менять код и собирать проект самостоятельно:

```bash
docker compose -f docker-compose.dev.yml up -d --build
```

Файл [`docker-compose.dev.yml`](./docker-compose.dev.yml) собирает образ из текущего репозитория.

### Dockerfile и docker run

Если нужен запуск без Compose:

```bash
docker build -t vk_to_tg_poster .
docker run -d \
  --name vk_to_tg_poster \
  -p 8222:8222 \
  -v "$(pwd)/config:/app/config" \
  -v "$(pwd)/logs:/app/logs" \
  -v "$(pwd)/data:/app/data" \
  vk_to_tg_poster
```

## Обновление

Если вы используете готовый образ:

```bash
docker compose pull
docker compose up -d
```

Если вы собираете проект из исходников:

```bash
docker compose -f docker-compose.dev.yml up -d --build
```

## Настройка через YAML

Основной сценарий — настройка через web-интерфейс.

Если нужно, конфиг можно редактировать вручную в файле:

- `config/config.yaml`

Если файла еще нет, он будет создан автоматически при первом запуске контейнера.

## Структура проекта

- `src/main.py` — запуск планировщика
- `src/web.py` — web-интерфейс и API конфигурации
- `src/pipeline.py` — основной пайплайн обработки постов
- `src/config.py` — загрузка и сохранение конфигурации
- `docker-compose.yml` — запуск готового образа
- `docker-compose.dev.yml` — локальная сборка из исходников

## Важно

- проект не использует базу данных
- состояние хранится в файлах
- планировщик и web-интерфейс работают в одном контейнере
