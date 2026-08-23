from __future__ import annotations

import re
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from config import BASE_DIR, DATA_DIR, DATABASE_PATH
from migrations import run_migrations
USERNAME_PATTERN = re.compile(r"^[a-z0-9_]{3,32}$")


def _connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DATABASE_PATH, timeout=15)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA busy_timeout = 5000")
    return connection


def init_database() -> None:
    with _connect() as connection:
        run_migrations(connection)


def _make_username(connection: sqlite3.Connection, email: str) -> str:
    local_part = email.split("@", 1)[0].strip().lower()
    base = re.sub(r"[^a-z0-9_]+", "_", local_part).strip("_") or "ramix_artist"
    base = base[:27]
    if len(base) < 3:
        base = f"artist_{base}"[:27]

    candidate = base
    suffix = 1
    while connection.execute(
        "SELECT 1 FROM users WHERE username = ? COLLATE NOCASE", (candidate,)
    ).fetchone():
        suffix += 1
        suffix_text = f"_{suffix}"
        candidate = f"{base[:32 - len(suffix_text)]}{suffix_text}"
    return candidate


def create_user(name: str, email: str, password_hash: str) -> dict[str, Any] | None:
    now = datetime.now(timezone.utc).isoformat()
    try:
        with _connect() as connection:
            username = _make_username(connection, email)
            has_admin = connection.execute(
                "SELECT 1 FROM users WHERE is_admin = 1 LIMIT 1"
            ).fetchone()
            is_admin = 0 if has_admin else 1
            cursor = connection.execute(
                """
                INSERT INTO users (
                    name, email, password_hash, username, is_admin, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (name, email, password_hash, username, is_admin, now, now),
            )
            connection.commit()
            user_id = int(cursor.lastrowid)
    except sqlite3.IntegrityError:
        return None

    return get_user_by_id(user_id)


def _user_select() -> str:
    return """
        SELECT id, name, email, password_hash, username, balance,
               pending_balance, last_payout_at, telegram_id,
               telegram_connected, is_admin, is_blocked, blocked_reason,
               last_login_at, created_at, updated_at
        FROM users
    """


def get_user_by_email(email: str) -> dict[str, Any] | None:
    with _connect() as connection:
        row = connection.execute(
            _user_select() + " WHERE email = ? COLLATE NOCASE",
            (email,),
        ).fetchone()
    return dict(row) if row else None


def get_user_by_id(user_id: int) -> dict[str, Any] | None:
    with _connect() as connection:
        row = connection.execute(
            _user_select() + " WHERE id = ?",
            (user_id,),
        ).fetchone()
    return dict(row) if row else None


def email_is_taken(email: str, *, exclude_user_id: int | None = None) -> bool:
    query = "SELECT 1 FROM users WHERE email = ? COLLATE NOCASE"
    params: list[Any] = [email]
    if exclude_user_id is not None:
        query += " AND id != ?"
        params.append(exclude_user_id)
    with _connect() as connection:
        return connection.execute(query, params).fetchone() is not None


def username_is_taken(username: str, *, exclude_user_id: int | None = None) -> bool:
    query = "SELECT 1 FROM users WHERE username = ? COLLATE NOCASE"
    params: list[Any] = [username]
    if exclude_user_id is not None:
        query += " AND id != ?"
        params.append(exclude_user_id)
    with _connect() as connection:
        return connection.execute(query, params).fetchone() is not None


def update_user_profile(
    user_id: int,
    *,
    name: str,
    email: str,
    username: str,
) -> dict[str, Any] | None:
    now = datetime.now(timezone.utc).isoformat()
    try:
        with _connect() as connection:
            cursor = connection.execute(
                """
                UPDATE users
                SET name = ?, email = ?, username = ?, updated_at = ?
                WHERE id = ?
                """,
                (name, email, username, now, user_id),
            )
            connection.commit()
            if cursor.rowcount != 1:
                return None
    except sqlite3.IntegrityError:
        return None
    return get_user_by_id(user_id)


def update_user_password(user_id: int, password_hash: str) -> bool:
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as connection:
        cursor = connection.execute(
            """
            UPDATE users
            SET password_hash = ?, updated_at = ?
            WHERE id = ?
            """,
            (password_hash, now, user_id),
        )
        connection.commit()
        return cursor.rowcount == 1


def _release_status_meta(status: str) -> dict[str, str]:
    mapping = {
        "accepted": {"label": "Принят", "class": "success"},
        "published": {"label": "Опубликован", "class": "success"},
        "moderation": {"label": "На модерации", "class": "warning"},
        "changes_required": {"label": "Требуются изменения", "class": "warning"},
        "rejected": {"label": "Отклонён", "class": "danger"},
        "draft": {"label": "Черновик", "class": "draft"},
    }
    return mapping.get(status, {"label": status.replace("_", " ").title(), "class": "draft"})


def _operation_status_meta(status: str) -> dict[str, str]:
    mapping = {
        "completed": {"label": "Готово", "class": "success"},
        "processing": {"label": "В работе", "class": "warning"},
        "saved": {"label": "Сохранён", "class": "draft"},
        "rejected": {"label": "Отклонено", "class": "danger"},
        "cancelled": {"label": "Отменено", "class": "danger"},
    }
    return mapping.get(status, {"label": status.replace("_", " ").title(), "class": "draft"})


def _format_money(value: int | None, *, show_plus: bool = False) -> str:
    if value is None:
        return "—"
    sign = "+" if show_plus and value > 0 else ""
    return f"{sign}{value:,}".replace(",", " ") + " ₽"


def _format_compact(value: int) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M".rstrip("0").rstrip(".")
    if value >= 1_000:
        return f"{value / 1_000:.1f}K".rstrip("0").rstrip(".")
    return str(value)


def _date_label(value: str | None, short: bool = False) -> str:
    if not value:
        return "Не было"
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return "—"
    return parsed.strftime("%d.%m" if short else "%d.%m.%Y")


def _plural(value: int, one: str, few: str, many: str) -> str:
    value = abs(value) % 100
    last = value % 10
    if 10 < value < 20:
        return many
    if last == 1:
        return one
    if 1 < last < 5:
        return few
    return many


def _chart_rows(connection: sqlite3.Connection, user_id: int, days: int) -> list[dict[str, Any]]:
    start = date.today() - timedelta(days=days - 1)
    rows = connection.execute(
        """
        SELECT listen_date, SUM(count) AS total
        FROM listens
        WHERE user_id = ? AND listen_date >= ?
        GROUP BY listen_date
        ORDER BY listen_date
        """,
        (user_id, start.isoformat()),
    ).fetchall()
    return [
        {
            "label": datetime.fromisoformat(str(row["listen_date"])).strftime("%d"),
            "value": int(row["total"] or 0),
        }
        for row in rows
        if int(row["total"] or 0) > 0
    ]


def _year_chart_rows(connection: sqlite3.Connection, user_id: int) -> list[dict[str, Any]]:
    start = (date.today().replace(day=1) - timedelta(days=365)).replace(day=1)
    rows = connection.execute(
        """
        SELECT substr(listen_date, 1, 7) AS month_key, SUM(count) AS total
        FROM listens
        WHERE user_id = ? AND listen_date >= ?
        GROUP BY month_key
        ORDER BY month_key
        """,
        (user_id, start.isoformat()),
    ).fetchall()
    month_names = ["Янв", "Фев", "Мар", "Апр", "Май", "Июн", "Июл", "Авг", "Сен", "Окт", "Ноя", "Дек"]
    result: list[dict[str, Any]] = []
    for row in rows:
        total = int(row["total"] or 0)
        month_key = str(row["month_key"] or "")
        if total <= 0 or len(month_key) != 7:
            continue
        result.append(
            {
                "label": month_names[int(month_key[5:7]) - 1],
                "value": total,
            }
        )
    return result


def get_dashboard_data(user_id: int) -> dict[str, Any]:
    with _connect() as connection:
        user_row = connection.execute(
            """
            SELECT id, name, email, username, balance, pending_balance,
                   last_payout_at, telegram_connected
            FROM users WHERE id = ?
            """,
            (user_id,),
        ).fetchone()
        if not user_row:
            raise ValueError("User not found")

        status_rows = connection.execute(
            """
            SELECT status, COUNT(*) AS total
            FROM releases
            WHERE user_id = ?
            GROUP BY status
            """,
            (user_id,),
        ).fetchall()
        status_counts = {str(row["status"]): int(row["total"]) for row in status_rows}
        release_count = sum(status_counts.values())
        moderation_count = status_counts.get("moderation", 0)

        month_start = (date.today() - timedelta(days=29)).isoformat()
        total_listens = int(
            connection.execute(
                """
                SELECT COALESCE(SUM(count), 0)
                FROM listens
                WHERE user_id = ? AND listen_date >= ?
                """,
                (user_id, month_start),
            ).fetchone()[0]
        )

        release_rows = connection.execute(
            """
            SELECT id, title, artist_name, release_type, status, upc,
                   cover_path, release_date, updated_at
            FROM releases
            WHERE user_id = ?
            ORDER BY datetime(updated_at) DESC, id DESC
            LIMIT 3
            """,
            (user_id,),
        ).fetchall()
        releases: list[dict[str, Any]] = []
        for row in release_rows:
            item = dict(row)
            item["status_meta"] = _release_status_meta(str(item["status"]))
            item["is_editable"] = str(item["status"]) in {"draft", "rejected", "changes_required"}
            item["cover_initial"] = str(item["title"] or "R")[:1].upper()
            cover_value = item.get("cover_path")
            item["cover_url"] = (
                str(cover_value)
                if cover_value and str(cover_value).startswith("/")
                else (f"/account/releases/{int(item['id'])}/cover" if cover_value else None)
            )
            releases.append(item)

        operation_rows = connection.execute(
            """
            SELECT bt.title, bt.status, bt.amount, bt.created_at,
                   r.title AS release_title
            FROM balance_transactions AS bt
            LEFT JOIN releases AS r ON r.id = bt.release_id
            WHERE bt.user_id = ?
            ORDER BY datetime(bt.created_at) DESC, bt.id DESC
            LIMIT 5
            """,
            (user_id,),
        ).fetchall()
        operations: list[dict[str, Any]] = []
        for row in operation_rows:
            operations.append(
                {
                    "title": str(row["title"]),
                    "release_title": str(row["release_title"] or "—"),
                    "date_label": _date_label(str(row["created_at"])),
                    "status_meta": _operation_status_meta(str(row["status"])),
                    "amount_label": _format_money(row["amount"], show_plus=True),
                }
            )

        now = datetime.now(timezone.utc).isoformat()
        news_rows = connection.execute(
            """
            SELECT id, title, body, published_at
            FROM service_news
            WHERE is_active = 1 AND published_at <= ?
            ORDER BY sort_order DESC, datetime(published_at) DESC, id DESC
            LIMIT 3
            """,
            (now,),
        ).fetchall()
        month_names = ["ЯНВ", "ФЕВ", "МАР", "АПР", "МАЙ", "ИЮН", "ИЮЛ", "АВГ", "СЕН", "ОКТ", "НОЯ", "ДЕК"]
        news: list[dict[str, Any]] = []
        for row in news_rows:
            try:
                published = datetime.fromisoformat(str(row["published_at"]).replace("Z", "+00:00"))
            except ValueError:
                continue
            news.append(
                {
                    "id": int(row["id"]),
                    "title": str(row["title"]),
                    "body": str(row["body"]),
                    "published_at": str(row["published_at"]),
                    "day": published.strftime("%d"),
                    "month": month_names[published.month - 1],
                }
            )

        pitching_rows = connection.execute(
            """
            SELECT id, name, slug, logo_path, support_url
            FROM pitching_platforms
            WHERE is_active = 1
              AND support_url IS NOT NULL
              AND trim(support_url) NOT IN ('', '#')
            ORDER BY sort_order DESC, name ASC
            LIMIT 4
            """
        ).fetchall()
        pitching_platforms = [
            {
                "id": int(row["id"]),
                "name": str(row["name"]),
                "slug": str(row["slug"]),
                "logo_path": row["logo_path"],
                "support_url": str(row["support_url"]),
            }
            for row in pitching_rows
        ]

        chart_data = {
            "7": _chart_rows(connection, user_id, 7),
            "30": _chart_rows(connection, user_id, 30),
            "365": _year_chart_rows(connection, user_id),
        }

    balance = int(user_row["balance"] or 0)
    pending_balance = int(user_row["pending_balance"] or 0)
    user = {
        "id": int(user_row["id"]),
        "name": str(user_row["name"]),
        "email": str(user_row["email"]),
        "username": str(user_row["username"] or "artist"),
    }

    return {
        "user": user,
        "stats": {
            "listens": total_listens,
            "listens_label": _format_compact(total_listens),
            "releases": release_count,
            "releases_word": _plural(release_count, "релиз", "релиза", "релизов"),
            "moderation": moderation_count,
            "moderation_word": _plural(moderation_count, "релиз", "релиза", "релизов"),
            "balance": balance,
            "balance_label": _format_money(balance),
            "statuses": {
                "accepted": status_counts.get("accepted", 0) + status_counts.get("published", 0),
                "rejected": status_counts.get("rejected", 0) + status_counts.get("changes_required", 0),
                "changes_required": status_counts.get("changes_required", 0),
                "draft": status_counts.get("draft", 0),
                "moderation": moderation_count,
            },
        },
        "releases": releases,
        "operations": operations,
        "news": news,
        "pitching_platforms": pitching_platforms,
        "chart_data": chart_data,
        "has_chart_data": any(chart_data.values()),
        "balance": {
            "available": balance,
            "available_label": _format_money(balance),
            "pending": pending_balance,
            "pending_label": _format_money(pending_balance),
            "last_payout_label": _date_label(user_row["last_payout_at"], short=True),
            "public_id": str(user_row["id"]).zfill(4),
            "telegram_connected": bool(user_row["telegram_connected"]),
            "can_withdraw": balance > 0 and bool(user_row["telegram_connected"]),
        },
    }

# Release creation and catalog workflow
RELEASE_EDITABLE_STATUSES = {"draft", "rejected", "changes_required"}
RELEASE_TYPES = {"single", "ep", "album"}
RELEASE_LANGUAGES = {"ru", "en", "instrumental", "other"}
TRACK_LANGUAGES = {"ru", "en", "instrumental", "other"}


def release_status_meta(status: str) -> dict[str, str]:
    return _release_status_meta(status)


def _cover_url(release_id: int, cover_path: str | None) -> str | None:
    if not cover_path:
        return None
    value = str(cover_path)
    if value.startswith("/"):
        return value
    return f"/account/releases/{release_id}/cover"


def _release_type_label(value: str) -> str:
    return {"single": "Single", "ep": "EP", "album": "Альбом"}.get(value, value.title())


def _language_label(value: str) -> str:
    return {
        "ru": "Русский",
        "en": "Английский",
        "instrumental": "Инструментал",
        "other": "Другой",
    }.get(value, value)


def _release_dict(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    release_id = int(item["id"])
    item["id"] = release_id
    item["status_meta"] = _release_status_meta(str(item.get("status") or "draft"))
    item["release_type_label"] = _release_type_label(str(item.get("release_type") or "single"))
    item["metadata_language_label"] = _language_label(str(item.get("metadata_language") or "ru"))
    item["cover_url"] = _cover_url(release_id, item.get("cover_path"))
    item["cover_initial"] = str(item.get("title") or "R")[:1].upper()
    item["is_editable"] = str(item.get("status")) in RELEASE_EDITABLE_STATUSES
    item["release_date_label"] = _date_label(item.get("release_date")) if item.get("release_date") else "Не назначена"
    item["created_at_label"] = _date_label(item.get("created_at"))
    item["updated_at_label"] = _date_label(item.get("updated_at"))
    return item


def get_release_for_user(user_id: int, release_id: int) -> dict[str, Any] | None:
    with _connect() as connection:
        row = connection.execute(
            """
            SELECT r.*, COUNT(rt.id) AS track_count
            FROM releases AS r
            LEFT JOIN release_tracks AS rt ON rt.release_id = r.id
            WHERE r.id = ? AND r.user_id = ?
            GROUP BY r.id
            """,
            (release_id, user_id),
        ).fetchone()
    return _release_dict(row) if row else None


def create_release(
    user_id: int,
    *,
    title: str,
    artist_name: str,
    release_type: str,
    release_version: str,
    genre: str,
    metadata_language: str,
    release_date: str,
    is_explicit: bool,
    cover: dict[str, Any],
) -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as connection:
        cursor = connection.execute(
            """
            INSERT INTO releases (
                user_id, title, artist_name, release_type, status,
                cover_path, cover_filename, cover_size, cover_width, cover_height,
                release_version, genre, metadata_language, release_date, is_explicit,
                release_soon, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'draft', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
            """,
            (
                user_id,
                title,
                artist_name,
                release_type,
                cover["path"],
                cover["filename"],
                int(cover["size"]),
                int(cover["width"]),
                int(cover["height"]),
                release_version or None,
                genre,
                metadata_language,
                release_date,
                1 if is_explicit else 0,
                now,
                now,
            ),
        )
        connection.commit()
        release_id = int(cursor.lastrowid)
    result = get_release_for_user(user_id, release_id)
    if not result:
        raise RuntimeError("Release was not created")
    return result


def update_release_details(
    user_id: int,
    release_id: int,
    *,
    title: str,
    artist_name: str,
    release_type: str,
    release_version: str,
    genre: str,
    metadata_language: str,
    release_date: str,
    is_explicit: bool,
    cover: dict[str, Any] | None,
) -> dict[str, Any] | None:
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as connection:
        existing = connection.execute(
            "SELECT status FROM releases WHERE id = ? AND user_id = ?",
            (release_id, user_id),
        ).fetchone()
        if not existing or str(existing["status"]) not in RELEASE_EDITABLE_STATUSES:
            return None

        if cover:
            connection.execute(
                """
                UPDATE releases
                SET title = ?, artist_name = ?, release_type = ?, release_version = ?, genre = ?,
                    metadata_language = ?, release_date = ?, is_explicit = ?, status = 'draft',
                    rejection_reason = NULL, cover_path = ?, cover_filename = ?,
                    cover_size = ?, cover_width = ?, cover_height = ?, updated_at = ?
                WHERE id = ? AND user_id = ?
                """,
                (
                    title,
                    artist_name,
                    release_type,
                    release_version or None,
                    genre,
                    metadata_language,
                    release_date,
                    1 if is_explicit else 0,
                    cover["path"],
                    cover["filename"],
                    int(cover["size"]),
                    int(cover["width"]),
                    int(cover["height"]),
                    now,
                    release_id,
                    user_id,
                ),
            )
        else:
            connection.execute(
                """
                UPDATE releases
                SET title = ?, artist_name = ?, release_type = ?, release_version = ?, genre = ?,
                    metadata_language = ?, release_date = ?, is_explicit = ?, status = 'draft',
                    rejection_reason = NULL, updated_at = ?
                WHERE id = ? AND user_id = ?
                """,
                (
                    title,
                    artist_name,
                    release_type,
                    release_version or None,
                    genre,
                    metadata_language,
                    release_date,
                    1 if is_explicit else 0,
                    now,
                    release_id,
                    user_id,
                ),
            )
        connection.commit()
    return get_release_for_user(user_id, release_id)


def list_release_tracks(user_id: int, release_id: int) -> list[dict[str, Any]]:
    from release_service import format_file_size, format_timecode

    with _connect() as connection:
        rows = connection.execute(
            """
            SELECT *
            FROM release_tracks
            WHERE user_id = ? AND release_id = ?
            ORDER BY position ASC, id ASC
            """,
            (user_id, release_id),
        ).fetchall()

    result: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["id"] = int(item["id"])
        item["position"] = int(item["position"])
        item["language_label"] = _language_label(str(item.get("language") or "ru"))
        item["audio_size_label"] = format_file_size(item.get("audio_size"))
        item["tiktok_start_label"] = format_timecode(item.get("tiktok_start_seconds"))
        item["tiktok_end_label"] = format_timecode(item.get("tiktok_end_seconds"))
        item["explicit_label"] = "Есть" if item.get("is_explicit") else "Нет"
        result.append(item)
    return result


def get_track_for_user(user_id: int, release_id: int, track_id: int) -> dict[str, Any] | None:
    with _connect() as connection:
        row = connection.execute(
            """
            SELECT * FROM release_tracks
            WHERE id = ? AND release_id = ? AND user_id = ?
            """,
            (track_id, release_id, user_id),
        ).fetchone()
    return dict(row) if row else None


def create_release_track(
    user_id: int,
    release_id: int,
    *,
    title: str,
    artists: str,
    version: str,
    lyrics: str,
    language: str,
    lyricist: str,
    composer: str,
    is_explicit: bool,
    audio: dict[str, Any],
    tiktok_start_seconds: int,
    tiktok_end_seconds: int,
) -> dict[str, Any] | None:
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as connection:
        release = connection.execute(
            "SELECT status FROM releases WHERE id = ? AND user_id = ?",
            (release_id, user_id),
        ).fetchone()
        if not release or str(release["status"]) not in RELEASE_EDITABLE_STATUSES:
            return None
        position = int(
            connection.execute(
                "SELECT COALESCE(MAX(position), 0) + 1 FROM release_tracks WHERE release_id = ?",
                (release_id,),
            ).fetchone()[0]
        )
        cursor = connection.execute(
            """
            INSERT INTO release_tracks (
                release_id, user_id, title, artists, version, lyrics, language,
                lyricist, composer, is_explicit, audio_path, audio_filename,
                audio_size, tiktok_start_seconds, tiktok_end_seconds, position,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                release_id,
                user_id,
                title,
                artists,
                version or None,
                lyrics or None,
                language,
                lyricist,
                composer,
                1 if is_explicit else 0,
                audio["path"],
                audio["filename"],
                int(audio["size"]),
                tiktok_start_seconds,
                tiktok_end_seconds,
                position,
                now,
                now,
            ),
        )
        connection.execute(
            """
            UPDATE releases
            SET status = 'draft', rejection_reason = NULL, updated_at = ?
            WHERE id = ? AND user_id = ?
            """,
            (now, release_id, user_id),
        )
        connection.commit()
        track_id = int(cursor.lastrowid)
    return get_track_for_user(user_id, release_id, track_id)


def update_release_track(
    user_id: int,
    release_id: int,
    track_id: int,
    *,
    title: str,
    artists: str,
    version: str,
    lyrics: str,
    language: str,
    lyricist: str,
    composer: str,
    is_explicit: bool,
    audio: dict[str, Any] | None,
    tiktok_start_seconds: int,
    tiktok_end_seconds: int,
) -> dict[str, Any] | None:
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as connection:
        existing = connection.execute(
            """
            SELECT rt.id, r.status
            FROM release_tracks AS rt
            JOIN releases AS r ON r.id = rt.release_id
            WHERE rt.id = ? AND rt.release_id = ? AND rt.user_id = ? AND r.user_id = ?
            """,
            (track_id, release_id, user_id, user_id),
        ).fetchone()
        if not existing or str(existing["status"]) not in RELEASE_EDITABLE_STATUSES:
            return None

        if audio:
            connection.execute(
                """
                UPDATE release_tracks
                SET title = ?, artists = ?, version = ?, lyrics = ?, language = ?,
                    lyricist = ?, composer = ?, is_explicit = ?, audio_path = ?,
                    audio_filename = ?, audio_size = ?, tiktok_start_seconds = ?,
                    tiktok_end_seconds = ?, updated_at = ?
                WHERE id = ? AND release_id = ? AND user_id = ?
                """,
                (
                    title,
                    artists,
                    version or None,
                    lyrics or None,
                    language,
                    lyricist,
                    composer,
                    1 if is_explicit else 0,
                    audio["path"],
                    audio["filename"],
                    int(audio["size"]),
                    tiktok_start_seconds,
                    tiktok_end_seconds,
                    now,
                    track_id,
                    release_id,
                    user_id,
                ),
            )
        else:
            connection.execute(
                """
                UPDATE release_tracks
                SET title = ?, artists = ?, version = ?, lyrics = ?, language = ?,
                    lyricist = ?, composer = ?, is_explicit = ?,
                    tiktok_start_seconds = ?, tiktok_end_seconds = ?, updated_at = ?
                WHERE id = ? AND release_id = ? AND user_id = ?
                """,
                (
                    title,
                    artists,
                    version or None,
                    lyrics or None,
                    language,
                    lyricist,
                    composer,
                    1 if is_explicit else 0,
                    tiktok_start_seconds,
                    tiktok_end_seconds,
                    now,
                    track_id,
                    release_id,
                    user_id,
                ),
            )
        connection.execute(
            """
            UPDATE releases
            SET status = 'draft', rejection_reason = NULL, updated_at = ?
            WHERE id = ? AND user_id = ?
            """,
            (now, release_id, user_id),
        )
        connection.commit()
    return get_track_for_user(user_id, release_id, track_id)


def delete_release_track(user_id: int, release_id: int, track_id: int) -> dict[str, Any] | None:
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as connection:
        row = connection.execute(
            """
            SELECT rt.*, r.status AS release_status
            FROM release_tracks AS rt
            JOIN releases AS r ON r.id = rt.release_id
            WHERE rt.id = ? AND rt.release_id = ? AND rt.user_id = ? AND r.user_id = ?
            """,
            (track_id, release_id, user_id, user_id),
        ).fetchone()
        if not row or str(row["release_status"]) not in RELEASE_EDITABLE_STATUSES:
            return None
        item = dict(row)
        connection.execute("DELETE FROM release_tracks WHERE id = ?", (track_id,))
        remaining = connection.execute(
            "SELECT id FROM release_tracks WHERE release_id = ? ORDER BY position, id",
            (release_id,),
        ).fetchall()
        for index, remaining_row in enumerate(remaining, start=1):
            connection.execute(
                "UPDATE release_tracks SET position = ?, updated_at = ? WHERE id = ?",
                (index, now, int(remaining_row["id"])),
            )
        connection.execute(
            "UPDATE releases SET updated_at = ?, status = 'draft' WHERE id = ? AND user_id = ?",
            (now, release_id, user_id),
        )
        connection.commit()
    return item


def submit_release_for_moderation(
    user_id: int,
    release_id: int,
    *,
    moderator_comment: str,
    release_soon: bool,
) -> tuple[bool, str]:
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as connection:
        release = connection.execute(
            """
            SELECT status, title, genre, cover_path, release_date
            FROM releases WHERE id = ? AND user_id = ?
            """,
            (release_id, user_id),
        ).fetchone()
        if not release:
            return False, "Релиз не найден."
        if str(release["status"]) not in RELEASE_EDITABLE_STATUSES:
            return False, "Этот релиз уже отправлен и недоступен для изменения."
        if not str(release["title"] or "").strip() or not str(release["genre"] or "").strip():
            return False, "Заполните основную информацию о релизе."
        if not str(release["cover_path"] or "").strip():
            return False, "Загрузите обложку релиза."
        if not str(release["release_date"] or "").strip():
            return False, "Укажите планируемую дату выхода релиза."
        try:
            planned_date = date.fromisoformat(str(release["release_date"]))
        except ValueError:
            return False, "Проверьте планируемую дату выхода релиза."
        if planned_date <= date.today():
            return False, "Дата выхода должна быть позже сегодняшнего дня."
        track_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM release_tracks WHERE release_id = ? AND user_id = ?",
                (release_id, user_id),
            ).fetchone()[0]
        )
        if track_count < 1:
            return False, "Добавьте хотя бы один трек."

        connection.execute(
            """
            UPDATE releases
            SET status = 'moderation', moderator_comment = ?, release_soon = ?,
                submitted_at = ?, rejection_reason = NULL, updated_at = ?
            WHERE id = ? AND user_id = ?
            """,
            (
                moderator_comment or None,
                1 if release_soon else 0,
                now,
                now,
                release_id,
                user_id,
            ),
        )
        connection.commit()
    return True, "Релиз отправлен на модерацию."


def list_user_releases(
    user_id: int,
    *,
    query: str = "",
    status_filter: str = "all",
) -> dict[str, Any]:
    clean_query = query.strip()
    where = ["r.user_id = ?"]
    params: list[Any] = [user_id]
    if clean_query:
        where.append("(r.title LIKE ? COLLATE NOCASE OR r.artist_name LIKE ? COLLATE NOCASE OR COALESCE(r.upc, '') LIKE ?)")
        pattern = f"%{clean_query}%"
        params.extend([pattern, pattern, pattern])
    if status_filter in {"draft", "moderation", "changes_required", "rejected", "accepted", "published"}:
        where.append("r.status = ?")
        params.append(status_filter)
    elif status_filter == "published_all":
        where.append("r.status IN ('accepted', 'published')")

    with _connect() as connection:
        rows = connection.execute(
            f"""
            SELECT r.*, COUNT(rt.id) AS track_count
            FROM releases AS r
            LEFT JOIN release_tracks AS rt ON rt.release_id = r.id
            WHERE {' AND '.join(where)}
            GROUP BY r.id
            ORDER BY datetime(r.updated_at) DESC, r.id DESC
            """,
            params,
        ).fetchall()
        status_rows = connection.execute(
            "SELECT status, COUNT(*) AS total FROM releases WHERE user_id = ? GROUP BY status",
            (user_id,),
        ).fetchall()

    status_counts = {str(row["status"]): int(row["total"]) for row in status_rows}
    items = [_release_dict(row) for row in rows]
    total = sum(status_counts.values())
    return {
        "items": items,
        "query": clean_query,
        "status_filter": status_filter,
        "counts": {
            "all": total,
            "published": status_counts.get("accepted", 0) + status_counts.get("published", 0),
            "moderation": status_counts.get("moderation", 0),
            "rejected": status_counts.get("rejected", 0) + status_counts.get("changes_required", 0),
            "changes_required": status_counts.get("changes_required", 0),
            "draft": status_counts.get("draft", 0),
        },
    }


def delete_release(user_id: int, release_id: int) -> dict[str, Any] | None:
    with _connect() as connection:
        release = connection.execute(
            "SELECT * FROM releases WHERE id = ? AND user_id = ?",
            (release_id, user_id),
        ).fetchone()
        if not release or str(release["status"]) not in RELEASE_EDITABLE_STATUSES:
            return None
        tracks = connection.execute(
            "SELECT audio_path FROM release_tracks WHERE release_id = ? AND user_id = ?",
            (release_id, user_id),
        ).fetchall()
        result = dict(release)
        result["audio_paths"] = [str(row["audio_path"]) for row in tracks]
        connection.execute("DELETE FROM releases WHERE id = ? AND user_id = ?", (release_id, user_id))
        connection.commit()
    return result


# Support, pitching and release rules
SUPPORT_CATEGORIES = {"Общий вопрос", "Релизы", "Аудио", "Права", "Питчинг", "Аккаунт"}
SUPPORT_OPEN_STATUSES = {"open", "in_progress", "answered"}
PITCHING_MIN_DAYS = 12


def _date_time_label(value: str | None) -> str:
    if not value:
        return "—"
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return "—"
    return parsed.strftime("%d.%m.%Y · %H:%M")


def _support_status_meta(value: str) -> dict[str, str]:
    mapping = {
        "open": {"label": "Новая", "class": "warning", "description": "Заявка ожидает ответа поддержки."},
        "in_progress": {"label": "В работе", "class": "info", "description": "Администратор уже занимается вопросом."},
        "answered": {"label": "Есть ответ", "class": "success", "description": "Поддержка ответила. Можно продолжить диалог."},
        "closed": {"label": "Закрыта", "class": "draft", "description": "Диалог завершён. При необходимости создайте новую заявку."},
    }
    return mapping.get(value, {"label": value.replace("_", " ").title(), "class": "draft", "description": ""})


def _pitching_status_meta(value: str) -> dict[str, str]:
    mapping = {
        "submitted": {"label": "Отправлена", "class": "warning"},
        "in_review": {"label": "На рассмотрении", "class": "info"},
        "approved": {"label": "Принята", "class": "success"},
        "rejected": {"label": "Отклонена", "class": "danger"},
        "cancelled": {"label": "Отменена", "class": "draft"},
        "draft": {"label": "Черновик", "class": "draft"},
    }
    return mapping.get(value, {"label": value.replace("_", " ").title(), "class": "draft"})


def list_support_faqs() -> list[dict[str, Any]]:
    with _connect() as connection:
        rows = connection.execute(
            """
            SELECT id, category, question, answer
            FROM support_faqs
            WHERE is_active = 1
            ORDER BY sort_order, id
            """
        ).fetchall()
    return [dict(row) for row in rows]


def create_support_ticket(
    user_id: int,
    *,
    subject: str,
    category: str,
    message: str,
) -> int:
    now = datetime.now(timezone.utc).isoformat()
    safe_category = category if category in SUPPORT_CATEGORIES else "Общий вопрос"
    with _connect() as connection:
        cursor = connection.execute(
            """
            INSERT INTO support_tickets (
                user_id, subject, category, status, priority,
                created_at, updated_at, last_message_at
            ) VALUES (?, ?, ?, 'open', 'normal', ?, ?, ?)
            """,
            (user_id, subject, safe_category, now, now, now),
        )
        ticket_id = int(cursor.lastrowid)
        connection.execute(
            """
            INSERT INTO support_messages (
                ticket_id, sender_type, user_id, body, is_read, created_at
            ) VALUES (?, 'user', ?, ?, 1, ?)
            """,
            (ticket_id, user_id, message, now),
        )
        connection.commit()
    return ticket_id


def _support_ticket_dict(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["status_meta"] = _support_status_meta(str(item.get("status") or "open"))
    item["created_at_label"] = _date_time_label(item.get("created_at"))
    item["updated_at_label"] = _date_time_label(item.get("updated_at"))
    item["public_id"] = f"SUP-{int(item['id']):05d}"
    item["is_open"] = str(item.get("status")) in SUPPORT_OPEN_STATUSES
    item["unread_count"] = int(item.get("unread_count") or 0)
    item["message_count"] = int(item.get("message_count") or 0)
    return item


def list_user_support_tickets(
    user_id: int,
    *,
    status_filter: str = "all",
) -> dict[str, Any]:
    where = ["t.user_id = ?"]
    params: list[Any] = [user_id]
    if status_filter in {"open", "in_progress", "answered", "closed"}:
        where.append("t.status = ?")
        params.append(status_filter)
    with _connect() as connection:
        rows = connection.execute(
            f"""
            SELECT t.*,
                   COUNT(m.id) AS message_count,
                   SUM(CASE WHEN m.sender_type = 'admin' AND m.is_read = 0 THEN 1 ELSE 0 END) AS unread_count,
                   (
                       SELECT sm.body FROM support_messages AS sm
                       WHERE sm.ticket_id = t.id
                       ORDER BY datetime(sm.created_at) DESC, sm.id DESC LIMIT 1
                   ) AS last_message
            FROM support_tickets AS t
            LEFT JOIN support_messages AS m ON m.ticket_id = t.id
            WHERE {' AND '.join(where)}
            GROUP BY t.id
            ORDER BY datetime(t.updated_at) DESC, t.id DESC
            """,
            params,
        ).fetchall()
        count_rows = connection.execute(
            "SELECT status, COUNT(*) AS total FROM support_tickets WHERE user_id = ? GROUP BY status",
            (user_id,),
        ).fetchall()
    counts = {str(row["status"]): int(row["total"]) for row in count_rows}
    return {
        "items": [_support_ticket_dict(row) for row in rows],
        "status_filter": status_filter,
        "counts": {
            "all": sum(counts.values()),
            "open": counts.get("open", 0),
            "in_progress": counts.get("in_progress", 0),
            "answered": counts.get("answered", 0),
            "closed": counts.get("closed", 0),
        },
    }


def get_user_support_ticket(
    user_id: int,
    ticket_id: int,
    *,
    mark_read: bool = True,
) -> dict[str, Any] | None:
    with _connect() as connection:
        row = connection.execute(
            """
            SELECT t.*,
                   COUNT(m.id) AS message_count,
                   SUM(CASE WHEN m.sender_type = 'admin' AND m.is_read = 0 THEN 1 ELSE 0 END) AS unread_count
            FROM support_tickets AS t
            LEFT JOIN support_messages AS m ON m.ticket_id = t.id
            WHERE t.id = ? AND t.user_id = ?
            GROUP BY t.id
            """,
            (ticket_id, user_id),
        ).fetchone()
        if not row:
            return None
        if mark_read:
            connection.execute(
                "UPDATE support_messages SET is_read = 1 WHERE ticket_id = ? AND sender_type = 'admin'",
                (ticket_id,),
            )
            connection.commit()
        messages = connection.execute(
            """
            SELECT m.*, u.name AS sender_name
            FROM support_messages AS m
            LEFT JOIN users AS u ON u.id = m.user_id
            WHERE m.ticket_id = ?
            ORDER BY datetime(m.created_at), m.id
            """,
            (ticket_id,),
        ).fetchall()
    item = _support_ticket_dict(row)
    item["messages"] = [
        {
            **dict(message),
            "created_at_label": _date_time_label(message["created_at"]),
            "sender_label": "Поддержка RAMIX MUSIC" if message["sender_type"] == "admin" else "Вы",
        }
        for message in messages
    ]
    item["unread_count"] = 0 if mark_read else item["unread_count"]
    return item


def add_user_support_message(user_id: int, ticket_id: int, body: str) -> tuple[bool, str]:
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as connection:
        ticket = connection.execute(
            "SELECT status FROM support_tickets WHERE id = ? AND user_id = ?",
            (ticket_id, user_id),
        ).fetchone()
        if not ticket:
            return False, "Заявка не найдена."
        if str(ticket["status"]) == "closed":
            return False, "Эта заявка закрыта. Создайте новую, если вопрос остался."
        connection.execute(
            """
            INSERT INTO support_messages (
                ticket_id, sender_type, user_id, body, is_read, created_at
            ) VALUES (?, 'user', ?, ?, 1, ?)
            """,
            (ticket_id, user_id, body, now),
        )
        connection.execute(
            """
            UPDATE support_tickets
            SET status = 'open', updated_at = ?, last_message_at = ?, closed_at = NULL
            WHERE id = ?
            """,
            (now, now, ticket_id),
        )
        connection.commit()
    return True, "Сообщение отправлено."


def close_user_support_ticket(user_id: int, ticket_id: int) -> bool:
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as connection:
        cursor = connection.execute(
            """
            UPDATE support_tickets
            SET status = 'closed', updated_at = ?, closed_at = ?
            WHERE id = ? AND user_id = ? AND status != 'closed'
            """,
            (now, now, ticket_id, user_id),
        )
        connection.commit()
        return cursor.rowcount == 1


def list_admin_support_tickets(status_filter: str = "all") -> dict[str, Any]:
    where = ["1 = 1"]
    params: list[Any] = []
    if status_filter in {"open", "in_progress", "answered", "closed"}:
        where.append("t.status = ?")
        params.append(status_filter)
    with _connect() as connection:
        rows = connection.execute(
            f"""
            SELECT t.*, u.name AS user_name, u.email AS user_email,
                   COUNT(m.id) AS message_count,
                   SUM(CASE WHEN m.sender_type = 'user' AND m.is_read = 0 THEN 1 ELSE 0 END) AS unread_count
            FROM support_tickets AS t
            JOIN users AS u ON u.id = t.user_id
            LEFT JOIN support_messages AS m ON m.ticket_id = t.id
            WHERE {' AND '.join(where)}
            GROUP BY t.id
            ORDER BY CASE t.status WHEN 'open' THEN 0 WHEN 'in_progress' THEN 1 WHEN 'answered' THEN 2 ELSE 3 END,
                     datetime(t.updated_at) DESC
            """,
            params,
        ).fetchall()
    return {"items": [_support_ticket_dict(row) for row in rows], "status_filter": status_filter}


def get_admin_support_ticket(ticket_id: int, *, mark_read: bool = True) -> dict[str, Any] | None:
    with _connect() as connection:
        row = connection.execute(
            """
            SELECT t.*, u.name AS user_name, u.email AS user_email,
                   COUNT(m.id) AS message_count,
                   SUM(CASE WHEN m.sender_type = 'user' AND m.is_read = 0 THEN 1 ELSE 0 END) AS unread_count
            FROM support_tickets AS t
            JOIN users AS u ON u.id = t.user_id
            LEFT JOIN support_messages AS m ON m.ticket_id = t.id
            WHERE t.id = ?
            GROUP BY t.id
            """,
            (ticket_id,),
        ).fetchone()
        if not row:
            return None
        if mark_read:
            connection.execute(
                "UPDATE support_messages SET is_read = 1 WHERE ticket_id = ? AND sender_type = 'user'",
                (ticket_id,),
            )
            connection.commit()
        messages = connection.execute(
            """
            SELECT m.*, u.name AS sender_name
            FROM support_messages AS m
            LEFT JOIN users AS u ON u.id = m.user_id
            WHERE m.ticket_id = ?
            ORDER BY datetime(m.created_at), m.id
            """,
            (ticket_id,),
        ).fetchall()
    item = _support_ticket_dict(row)
    item["messages"] = [
        {
            **dict(message),
            "created_at_label": _date_time_label(message["created_at"]),
            "sender_label": "Администратор" if message["sender_type"] == "admin" else str(message["sender_name"] or "Пользователь"),
        }
        for message in messages
    ]
    return item


def admin_reply_support_ticket(
    admin_user_id: int,
    ticket_id: int,
    *,
    body: str,
    status_value: str = "answered",
) -> tuple[bool, str]:
    if status_value not in {"in_progress", "answered", "closed"}:
        status_value = "answered"
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as connection:
        ticket = connection.execute("SELECT id FROM support_tickets WHERE id = ?", (ticket_id,)).fetchone()
        if not ticket:
            return False, "Заявка не найдена."
        connection.execute(
            """
            INSERT INTO support_messages (
                ticket_id, sender_type, user_id, body, is_read, created_at
            ) VALUES (?, 'admin', ?, ?, 0, ?)
            """,
            (ticket_id, admin_user_id, body, now),
        )
        connection.execute(
            """
            UPDATE support_tickets
            SET status = ?, updated_at = ?, last_message_at = ?,
                closed_at = CASE WHEN ? = 'closed' THEN ? ELSE NULL END
            WHERE id = ?
            """,
            (status_value, now, now, status_value, now, ticket_id),
        )
        connection.commit()
    return True, "Ответ сохранён и доступен пользователю."


def admin_update_support_ticket_status(ticket_id: int, status_value: str) -> bool:
    if status_value not in {"open", "in_progress", "answered", "closed"}:
        return False
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as connection:
        cursor = connection.execute(
            """
            UPDATE support_tickets
            SET status = ?, updated_at = ?,
                closed_at = CASE WHEN ? = 'closed' THEN ? ELSE NULL END
            WHERE id = ?
            """,
            (status_value, now, status_value, now, ticket_id),
        )
        connection.commit()
        return cursor.rowcount == 1


def _pitching_request_dict(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["status_meta"] = _pitching_status_meta(str(item.get("status") or "submitted"))
    item["created_at_label"] = _date_time_label(item.get("created_at"))
    item["updated_at_label"] = _date_time_label(item.get("updated_at"))
    item["release_date_label"] = _date_label(item.get("release_date")) if item.get("release_date") else "Не указана"
    item["public_id"] = f"PIT-{int(item['id']):05d}"
    return item



def list_pitching_resources() -> list[dict[str, Any]]:
    """Return active external artist resources shown in the user cabinet."""
    with _connect() as connection:
        rows = connection.execute(
            """
            SELECT id, name, slug, logo_path, support_url, description
            FROM pitching_platforms
            WHERE is_active = 1
              AND support_url IS NOT NULL
              AND trim(support_url) NOT IN ('', '#')
            ORDER BY sort_order ASC, name ASC
            """
        ).fetchall()
    return [dict(row) for row in rows]

def get_pitching_page_data(user_id: int) -> dict[str, Any]:
    minimum_date = date.today() + timedelta(days=PITCHING_MIN_DAYS)
    with _connect() as connection:
        releases = connection.execute(
            """
            SELECT id, title, artist_name, release_date, cover_path
            FROM releases
            WHERE user_id = ?
              AND status = 'accepted'
              AND release_date IS NOT NULL
              AND date(release_date) >= date(?)
            ORDER BY date(release_date), id
            """,
            (user_id, minimum_date.isoformat()),
        ).fetchall()
        platforms = connection.execute(
            """
            SELECT id, name, slug, logo_path
            FROM pitching_platforms
            WHERE is_active = 1
            ORDER BY sort_order, name
            """
        ).fetchall()
        requests = connection.execute(
            """
            SELECT pr.*, r.title AS release_title, r.artist_name, r.release_date,
                   pp.name AS platform_name, pp.logo_path
            FROM pitching_requests AS pr
            JOIN releases AS r ON r.id = pr.release_id
            JOIN pitching_platforms AS pp ON pp.id = pr.platform_id
            WHERE pr.user_id = ?
            ORDER BY datetime(pr.updated_at) DESC, pr.id DESC
            """,
            (user_id,),
        ).fetchall()
    release_items = []
    for row in releases:
        item = dict(row)
        item["release_date_label"] = _date_label(item.get("release_date"))
        item["cover_url"] = _cover_url(int(item["id"]), item.get("cover_path"))
        release_items.append(item)
    return {
        "eligible_releases": release_items,
        "platforms": [dict(row) for row in platforms],
        "requests": [_pitching_request_dict(row) for row in requests],
        "minimum_date_label": minimum_date.strftime("%d.%m.%Y"),
        "minimum_days": PITCHING_MIN_DAYS,
    }


def create_pitching_request(
    user_id: int,
    *,
    release_id: int,
    platform_id: int,
    message: str,
) -> tuple[bool, str]:
    minimum_date = date.today() + timedelta(days=PITCHING_MIN_DAYS)
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as connection:
        release = connection.execute(
            """
            SELECT id FROM releases
            WHERE id = ? AND user_id = ? AND status = 'accepted'
              AND release_date IS NOT NULL AND date(release_date) >= date(?)
            """,
            (release_id, user_id, minimum_date.isoformat()),
        ).fetchone()
        if not release:
            return False, f"Для питчинга нужен принятый релиз с датой выхода не раньше {minimum_date.strftime('%d.%m.%Y')}."
        platform = connection.execute(
            "SELECT id FROM pitching_platforms WHERE id = ? AND is_active = 1",
            (platform_id,),
        ).fetchone()
        if not platform:
            return False, "Выбранная площадка сейчас недоступна."
        duplicate = connection.execute(
            """
            SELECT id FROM pitching_requests
            WHERE user_id = ? AND release_id = ? AND platform_id = ?
              AND status IN ('submitted', 'in_review', 'approved')
            """,
            (user_id, release_id, platform_id),
        ).fetchone()
        if duplicate:
            return False, "Заявка на этот релиз и площадку уже существует."
        connection.execute(
            """
            INSERT INTO pitching_requests (
                user_id, release_id, platform_id, status, message,
                submitted_at, created_at, updated_at
            ) VALUES (?, ?, ?, 'submitted', ?, ?, ?, ?)
            """,
            (user_id, release_id, platform_id, message or None, now, now, now),
        )
        connection.commit()
    return True, "Заявка на питчинг отправлена."


def list_admin_pitching_requests(status_filter: str = "all") -> list[dict[str, Any]]:
    where = ["1 = 1"]
    params: list[Any] = []
    if status_filter in {"submitted", "in_review", "approved", "rejected", "cancelled"}:
        where.append("pr.status = ?")
        params.append(status_filter)
    with _connect() as connection:
        rows = connection.execute(
            f"""
            SELECT pr.*, r.title AS release_title, r.artist_name, r.release_date,
                   pp.name AS platform_name, pp.logo_path,
                   u.name AS user_name, u.email AS user_email
            FROM pitching_requests AS pr
            JOIN releases AS r ON r.id = pr.release_id
            JOIN pitching_platforms AS pp ON pp.id = pr.platform_id
            JOIN users AS u ON u.id = pr.user_id
            WHERE {' AND '.join(where)}
            ORDER BY CASE pr.status WHEN 'submitted' THEN 0 WHEN 'in_review' THEN 1 ELSE 2 END,
                     datetime(pr.updated_at) DESC
            """,
            params,
        ).fetchall()
    return [_pitching_request_dict(row) for row in rows]


def admin_update_pitching_request(
    admin_user_id: int,
    request_id: int,
    *,
    status_value: str,
    admin_comment: str,
) -> tuple[bool, str]:
    if status_value not in {"submitted", "in_review", "approved", "rejected", "cancelled"}:
        return False, "Выберите корректный статус."
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as connection:
        cursor = connection.execute(
            """
            UPDATE pitching_requests
            SET status = ?, admin_comment = ?, reviewed_at = ?, reviewed_by = ?, updated_at = ?
            WHERE id = ?
            """,
            (status_value, admin_comment or None, now, admin_user_id, now, request_id),
        )
        connection.commit()
        if cursor.rowcount != 1:
            return False, "Заявка не найдена."
    return True, "Статус заявки обновлён."


def list_release_rule_sections() -> list[dict[str, Any]]:
    with _connect() as connection:
        rows = connection.execute(
            """
            SELECT step_number, title, intro, items_text, note
            FROM release_rule_sections
            WHERE is_active = 1
            ORDER BY step_number
            """
        ).fetchall()
    return [
        {
            **dict(row),
            "items": [line.strip() for line in str(row["items_text"]).splitlines() if line.strip()],
        }
        for row in rows
    ]
