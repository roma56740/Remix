from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import datetime, timezone

Migration = tuple[int, str, Callable[[sqlite3.Connection], None]]


def _columns(connection: sqlite3.Connection, table_name: str) -> set[str]:
    rows = connection.execute(f"PRAGMA table_info({table_name})").fetchall()
    return {str(row[1]) for row in rows}


def _add_column_if_missing(
    connection: sqlite3.Connection,
    table_name: str,
    column_name: str,
    definition: str,
) -> None:
    if column_name not in _columns(connection, table_name):
        connection.execute(
            f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}"
        )


def migration_001_users(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            email TEXT NOT NULL UNIQUE COLLATE NOCASE,
            password_hash TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )


def migration_002_user_profile_fields(connection: sqlite3.Connection) -> None:
    _add_column_if_missing(connection, "users", "username", "TEXT")
    _add_column_if_missing(connection, "users", "balance", "INTEGER NOT NULL DEFAULT 0")
    _add_column_if_missing(
        connection, "users", "pending_balance", "INTEGER NOT NULL DEFAULT 0"
    )
    _add_column_if_missing(connection, "users", "last_payout_at", "TEXT")
    _add_column_if_missing(connection, "users", "telegram_id", "TEXT")
    _add_column_if_missing(
        connection, "users", "telegram_connected", "INTEGER NOT NULL DEFAULT 0"
    )

    rows = connection.execute(
        "SELECT id, email, username FROM users ORDER BY id"
    ).fetchall()
    for row in rows:
        if row[2]:
            continue
        local_part = str(row[1]).split("@", 1)[0].strip().lower() or f"artist_{row[0]}"
        safe = "".join(char if char.isalnum() or char == "_" else "_" for char in local_part)
        safe = safe.strip("_") or f"artist_{row[0]}"
        candidate = safe[:32]
        suffix = 1
        while connection.execute(
            "SELECT 1 FROM users WHERE username = ? COLLATE NOCASE AND id != ?",
            (candidate, row[0]),
        ).fetchone():
            suffix += 1
            suffix_text = f"_{suffix}"
            candidate = f"{safe[:32 - len(suffix_text)]}{suffix_text}"
        connection.execute(
            "UPDATE users SET username = ? WHERE id = ?", (candidate, row[0])
        )

    connection.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_username ON users(username COLLATE NOCASE)"
    )


def migration_003_platform_tables(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS releases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            artist_name TEXT NOT NULL,
            release_type TEXT NOT NULL DEFAULT 'single',
            status TEXT NOT NULL DEFAULT 'draft',
            upc TEXT,
            cover_path TEXT,
            release_date TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS listens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            release_id INTEGER,
            listen_date TEXT NOT NULL,
            count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY (release_id) REFERENCES releases(id) ON DELETE SET NULL
        );

        CREATE TABLE IF NOT EXISTS balance_transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            release_id INTEGER,
            transaction_type TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'completed',
            amount INTEGER,
            title TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY (release_id) REFERENCES releases(id) ON DELETE SET NULL
        );

        CREATE TABLE IF NOT EXISTS service_news (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            body TEXT NOT NULL,
            published_at TEXT NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 1,
            sort_order INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS pitching_platforms (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            slug TEXT NOT NULL UNIQUE,
            logo_path TEXT,
            support_url TEXT,
            is_active INTEGER NOT NULL DEFAULT 1,
            sort_order INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS pitching_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            release_id INTEGER NOT NULL,
            platform_id INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'draft',
            submitted_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY (release_id) REFERENCES releases(id) ON DELETE CASCADE,
            FOREIGN KEY (platform_id) REFERENCES pitching_platforms(id) ON DELETE CASCADE
        );
        """
    )


def migration_004_indexes(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE INDEX IF NOT EXISTS idx_releases_user_status
            ON releases(user_id, status);
        CREATE INDEX IF NOT EXISTS idx_releases_user_updated
            ON releases(user_id, updated_at DESC);
        CREATE INDEX IF NOT EXISTS idx_listens_user_date
            ON listens(user_id, listen_date);
        CREATE INDEX IF NOT EXISTS idx_balance_transactions_user_date
            ON balance_transactions(user_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_pitching_requests_user_status
            ON pitching_requests(user_id, status);
        CREATE INDEX IF NOT EXISTS idx_service_news_active_date
            ON service_news(is_active, published_at DESC);
        """
    )


def migration_005_global_seed(connection: sqlite3.Connection) -> None:
    """Kept as an empty historical migration. Real data is managed by the platform."""


def migration_006_remove_demo_data(connection: sqlite3.Connection) -> None:
    """Remove only demo records introduced by the previous dashboard prototype."""
    demo_covers = (
        "/static/img/dashboard/cover-tears.png",
        "/static/img/dashboard/cover-heavy-luxury.png",
        "/static/img/dashboard/cover-digital-noise.png",
    )
    placeholders = ",".join("?" for _ in demo_covers)
    demo_release_rows = connection.execute(
        f"""
        SELECT id, user_id
        FROM releases
        WHERE cover_path IN ({placeholders})
        """,
        demo_covers,
    ).fetchall()
    demo_release_ids = [int(row[0]) for row in demo_release_rows]
    demo_user_ids = {int(row[1]) for row in demo_release_rows}

    if demo_release_ids:
        id_placeholders = ",".join("?" for _ in demo_release_ids)
        connection.execute(
            f"DELETE FROM listens WHERE release_id IN ({id_placeholders})",
            demo_release_ids,
        )
        connection.execute(
            f"DELETE FROM balance_transactions WHERE release_id IN ({id_placeholders})",
            demo_release_ids,
        )
        connection.execute(
            f"DELETE FROM releases WHERE id IN ({id_placeholders})",
            demo_release_ids,
        )

    for user_id in demo_user_ids:
        connection.execute(
            """
            DELETE FROM releases
            WHERE user_id = ?
              AND artist_name = 'Ramix Artist'
              AND title GLOB 'Archive Release [0-9][0-9]'
              AND upc IS NULL
              AND cover_path IS NULL
            """,
            (user_id,),
        )
        connection.execute(
            """
            UPDATE users
            SET balance = 0, pending_balance = 0, last_payout_at = NULL
            WHERE id = ? AND balance = 8420 AND pending_balance = 2180
            """,
            (user_id,),
        )

    connection.execute(
        """
        DELETE FROM service_news
        WHERE title IN (
            'Обновлены правила обложек',
            'Появился раздел питчинга',
            'Скоро: синхронизация текста'
        )
        AND body IN (
            'Добавьте корректное изображение без лишних логотипов и запрещённых элементов.',
            'Собрали ссылки на кабинеты площадок для продвижения будущих релизов.',
            'Инструмент для разметки строк песни по таймингу трека.'
        )
        """
    )
    connection.execute(
        """
        DELETE FROM pitching_platforms
        WHERE support_url = '#'
          AND slug IN (
            'mts-music', 'deezer', 'spotify', 'tiktok', 'vk-music',
            'yandex-music', 'zvuk', 'tidal', 'apple-music'
          )
        """
    )


def migration_007_user_updated_at(connection: sqlite3.Connection) -> None:
    _add_column_if_missing(connection, "users", "updated_at", "TEXT")
    connection.execute(
        "UPDATE users SET updated_at = created_at WHERE updated_at IS NULL"
    )


def migration_008_username_index(connection: sqlite3.Connection) -> None:
    connection.execute("DROP INDEX IF EXISTS idx_users_username")
    connection.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_username ON users(username COLLATE NOCASE)"
    )


def migration_009_release_creation(connection: sqlite3.Connection) -> None:
    _add_column_if_missing(connection, "releases", "release_version", "TEXT")
    _add_column_if_missing(connection, "releases", "genre", "TEXT")
    _add_column_if_missing(
        connection, "releases", "metadata_language", "TEXT NOT NULL DEFAULT 'ru'"
    )
    _add_column_if_missing(
        connection, "releases", "is_explicit", "INTEGER NOT NULL DEFAULT 0"
    )
    _add_column_if_missing(connection, "releases", "moderator_comment", "TEXT")
    _add_column_if_missing(
        connection, "releases", "release_soon", "INTEGER NOT NULL DEFAULT 1"
    )
    _add_column_if_missing(connection, "releases", "submitted_at", "TEXT")
    _add_column_if_missing(connection, "releases", "rejection_reason", "TEXT")
    _add_column_if_missing(connection, "releases", "cover_filename", "TEXT")
    _add_column_if_missing(connection, "releases", "cover_size", "INTEGER")
    _add_column_if_missing(connection, "releases", "cover_width", "INTEGER")
    _add_column_if_missing(connection, "releases", "cover_height", "INTEGER")

    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS release_tracks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            release_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            artists TEXT NOT NULL,
            version TEXT,
            lyrics TEXT,
            language TEXT NOT NULL DEFAULT 'ru',
            lyricist TEXT NOT NULL,
            composer TEXT NOT NULL,
            is_explicit INTEGER NOT NULL DEFAULT 0,
            audio_path TEXT NOT NULL,
            audio_filename TEXT NOT NULL,
            audio_size INTEGER NOT NULL DEFAULT 0,
            tiktok_start_seconds INTEGER NOT NULL DEFAULT 0,
            tiktok_end_seconds INTEGER NOT NULL DEFAULT 60,
            position INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (release_id) REFERENCES releases(id) ON DELETE CASCADE,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_release_tracks_release_position
            ON release_tracks(release_id, position, id);
        CREATE INDEX IF NOT EXISTS idx_release_tracks_user
            ON release_tracks(user_id, release_id);
        """
    )


def migration_010_release_creation_indexes(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE INDEX IF NOT EXISTS idx_releases_user_created
            ON releases(user_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_releases_user_title
            ON releases(user_id, title COLLATE NOCASE);
        """
    )


def migration_011_support_center(connection: sqlite3.Connection) -> None:
    _add_column_if_missing(
        connection, "users", "is_admin", "INTEGER NOT NULL DEFAULT 0"
    )
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS support_faqs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category TEXT NOT NULL DEFAULT 'Общее',
            question TEXT NOT NULL UNIQUE,
            answer TEXT NOT NULL,
            sort_order INTEGER NOT NULL DEFAULT 0,
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS support_tickets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            subject TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT 'Общий вопрос',
            status TEXT NOT NULL DEFAULT 'open',
            priority TEXT NOT NULL DEFAULT 'normal',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            last_message_at TEXT NOT NULL,
            closed_at TEXT,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS support_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticket_id INTEGER NOT NULL,
            sender_type TEXT NOT NULL,
            user_id INTEGER,
            body TEXT NOT NULL,
            is_read INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            FOREIGN KEY (ticket_id) REFERENCES support_tickets(id) ON DELETE CASCADE,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL
        );

        CREATE INDEX IF NOT EXISTS idx_support_tickets_user_updated
            ON support_tickets(user_id, updated_at DESC);
        CREATE INDEX IF NOT EXISTS idx_support_tickets_status_updated
            ON support_tickets(status, updated_at DESC);
        CREATE INDEX IF NOT EXISTS idx_support_messages_ticket_created
            ON support_messages(ticket_id, created_at, id);
        """
    )

    now = datetime.now(timezone.utc).isoformat()
    faq_rows = (
        ("Релизы", "Сколько длится модерация релиза?", "Обычно проверка занимает до 5 рабочих дней. Если потребуются изменения, причина появится в карточке релиза и в уведомлении сервиса.", 10),
        ("Релизы", "Можно ли изменить релиз после отправки?", "Пока релиз находится на модерации, редактирование недоступно. После отклонения можно исправить замечания и отправить релиз повторно.", 20),
        ("Аудио", "Какие форматы аудио принимаются?", "Загружайте мастер-файлы в WAV или FLAC. Не используйте MP3 и другие сжатые форматы для финальной отправки.", 30),
        ("Права", "Почему релиз может быть отклонён?", "Чаще всего причиной становятся некорректная обложка, ошибки в авторах и исполнителях, неподтверждённые права или несоответствие аудиофайла требованиям.", 40),
        ("Поддержка", "Где посмотреть ответ поддержки?", "Ответ администратора появится в истории выбранной заявки на этой странице. Статус заявки также обновится автоматически.", 50),
    )
    for category, question, answer, sort_order in faq_rows:
        connection.execute(
            """
            INSERT INTO support_faqs (
                category, question, answer, sort_order, is_active, created_at, updated_at
            )
            SELECT ?, ?, ?, ?, 1, ?, ?
            WHERE NOT EXISTS (
                SELECT 1 FROM support_faqs WHERE question = ?
            )
            """,
            (category, question, answer, sort_order, now, now, question),
        )


def migration_012_pitching_and_rules(connection: sqlite3.Connection) -> None:
    _add_column_if_missing(connection, "pitching_requests", "message", "TEXT")
    _add_column_if_missing(connection, "pitching_requests", "admin_comment", "TEXT")
    _add_column_if_missing(connection, "pitching_requests", "reviewed_at", "TEXT")
    _add_column_if_missing(connection, "pitching_requests", "reviewed_by", "INTEGER")

    connection.executescript(
        """
        CREATE INDEX IF NOT EXISTS idx_pitching_requests_user_updated
            ON pitching_requests(user_id, updated_at DESC);
        CREATE INDEX IF NOT EXISTS idx_pitching_requests_platform_status
            ON pitching_requests(platform_id, status, updated_at DESC);

        CREATE TABLE IF NOT EXISTS release_rule_sections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            step_number INTEGER NOT NULL UNIQUE,
            title TEXT NOT NULL,
            intro TEXT NOT NULL,
            items_text TEXT NOT NULL,
            note TEXT,
            is_active INTEGER NOT NULL DEFAULT 1,
            updated_at TEXT NOT NULL
        );
        """
    )

    platform_rows = (
        ("Яндекс Музыка", "yandex-music", "/static/img/yandex-music.png", 10),
        ("VK Музыка", "vk-music", "/static/img/vk-music.png", 20),
        ("Apple Music", "apple-music", "/static/img/apple-music.png", 30),
        ("МТС Музыка", "mts-music", "/static/img/mts-music.png", 40),
        ("Deezer", "deezer", "/static/img/deezer.png", 50),
        ("TikTok", "tiktok", "/static/img/tiktok.png", 60),
        ("Звук", "zvuk", "/static/img/zvuk.png", 70),
    )
    for name, slug, logo_path, sort_order in platform_rows:
        connection.execute(
            """
            INSERT INTO pitching_platforms (
                name, slug, logo_path, support_url, is_active, sort_order
            ) VALUES (?, ?, ?, NULL, 1, ?)
            ON CONFLICT(slug) DO UPDATE SET
                name = excluded.name,
                logo_path = COALESCE(pitching_platforms.logo_path, excluded.logo_path),
                sort_order = excluded.sort_order
            """,
            (name, slug, logo_path, sort_order),
        )

    now = datetime.now(timezone.utc).isoformat()
    rule_rows = (
        (1, "Подготовьте данные релиза", "До загрузки соберите точные данные, которые будут опубликованы на площадках.", "Проверьте написание названия релиза и имени исполнителя.\nОпределите тип релиза: Single, EP или альбом.\nПодготовьте имена авторов слов и музыки без сокращений.\nУкажите реальный язык метаданных и основной жанр.", "После отправки изменение ключевых данных может потребовать повторной модерации."),
        (2, "Подготовьте обложку", "Обложка должна одинаково хорошо выглядеть в каталоге и в маленькой карточке трека.", "Используйте квадратное изображение JPG, PNG или WEBP.\nМинимальный размер — 1000×1000 px, рекомендуемый — 3000×3000 px.\nНе размещайте контакты, ссылки, цены и логотипы сторонних сервисов.\nИзображение не должно нарушать авторские права.", "Текст на обложке должен совпадать с названием релиза и именем исполнителя."),
        (3, "Проверьте аудиофайлы", "Загружайте финальные мастер-файлы без дополнительной обработки со стороны сервиса.", "Допустимые форматы — WAV и FLAC.\nВ начале и конце трека не должно быть случайной тишины или обрезанных фрагментов.\nУровень громкости не должен приводить к слышимым искажениям.\nКаждый файл должен соответствовать выбранному треку.", "MP3 и другие сжатые форматы не подходят для отправки релиза."),
        (4, "Укажите права и участников", "Метаданные должны однозначно объяснять, кто исполняет и кто создал произведение.", "Добавьте всех основных и приглашённых исполнителей.\nУкажите автора слов и автора музыки полными именами.\nОтметьте Explicit, если в треке есть ненормативная лексика.\nОтправляйте только контент, на который у вас есть права.", "По запросу модерации может потребоваться подтверждение прав или согласие участников."),
        (5, "Выберите дату и отправьте заранее", "Площадкам требуется время на проверку и размещение релиза.", "Укажите планируемую дату релиза.\nОтправляйте релиз как можно раньше, особенно если нужен питчинг.\nДля питчинга до даты выхода должно оставаться не менее 12 дней.\nПеред отправкой проверьте финальный экран со всеми данными.", "Дата выхода не гарантирует моментальное появление на всех площадках: сроки обновления каталогов отличаются."),
        (6, "Следите за модерацией", "Статус релиза и комментарии модератора всегда доступны в личном кабинете.", "Черновик можно свободно редактировать.\nНа модерации релиз временно заблокирован для изменений.\nПри отклонении исправьте указанную причину и отправьте повторно.\nПосле принятия не создавайте дубликат того же релиза.", "Если комментарий непонятен, создайте заявку в разделе «Поддержка» и приложите название релиза."),
    )
    for step, title, intro, items_text, note in rule_rows:
        connection.execute(
            """
            INSERT INTO release_rule_sections (
                step_number, title, intro, items_text, note, is_active, updated_at
            ) VALUES (?, ?, ?, ?, ?, 1, ?)
            ON CONFLICT(step_number) DO UPDATE SET
                title = excluded.title,
                intro = excluded.intro,
                items_text = excluded.items_text,
                note = excluded.note,
                updated_at = excluded.updated_at
            """,
            (step, title, intro, items_text, note, now),
        )


def migration_013_admin_platform_and_analytics(connection: sqlite3.Connection) -> None:
    _add_column_if_missing(connection, "users", "is_blocked", "INTEGER NOT NULL DEFAULT 0")
    _add_column_if_missing(connection, "users", "blocked_reason", "TEXT")
    _add_column_if_missing(connection, "users", "last_login_at", "TEXT")

    _add_column_if_missing(connection, "releases", "moderated_at", "TEXT")
    _add_column_if_missing(connection, "releases", "moderator_id", "INTEGER")
    _add_column_if_missing(connection, "releases", "moderation_decision", "TEXT")
    _add_column_if_missing(connection, "release_tracks", "isrc", "TEXT")

    _add_column_if_missing(connection, "service_news", "updated_at", "TEXT")
    connection.execute(
        "UPDATE service_news SET updated_at = published_at WHERE updated_at IS NULL"
    )

    _add_column_if_missing(connection, "pitching_platforms", "description", "TEXT")
    _add_column_if_missing(connection, "pitching_platforms", "instructions", "TEXT")

    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS track_listens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            release_id INTEGER NOT NULL,
            track_id INTEGER NOT NULL,
            listen_date TEXT NOT NULL,
            count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY (release_id) REFERENCES releases(id) ON DELETE CASCADE,
            FOREIGN KEY (track_id) REFERENCES release_tracks(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS payout_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            amount INTEGER NOT NULL,
            method TEXT NOT NULL DEFAULT 'telegram',
            details TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            admin_comment TEXT,
            transaction_id INTEGER,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            processed_at TEXT,
            processed_by INTEGER,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY (transaction_id) REFERENCES balance_transactions(id) ON DELETE SET NULL
        );

        CREATE TABLE IF NOT EXISTS moderation_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            release_id INTEGER NOT NULL,
            admin_user_id INTEGER,
            action TEXT NOT NULL,
            comment TEXT,
            from_status TEXT,
            to_status TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (release_id) REFERENCES releases(id) ON DELETE CASCADE,
            FOREIGN KEY (admin_user_id) REFERENCES users(id) ON DELETE SET NULL
        );

        CREATE TABLE IF NOT EXISTS user_notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            body TEXT NOT NULL,
            notification_type TEXT NOT NULL DEFAULT 'info',
            action_url TEXT,
            is_read INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            read_at TEXT,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS admin_audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            admin_user_id INTEGER,
            action TEXT NOT NULL,
            entity_type TEXT NOT NULL,
            entity_id INTEGER,
            details TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (admin_user_id) REFERENCES users(id) ON DELETE SET NULL
        );

        CREATE TABLE IF NOT EXISTS analytics_imports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            uploaded_by INTEGER,
            original_filename TEXT NOT NULL,
            stored_filename TEXT,
            period_label TEXT,
            status TEXT NOT NULL DEFAULT 'processing',
            row_count INTEGER NOT NULL DEFAULT 0,
            success_count INTEGER NOT NULL DEFAULT 0,
            error_count INTEGER NOT NULL DEFAULT 0,
            error_summary TEXT,
            created_at TEXT NOT NULL,
            completed_at TEXT,
            FOREIGN KEY (uploaded_by) REFERENCES users(id) ON DELETE SET NULL
        );

        CREATE TABLE IF NOT EXISTS analytics_import_rows (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            import_id INTEGER NOT NULL,
            row_number INTEGER NOT NULL,
            status TEXT NOT NULL,
            message TEXT,
            user_id INTEGER,
            release_id INTEGER,
            track_id INTEGER,
            listens INTEGER,
            amount INTEGER,
            created_at TEXT NOT NULL,
            FOREIGN KEY (import_id) REFERENCES analytics_imports(id) ON DELETE CASCADE,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL,
            FOREIGN KEY (release_id) REFERENCES releases(id) ON DELETE SET NULL,
            FOREIGN KEY (track_id) REFERENCES release_tracks(id) ON DELETE SET NULL
        );

        CREATE INDEX IF NOT EXISTS idx_track_listens_user_date
            ON track_listens(user_id, listen_date);
        CREATE INDEX IF NOT EXISTS idx_track_listens_track_date
            ON track_listens(track_id, listen_date);
        CREATE INDEX IF NOT EXISTS idx_payout_requests_user_status
            ON payout_requests(user_id, status, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_payout_requests_status_created
            ON payout_requests(status, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_moderation_events_release_created
            ON moderation_events(release_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_user_notifications_user_read
            ON user_notifications(user_id, is_read, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_admin_audit_created
            ON admin_audit_log(created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_analytics_imports_created
            ON analytics_imports(created_at DESC);
        """
    )

    platform_copy = {
        "yandex-music": (
            "Редакционный питчинг будущего релиза в Яндекс Музыке.",
            "Опишите историю трека, жанр, настроение, ключевую аудиторию и подтверждённый план продвижения."
        ),
        "vk-music": (
            "Заявка на редакционное рассмотрение релиза во VK Музыке.",
            "Укажите сильную сторону релиза, целевую аудиторию, регион продвижения и ссылки на активные социальные сети."
        ),
        "apple-music": (
            "Подготовка релиза к редакционному рассмотрению Apple Music.",
            "Сфокусируйтесь на истории артиста, культурном контексте релиза и подтверждённой промокампании."
        ),
        "mts-music": (
            "Рассмотрение релиза редакцией МТС Музыки.",
            "Добавьте краткое описание артиста, релиза, аудитории и запланированных публикаций."
        ),
        "deezer": (
            "Питчинг релиза для возможного редакционного размещения в Deezer.",
            "Опишите жанр, ключевые рынки, историю композиции и маркетинговые активности."
        ),
        "tiktok": (
            "Продвижение выбранного отрывка релиза для коротких видео.",
            "Укажите точный отрывок, идею пользовательского контента и план работы с авторами видео."
        ),
        "zvuk": (
            "Заявка на редакционную поддержку релиза в сервисе Звук.",
            "Расскажите о релизе, аудитории, регионе и запланированном продвижении."
        ),
    }
    for slug, (description, instructions) in platform_copy.items():
        connection.execute(
            """
            UPDATE pitching_platforms
            SET description = COALESCE(description, ?),
                instructions = COALESCE(instructions, ?)
            WHERE slug = ?
            """,
            (description, instructions, slug),
        )


def migration_014_bootstrap_first_admin(connection: sqlite3.Connection) -> None:
    """Ensure a local installation is not left without an administrator."""
    has_admin = connection.execute(
        "SELECT 1 FROM users WHERE is_admin = 1 LIMIT 1"
    ).fetchone()
    if has_admin:
        return
    first_user = connection.execute(
        "SELECT id FROM users ORDER BY id ASC LIMIT 1"
    ).fetchone()
    if first_user:
        connection.execute(
            "UPDATE users SET is_admin = 1 WHERE id = ?",
            (int(first_user[0]),),
        )


def migration_015_telegram_integration(connection: sqlite3.Connection) -> None:
    """Telegram account linking, durable dialog state and notification outbox."""
    _add_column_if_missing(connection, "users", "telegram_username", "TEXT")
    _add_column_if_missing(connection, "users", "telegram_chat_id", "TEXT")
    _add_column_if_missing(connection, "users", "telegram_connected_at", "TEXT")
    _add_column_if_missing(connection, "users", "telegram_notifications_enabled", "INTEGER NOT NULL DEFAULT 1")

    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS telegram_dialog_sessions (
            telegram_id TEXT PRIMARY KEY,
            chat_id TEXT NOT NULL,
            step TEXT NOT NULL,
            login_value TEXT,
            payload_json TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            expires_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS telegram_outbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            recipient_type TEXT NOT NULL,
            user_id INTEGER,
            chat_id TEXT,
            event_type TEXT NOT NULL,
            message_text TEXT NOT NULL,
            action_url TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,
            available_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            sent_at TEXT,
            last_error TEXT,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE UNIQUE INDEX IF NOT EXISTS idx_users_telegram_id_unique
            ON users(telegram_id) WHERE telegram_id IS NOT NULL AND telegram_id != '';
        CREATE INDEX IF NOT EXISTS idx_telegram_dialog_expiry
            ON telegram_dialog_sessions(expires_at);
        CREATE INDEX IF NOT EXISTS idx_telegram_outbox_pending
            ON telegram_outbox(status, available_at, id);
        CREATE INDEX IF NOT EXISTS idx_telegram_outbox_user
            ON telegram_outbox(user_id, created_at DESC);
        """
    )



def migration_016_static_pitching_resources(connection: sqlite3.Connection) -> None:
    """Switch pitching to direct external resources and add current platform links."""
    resources = (
        ("МТС Музыка", "mts-music", "/static/img/mts-music.png", "https://music.mts.ru/", 10),
        ("Deezer", "deezer", "/static/img/deezer.png", "https://creators.deezer.com/", 20),
        ("Spotify", "spotify", "/static/img/spotify.svg", "https://artists.spotify.com/", 30),
        ("TikTok", "tiktok", "/static/img/tiktok.png", "https://artists.tiktok.com/", 40),
        ("VK Музыка", "vk-music", "/static/img/vk-music.png", "https://vk.com/vkmusic", 50),
        ("Яндекс Музыка", "yandex-music", "/static/img/yandex-music.png", "https://music.yandex.ru/", 60),
        ("Звук", "zvuk", "/static/img/zvuk.png", "https://zvuk.com/", 70),
        ("TIDAL", "tidal", "/static/img/tidal.svg", "https://artists.tidal.com/", 80),
        ("Apple Music", "apple-music", "/static/img/apple-music.png", "https://artists.apple.com/", 90),
    )
    for name, slug, logo_path, support_url, sort_order in resources:
        connection.execute(
            """
            INSERT INTO pitching_platforms (name, slug, logo_path, support_url, is_active, sort_order)
            VALUES (?, ?, ?, ?, 1, ?)
            ON CONFLICT(slug) DO UPDATE SET
                name = excluded.name,
                logo_path = excluded.logo_path,
                support_url = excluded.support_url,
                is_active = 1,
                sort_order = excluded.sort_order
            """,
            (name, slug, logo_path, support_url, sort_order),
        )


def migration_017_pitching_links(connection: sqlite3.Connection) -> None:
    """Install the supplied pitching links and hide removed platforms."""
    resources = (
        ("МТС Музыка", "mts-music", "/static/img/mts-music.png", "https://music.mts.ru/pitch", 10),
        ("Яндекс Музыка", "yandex-music", "/static/img/yandex-music.png", "https://teletype.in/@ramixmusic/yandexmusic", 20),
        ("VK Музыка", "vk-music", "/static/img/vk-music.png", "https://vk.ru/app5619682_-147845620#724562", 30),
        ("Spotify", "spotify", "/static/img/spotify.svg", "https://teletype.in/@ramixmusic/spotify", 40),
        ("TikTok", "tiktok", "/static/img/tiktok.png", "https://teletype.in/@ramixmusic/tiktok", 50),
        ("Звук", "zvuk", "/static/img/zvuk.png", "https://teletype.in/@ramixmusic/zvuk", 60),
    )
    for name, slug, logo_path, support_url, sort_order in resources:
        connection.execute(
            """
            INSERT INTO pitching_platforms (name, slug, logo_path, support_url, is_active, sort_order)
            VALUES (?, ?, ?, ?, 1, ?)
            ON CONFLICT(slug) DO UPDATE SET
                name = excluded.name,
                logo_path = excluded.logo_path,
                support_url = excluded.support_url,
                is_active = 1,
                sort_order = excluded.sort_order
            """,
            (name, slug, logo_path, support_url, sort_order),
        )
    connection.execute(
        "UPDATE pitching_platforms SET is_active = 0 WHERE slug IN ('tidal', 'deezer')"
    )


def migration_018_release_rule_requirements(connection: sqlite3.Connection) -> None:
    """Keep the standalone release rules consistent with the upload form."""
    now = datetime.now(timezone.utc).isoformat()
    connection.execute(
        """
        UPDATE release_rule_sections
        SET items_text = ?, note = ?, updated_at = ?
        WHERE step_number = 2
        """,
        (
            "Используйте квадратное изображение JPG или PNG.\n"
            "Размер — от 1400×1400 до 6000×6000 px, разрешение — не менее 72 dpi.\n"
            "Максимальный размер файла — 20 МБ.\n"
            "Не размещайте контакты, ссылки, даты, цены, штрих-коды и логотипы сторонних сервисов.\n"
            "Используйте только оригинальное изображение, на которое у вас есть права.",
            "Изображение должно быть чётким, без белой рамки, размытия и пикселизации.",
            now,
        ),
    )
    connection.execute(
        """
        UPDATE release_rule_sections
        SET items_text = ?, note = ?, updated_at = ?
        WHERE step_number = 5
        """,
        (
            "Укажите планируемую дату релиза.\n"
            "Минимальный срок доставки релиза на площадки — 3 дня.\n"
            "Рекомендуем загружать релиз как минимум за 10 дней до выхода.\n"
            "Перед отправкой проверьте финальный экран со всеми данными.",
            "Площадки модерируют релиз независимо, поэтому он может появиться на них не одновременно.",
            now,
        ),
    )


MIGRATIONS: tuple[Migration, ...] = (
    (1, "users", migration_001_users),
    (2, "user profile fields", migration_002_user_profile_fields),
    (3, "platform tables", migration_003_platform_tables),
    (4, "indexes", migration_004_indexes),
    (5, "historical global seed", migration_005_global_seed),
    (6, "remove dashboard demo data", migration_006_remove_demo_data),
    (7, "user updated timestamp", migration_007_user_updated_at),
    (8, "case-insensitive username index", migration_008_username_index),
    (9, "release creation workflow", migration_009_release_creation),
    (10, "release creation indexes", migration_010_release_creation_indexes),
    (11, "support center", migration_011_support_center),
    (12, "pitching and release rules", migration_012_pitching_and_rules),
    (13, "admin platform and analytics", migration_013_admin_platform_and_analytics),
    (14, "bootstrap first administrator", migration_014_bootstrap_first_admin),
    (15, "telegram integration and notification outbox", migration_015_telegram_integration),
    (16, "static pitching resources", migration_016_static_pitching_resources),
    (17, "supplied pitching links", migration_017_pitching_links),
    (18, "release rule requirements", migration_018_release_rule_requirements),
)


def run_migrations(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at TEXT NOT NULL
        )
        """
    )
    applied = {
        int(row[0])
        for row in connection.execute("SELECT version FROM schema_migrations").fetchall()
    }

    for version, name, migration in MIGRATIONS:
        if version in applied:
            continue
        migration(connection)
        connection.execute(
            """
            INSERT INTO schema_migrations (version, name, applied_at)
            VALUES (?, ?, ?)
            """,
            (version, name, datetime.now(timezone.utc).isoformat()),
        )
        connection.commit()
