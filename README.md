# RAMIX MUSIC

FastAPI-сайт и Telegram-бот уведомлений запускаются одной командой.

## Локальный запуск

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python run.py
```

Сайт: `http://127.0.0.1:8000`  
Проверка: `http://127.0.0.1:8000/health`

## Telegram-бот

Пользователь открывает `@ramixmusicbot`, подключает аккаунт по логину или email и паролю, после чего получает уведомления об изменениях релизов и других событиях аккаунта.

Пароль используется только для проверки входа. Сообщение с паролем удаляется после проверки и не сохраняется.

Основные команды:

```text
/start
/connect
/disconnect
/cancel
```

## Настройка `.env`

Файл `.env` используется только локально и уже добавлен в `.gitignore`. Для Railway значения нужно перенести в раздел Variables.

```env
SESSION_SECRET=длинная-случайная-строка
ADMIN_EMAILS=admin@example.com
BOT_ENABLED=true
BOT_TOKEN=токен_бота
BOT_USERNAME=ramixmusicbot
SITE_URL=http://127.0.0.1:8000
DATA_DIR=./data
```

## Railway

1. Загрузите проект в GitHub.
2. Создайте Railway-сервис из репозитория.
3. Добавьте переменные из `.env.example` в Railway Variables.
4. Укажите публичный адрес сайта в `SITE_URL`.
5. Создайте Railway Volume с путём `/data`.
6. Установите `DATA_DIR=/data`.
7. Оставьте одну реплику сервиса.

Команда запуска уже указана в `railway.toml` и `Procfile`:

```text
python run.py
```

## Данные

SQLite, обложки, аудиофайлы и импорты сохраняются в каталоге `DATA_DIR`. Миграции применяются автоматически при запуске и не пересоздают существующую базу.
