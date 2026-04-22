# VK → Telegram АвтоРепостер

![VK → Telegram Poster logo](static/logo.png)

Простой сервис, который по cron-расписанию читает посты из сообществ ВКонтакте и отправляет их в Telegram-канал. Без базы данных, конфигурация в YAML, защита от дублей через JSON-кеш.

## Быстрый старт
1. Скопируйте пример конфига и поправьте под себя:
   ```bash
   cp config/config.example.yaml config/config.yaml
   ```
2. Задайте переменные окружения (можно через `.env`):
   ```env
   VK_API_TOKEN=your_vk_service_token

vkhost.github.io - тут получаем access token

   TELEGRAM_BOT_TOKEN=your_telegram_bot_token
   ```
   При желании токены можно прописать в `config/config.yaml`.
3. Запустите через Docker Compose (единый сервис: постинг + веб-UI):
   ```bash
   docker compose up --build
   ```
   Интерфейс доступен на `http://localhost:8222` по умолчанию. Планировщик крутится в фоне в том же контейнере. Для одиночного прогона планировщика задайте `RUN_MODE=once`.

## Структура
- `src/` — исходный код сервиса.
- `entrypoint.sh` — запускает планировщик и веб-UI в одном контейнере.
- `config/config.yaml` — настройки (cron, группы, токены, логирование).
- `data/cache.json` — кеш опубликованных постов (создаётся автоматически).
- `logs/poster.log` — файл логов с ротацией.

## Настройки
Ключевые параметры в `config/config.yaml`:
- `general.cron` — расписание (формат cron).
- `general.posts_limit` — сколько последних постов забирать с `wall.get`.
- `general.cache_file`, `general.log_file`, `general.log_level`, `general.log_retention_days`, ротация логов.
- `general.blocked_keywords` — стоп-слова (если содержатся в тексте/заголовках вложений, пост пропускается).
- `general.refresh_avatars` — использовать кеш аватаров и не дёргать VK лишний раз.
- `vk.token` — токен сервиса ВК (или `VK_API_TOKEN`).
- `telegram.bot_token`, `telegram.channel_id` — бот и канал (или `TELEGRAM_BOT_TOKEN`).
- `communities[]` — список сообществ с флагом `active` и разрешёнными `content_types`.

Логирование по умолчанию настроено на компактный операционный лог в файле:
- в `logs/poster.log` пишутся краткие `INFO`/`ERROR` записи без `DEBUG`-шума и без traceback
- по каждому сообществу пишется одна summary-строка за прогон
- подробную отладку имеет смысл включать только временно

## Публикация образа
Workflow `.github/workflows/publish.yml` работает в двух режимах:
- push в `main` публикует `ghcr.io/kolx0zhik/vk_to_tg_poster:latest`
- ручной запуск workflow позволяет указать отдельный `image_tag` и выпустить тестовый образ, не затрагивая `latest`

Это удобно для проверки изменений перед merge в `main`.

## Разработка локально
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
CONFIG_PATH=config/config.yaml RUN_MODE=once python -m src.main
```
