# VK → Telegram АвтоРепостер

Простой сервис, который по cron-расписанию читает посты из сообществ ВКонтакте и отправляет их в Telegram-канал. Без базы данных, конфигурация в YAML, защита от дублей через JSON-кеш.

## Быстрый старт
1. Скопируйте пример конфига и поправьте под себя:
   ```bash
   cp config/config.example.yaml config/config.yaml
   ```
2. Задайте переменные окружения (можно через `.env`):
   ```env
   VK_API_TOKEN=your_vk_service_token
   TELEGRAM_BOT_TOKEN=your_telegram_bot_token
   ```
   При желании токены можно прописать в `config/config.yaml`.
3. Запустите через Docker Compose (единый сервис: постинг + веб-UI):
   ```bash
   docker compose up --build
   ```
   Интерфейс доступен на `http://localhost:8000` (конфиг/сообщества). Планировщик крутится в фоне в том же контейнере. Для одиночного прогона планировщика задайте `RUN_MODE=once`.

## Структура
- `src/` — исходный код сервиса.
- `entrypoint.sh` — запускает планировщик и веб-UI в одном контейнере.
- `config/config.yaml` — настройки (cron, группы, токены, логирование).
- `data/cache.json` — кеш опубликованных постов (создаётся автоматически).
- `logs/poster.log` — файл логов с ротацией.
- `docs/technical_spec.md` — уточнённое ТЗ.

## Настройки
Ключевые параметры в `config/config.yaml`:
- `general.cron` — расписание (формат cron).
- `general.posts_limit` — сколько последних постов забирать с `wall.get`.
- `general.cache_file`, `general.log_file`, `general.log_level`, ротация логов.
- `vk.token` — токен сервиса ВК (или `VK_API_TOKEN`).
- `telegram.bot_token`, `telegram.channel_id` — бот и канал (или `TELEGRAM_BOT_TOKEN`).
- `communities[]` — список сообществ с флагом `active` и разрешёнными `content_types`.

## Разработка локально
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
CONFIG_PATH=config/config.yaml RUN_MODE=once python -m src.main
```

## Ограничения MVP
- Каждое вложение отправляется отдельным сообщением.
- Один общий cron для всех сообществ.
- Минимальная валидация входных данных.
- После добавления нового сообщества отправляется последние `posts_limit` записей (с кнопкой перехода в VK); закреплённые посты публикуются один раз за счёт кеша.
- Видео без прямого файла отправляются ссылкой на пост/страницу VK, чтобы избежать ошибок Telegram 400.
- Дедупликация по исходному посту VK (учитывается `copy_history`): если один и тот же пост встречается в разных сообществах, он публикуется один раз.

Подробнее см. `docs/technical_spec.md`.
