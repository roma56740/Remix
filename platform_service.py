from __future__ import annotations

import json
import math
import sqlite3
from datetime import date, datetime, timedelta, timezone
from typing import Any

from database import (
    PITCHING_MIN_DAYS,
    _connect,
    _cover_url,
    _date_label,
    _format_compact,
    _format_money,
    _language_label,
    _operation_status_meta,
    _release_status_meta,
    _release_type_label,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _date_time_label(value: str | None) -> str:
    if not value:
        return "—"
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return "—"
    return parsed.astimezone().strftime("%d.%m.%Y, %H:%M")


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _page_meta(total: int, page: int, per_page: int) -> dict[str, int | bool]:
    pages = max(1, math.ceil(total / per_page))
    current = min(max(page, 1), pages)
    return {
        "page": current,
        "pages": pages,
        "total": total,
        "per_page": per_page,
        "has_previous": current > 1,
        "has_next": current < pages,
        "previous": max(1, current - 1),
        "next": min(pages, current + 1),
    }


def _audit(
    connection: sqlite3.Connection,
    admin_user_id: int | None,
    action: str,
    entity_type: str,
    entity_id: int | None,
    details: dict[str, Any] | str | None = None,
) -> None:
    if isinstance(details, dict):
        details_value = json.dumps(details, ensure_ascii=False)
    else:
        details_value = details
    connection.execute(
        """
        INSERT INTO admin_audit_log (
            admin_user_id, action, entity_type, entity_id, details, created_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (admin_user_id, action, entity_type, entity_id, details_value, _now()),
    )


def mark_user_login(user_id: int) -> None:
    with _connect() as connection:
        connection.execute(
            "UPDATE users SET last_login_at = ?, updated_at = ? WHERE id = ?",
            (_now(), _now(), user_id),
        )
        connection.commit()


def get_unread_notification_count(user_id: int) -> int:
    with _connect() as connection:
        return int(
            connection.execute(
                "SELECT COUNT(*) FROM user_notifications WHERE user_id = ? AND is_read = 0",
                (user_id,),
            ).fetchone()[0]
        )


def create_notification(
    connection: sqlite3.Connection,
    user_id: int,
    *,
    title: str,
    body: str,
    notification_type: str = "info",
    action_url: str | None = None,
) -> None:
    connection.execute(
        """
        INSERT INTO user_notifications (
            user_id, title, body, notification_type, action_url, is_read, created_at
        ) VALUES (?, ?, ?, ?, ?, 0, ?)
        """,
        (user_id, title, body, notification_type, action_url, _now()),
    )


def list_notifications(user_id: int, page: int = 1, per_page: int = 20) -> dict[str, Any]:
    with _connect() as connection:
        total = int(
            connection.execute(
                "SELECT COUNT(*) FROM user_notifications WHERE user_id = ?",
                (user_id,),
            ).fetchone()[0]
        )
        pagination = _page_meta(total, page, per_page)
        rows = connection.execute(
            """
            SELECT *
            FROM user_notifications
            WHERE user_id = ?
            ORDER BY is_read ASC, datetime(created_at) DESC, id DESC
            LIMIT ? OFFSET ?
            """,
            (
                user_id,
                per_page,
                (int(pagination["page"]) - 1) * per_page,
            ),
        ).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        item["created_at_label"] = _date_time_label(item.get("created_at"))
        items.append(item)
    return {"items": items, "pagination": pagination}


def mark_notifications_read(user_id: int) -> None:
    now = _now()
    with _connect() as connection:
        connection.execute(
            """
            UPDATE user_notifications
            SET is_read = 1, read_at = COALESCE(read_at, ?)
            WHERE user_id = ? AND is_read = 0
            """,
            (now, user_id),
        )
        connection.commit()


def _period_bounds(period: str) -> tuple[date, date, int]:
    today = date.today()
    days = {"7": 7, "30": 30, "90": 90, "365": 365}.get(period, 30)
    return today - timedelta(days=days - 1), today, days


def _daily_chart(
    connection: sqlite3.Connection,
    user_id: int,
    start: date,
    end: date,
) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT listen_date, SUM(count) AS total
        FROM listens
        WHERE user_id = ? AND date(listen_date) BETWEEN date(?) AND date(?)
        GROUP BY listen_date
        """,
        (user_id, start.isoformat(), end.isoformat()),
    ).fetchall()
    lookup = {str(row["listen_date"]): int(row["total"] or 0) for row in rows}
    result: list[dict[str, Any]] = []
    cursor = start
    total_days = (end - start).days + 1
    label_step = 1 if total_days <= 14 else 3 if total_days <= 45 else 10 if total_days <= 120 else 30
    index = 0
    while cursor <= end:
        value = max(0, lookup.get(cursor.isoformat(), 0))
        label = cursor.strftime("%d.%m") if index % label_step == 0 or cursor == end else ""
        result.append({"label": label, "full_label": cursor.strftime("%d.%m.%Y"), "value": value})
        cursor += timedelta(days=1)
        index += 1
    return result


def get_statistics_page_data(user_id: int, period: str = "30") -> dict[str, Any]:
    period = period if period in {"7", "30", "90", "365"} else "30"
    start, end, days = _period_bounds(period)
    previous_end = start - timedelta(days=1)
    previous_start = previous_end - timedelta(days=days - 1)
    with _connect() as connection:
        period_total = int(
            connection.execute(
                """
                SELECT COALESCE(SUM(count), 0)
                FROM listens
                WHERE user_id = ? AND date(listen_date) BETWEEN date(?) AND date(?)
                """,
                (user_id, start.isoformat(), end.isoformat()),
            ).fetchone()[0]
        )
        previous_total = int(
            connection.execute(
                """
                SELECT COALESCE(SUM(count), 0)
                FROM listens
                WHERE user_id = ? AND date(listen_date) BETWEEN date(?) AND date(?)
                """,
                (user_id, previous_start.isoformat(), previous_end.isoformat()),
            ).fetchone()[0]
        )
        all_time = int(
            connection.execute(
                "SELECT COALESCE(SUM(count), 0) FROM listens WHERE user_id = ?",
                (user_id,),
            ).fetchone()[0]
        )
        published_count = int(
            connection.execute(
                """
                SELECT COUNT(*) FROM releases
                WHERE user_id = ? AND status IN ('accepted', 'published')
                """,
                (user_id,),
            ).fetchone()[0]
        )
        release_rows = connection.execute(
            """
            SELECT r.id, r.title, r.artist_name, r.cover_path, r.status,
                   COALESCE(SUM(l.count), 0) AS listens
            FROM releases AS r
            LEFT JOIN listens AS l
              ON l.release_id = r.id
             AND date(l.listen_date) BETWEEN date(?) AND date(?)
            WHERE r.user_id = ?
            GROUP BY r.id
            HAVING COALESCE(SUM(l.count), 0) > 0
            ORDER BY listens DESC, r.title COLLATE NOCASE
            LIMIT 10
            """,
            (start.isoformat(), end.isoformat(), user_id),
        ).fetchall()
        track_rows = connection.execute(
            """
            SELECT rt.id, rt.title, rt.artists, r.title AS release_title,
                   r.id AS release_id, r.cover_path,
                   COALESCE(SUM(tl.count), 0) AS listens
            FROM release_tracks AS rt
            JOIN releases AS r ON r.id = rt.release_id
            LEFT JOIN track_listens AS tl
              ON tl.track_id = rt.id
             AND date(tl.listen_date) BETWEEN date(?) AND date(?)
            WHERE rt.user_id = ?
            GROUP BY rt.id
            HAVING COALESCE(SUM(tl.count), 0) > 0
            ORDER BY listens DESC, rt.title COLLATE NOCASE
            LIMIT 10
            """,
            (start.isoformat(), end.isoformat(), user_id),
        ).fetchall()
        chart = _daily_chart(connection, user_id, start, end)

    if previous_total > 0:
        growth = round((period_total - previous_total) / previous_total * 100)
    elif period_total > 0:
        growth = 100
    else:
        growth = 0
    releases = []
    for row in release_rows:
        item = dict(row)
        item["cover_url"] = _cover_url(int(item["id"]), item.get("cover_path"))
        item["listens_label"] = f"{int(item['listens']):,}".replace(",", " ")
        releases.append(item)
    tracks = []
    for row in track_rows:
        item = dict(row)
        item["cover_url"] = _cover_url(int(item["release_id"]), item.get("cover_path"))
        item["listens_label"] = f"{int(item['listens']):,}".replace(",", " ")
        tracks.append(item)
    return {
        "period": period,
        "period_label": {"7": "7 дней", "30": "30 дней", "90": "90 дней", "365": "Год"}[period],
        "chart": chart,
        "chart_payload": {period: chart},
        "has_data": period_total > 0,
        "summary": {
            "period_total": period_total,
            "period_total_label": _format_compact(period_total),
            "all_time": all_time,
            "all_time_label": _format_compact(all_time),
            "growth": growth,
            "growth_label": f"{growth:+d}%" if growth else "0%",
            "published": published_count,
            "top_release": releases[0] if releases else None,
        },
        "releases": releases,
        "tracks": tracks,
    }


def _payout_status_meta(value: str) -> dict[str, str]:
    return {
        "pending": {"label": "Ожидает", "class": "warning"},
        "processing": {"label": "В обработке", "class": "warning"},
        "paid": {"label": "Выплачено", "class": "success"},
        "rejected": {"label": "Отклонено", "class": "danger"},
        "cancelled": {"label": "Отменено", "class": "danger"},
    }.get(value, {"label": value.title(), "class": "draft"})


def get_balance_page_data(user_id: int, status_filter: str = "all") -> dict[str, Any]:
    valid_statuses = {"all", "completed", "processing", "cancelled", "rejected"}
    if status_filter not in valid_statuses:
        status_filter = "all"
    with _connect() as connection:
        user = connection.execute(
            """
            SELECT balance, pending_balance, last_payout_at, telegram_connected
            FROM users WHERE id = ?
            """,
            (user_id,),
        ).fetchone()
        where = ["bt.user_id = ?"]
        params: list[Any] = [user_id]
        if status_filter != "all":
            where.append("bt.status = ?")
            params.append(status_filter)
        rows = connection.execute(
            f"""
            SELECT bt.*, r.title AS release_title
            FROM balance_transactions AS bt
            LEFT JOIN releases AS r ON r.id = bt.release_id
            WHERE {' AND '.join(where)}
            ORDER BY datetime(bt.created_at) DESC, bt.id DESC
            LIMIT 100
            """,
            params,
        ).fetchall()
        payout_rows = connection.execute(
            """
            SELECT * FROM payout_requests
            WHERE user_id = ?
            ORDER BY datetime(created_at) DESC, id DESC
            LIMIT 20
            """,
            (user_id,),
        ).fetchall()
        income_month = int(
            connection.execute(
                """
                SELECT COALESCE(SUM(amount), 0)
                FROM balance_transactions
                WHERE user_id = ? AND amount > 0 AND status = 'completed'
                  AND strftime('%Y-%m', created_at) = strftime('%Y-%m', 'now')
                """,
                (user_id,),
            ).fetchone()[0]
        )
    operations = []
    for row in rows:
        item = dict(row)
        item["created_at_label"] = _date_time_label(item.get("created_at"))
        item["status_meta"] = _operation_status_meta(str(item.get("status") or "completed"))
        item["amount_label"] = _format_money(item.get("amount"), show_plus=True)
        operations.append(item)
    payouts = []
    for row in payout_rows:
        item = dict(row)
        item["created_at_label"] = _date_time_label(item.get("created_at"))
        item["status_meta"] = _payout_status_meta(str(item.get("status") or "pending"))
        item["amount_label"] = _format_money(item.get("amount"))
        payouts.append(item)
    available = int(user["balance"] or 0) if user else 0
    pending = int(user["pending_balance"] or 0) if user else 0
    active_payout = next((item for item in payouts if item["status"] in {"pending", "processing"}), None)
    return {
        "status_filter": status_filter,
        "available": available,
        "available_label": _format_money(available),
        "pending": pending,
        "pending_label": _format_money(pending),
        "income_month": income_month,
        "income_month_label": _format_money(income_month, show_plus=True),
        "last_payout_label": _date_label(user["last_payout_at"] if user else None),
        "telegram_connected": bool(user["telegram_connected"] if user else False),
        "operations": operations,
        "payouts": payouts,
        "active_payout": active_payout,
        "can_request": available >= 1000 and active_payout is None,
        "minimum_payout": 1000,
    }


def create_payout_request(
    user_id: int,
    *,
    amount: int,
    method: str,
    details: str,
) -> tuple[bool, str]:
    if amount < 1000:
        return False, "Минимальная сумма вывода — 1 000 ₽."
    if method not in {"telegram", "bank", "other"}:
        return False, "Выберите способ получения выплаты."
    if len(details.strip()) < 3:
        return False, "Укажите реквизиты или контакт для выплаты."
    now = _now()
    with _connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        user = connection.execute(
            "SELECT balance FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        if not user:
            connection.rollback()
            return False, "Пользователь не найден."
        active = connection.execute(
            """
            SELECT id FROM payout_requests
            WHERE user_id = ? AND status IN ('pending', 'processing')
            """,
            (user_id,),
        ).fetchone()
        if active:
            connection.rollback()
            return False, "У вас уже есть активная заявка на вывод."
        balance = int(user["balance"] or 0)
        if amount > balance:
            connection.rollback()
            return False, "На балансе недостаточно средств."
        connection.execute(
            """
            UPDATE users
            SET balance = balance - ?, pending_balance = pending_balance + ?, updated_at = ?
            WHERE id = ?
            """,
            (amount, amount, now, user_id),
        )
        transaction = connection.execute(
            """
            INSERT INTO balance_transactions (
                user_id, release_id, transaction_type, status, amount, title, created_at
            ) VALUES (?, NULL, 'payout', 'processing', ?, 'Заявка на вывод средств', ?)
            """,
            (user_id, -amount, now),
        )
        connection.execute(
            """
            INSERT INTO payout_requests (
                user_id, amount, method, details, status, transaction_id,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'pending', ?, ?, ?)
            """,
            (user_id, amount, method, details.strip(), int(transaction.lastrowid), now, now),
        )
        connection.commit()
    return True, "Заявка на вывод создана. Статус будет обновляться на этой странице."


def _admin_release_row(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["status_meta"] = _release_status_meta(str(item.get("status") or "draft"))
    item["release_type_label"] = _release_type_label(str(item.get("release_type") or "single"))
    item["cover_url"] = _cover_url(int(item["id"]), item.get("cover_path"))
    item["updated_at_label"] = _date_time_label(item.get("updated_at"))
    item["submitted_at_label"] = _date_time_label(item.get("submitted_at"))
    item["release_date_label"] = _date_label(item.get("release_date")) if item.get("release_date") else "Не назначена"
    item["listens_label"] = f"{int(item.get('listens') or 0):,}".replace(",", " ")
    return item


def get_admin_sidebar_counts() -> dict[str, int]:
    with _connect() as connection:
        moderation = int(
            connection.execute(
                "SELECT COUNT(*) FROM releases WHERE status = 'moderation'"
            ).fetchone()[0]
        )
        support = int(
            connection.execute(
                "SELECT COUNT(*) FROM support_tickets WHERE status IN ('open', 'in_progress')"
            ).fetchone()[0]
        )
        pitching = int(
            connection.execute(
                "SELECT COUNT(*) FROM pitching_requests WHERE status IN ('submitted', 'in_review')"
            ).fetchone()[0]
        )
        payouts = int(
            connection.execute(
                "SELECT COUNT(*) FROM payout_requests WHERE status IN ('pending', 'processing')"
            ).fetchone()[0]
        )
    return {"moderation": moderation, "support": support, "pitching": pitching, "payouts": payouts}


def get_admin_overview() -> dict[str, Any]:
    quarter = (date.today().month - 1) // 3 + 1
    with _connect() as connection:
        stats = {
            "moderation": int(connection.execute("SELECT COUNT(*) FROM releases WHERE status = 'moderation'").fetchone()[0]),
            "releases": int(connection.execute("SELECT COUNT(*) FROM releases").fetchone()[0]),
            "users": int(connection.execute("SELECT COUNT(*) FROM users").fetchone()[0]),
            "quarter": f"Q{quarter}",
            "support": int(connection.execute("SELECT COUNT(*) FROM support_tickets WHERE status IN ('open', 'in_progress')").fetchone()[0]),
            "pitching": int(connection.execute("SELECT COUNT(*) FROM pitching_requests WHERE status IN ('submitted', 'in_review')").fetchone()[0]),
        }
        releases = connection.execute(
            """
            SELECT r.*, u.name AS user_name, u.email AS user_email, u.username,
                   COUNT(DISTINCT rt.id) AS track_count,
                   COALESCE((SELECT SUM(l.count) FROM listens l WHERE l.release_id = r.id), 0) AS listens
            FROM releases AS r
            JOIN users AS u ON u.id = r.user_id
            LEFT JOIN release_tracks AS rt ON rt.release_id = r.id
            GROUP BY r.id
            ORDER BY CASE r.status WHEN 'moderation' THEN 0 ELSE 1 END,
                     datetime(COALESCE(r.submitted_at, r.updated_at)) DESC
            LIMIT 6
            """
        ).fetchall()
        audit_rows = connection.execute(
            """
            SELECT a.*, u.name AS admin_name
            FROM admin_audit_log AS a
            LEFT JOIN users AS u ON u.id = a.admin_user_id
            ORDER BY datetime(a.created_at) DESC, a.id DESC
            LIMIT 8
            """
        ).fetchall()
        news_rows = connection.execute(
            """
            SELECT * FROM service_news
            ORDER BY is_active DESC, datetime(published_at) DESC, id DESC
            LIMIT 4
            """
        ).fetchall()
    return {
        "stats": stats,
        "releases": [_admin_release_row(row) for row in releases],
        "audit": [
            {
                **dict(row),
                "created_at_label": _date_time_label(row["created_at"]),
            }
            for row in audit_rows
        ],
        "news": [
            {
                **dict(row),
                "published_at_label": _date_time_label(row["published_at"]),
            }
            for row in news_rows
        ],
    }


def list_admin_releases(
    *,
    query: str = "",
    status_filter: str = "all",
    sort: str = "new",
    page: int = 1,
    per_page: int = 20,
) -> dict[str, Any]:
    query = query.strip()
    where = ["1 = 1"]
    params: list[Any] = []
    valid_statuses = {"all", "draft", "moderation", "accepted", "published", "changes_required", "rejected"}
    if status_filter not in valid_statuses:
        status_filter = "all"
    if status_filter != "all":
        where.append("r.status = ?")
        params.append(status_filter)
    if query:
        wildcard = f"%{query}%"
        where.append(
            """(
                r.title LIKE ? COLLATE NOCASE OR r.artist_name LIKE ? COLLATE NOCASE
                OR COALESCE(r.upc, '') LIKE ? COLLATE NOCASE
                OR u.email LIKE ? COLLATE NOCASE OR u.username LIKE ? COLLATE NOCASE
                OR CAST(u.id AS TEXT) = ? OR CAST(r.id AS TEXT) = ?
            )"""
        )
        params.extend([wildcard, wildcard, wildcard, wildcard, wildcard, query, query])
    order = {
        "old": "datetime(r.created_at) ASC, r.id ASC",
        "title": "r.title COLLATE NOCASE ASC",
        "status": "r.status ASC, datetime(r.updated_at) DESC",
    }.get(sort, "datetime(r.updated_at) DESC, r.id DESC")
    with _connect() as connection:
        total = int(
            connection.execute(
                f"""
                SELECT COUNT(*)
                FROM releases AS r
                JOIN users AS u ON u.id = r.user_id
                WHERE {' AND '.join(where)}
                """,
                params,
            ).fetchone()[0]
        )
        pagination = _page_meta(total, page, per_page)
        rows = connection.execute(
            f"""
            SELECT r.*, u.name AS user_name, u.email AS user_email, u.username,
                   COUNT(DISTINCT rt.id) AS track_count,
                   COALESCE((SELECT SUM(l.count) FROM listens l WHERE l.release_id = r.id), 0) AS listens
            FROM releases AS r
            JOIN users AS u ON u.id = r.user_id
            LEFT JOIN release_tracks AS rt ON rt.release_id = r.id
            WHERE {' AND '.join(where)}
            GROUP BY r.id
            ORDER BY {order}
            LIMIT ? OFFSET ?
            """,
            [*params, per_page, (int(pagination["page"]) - 1) * per_page],
        ).fetchall()
        count_rows = connection.execute(
            "SELECT status, COUNT(*) AS total FROM releases GROUP BY status"
        ).fetchall()
    counts = {str(row["status"]): int(row["total"]) for row in count_rows}
    counts["all"] = sum(counts.values())
    return {
        "items": [_admin_release_row(row) for row in rows],
        "query": query,
        "status_filter": status_filter,
        "sort": sort,
        "counts": counts,
        "pagination": pagination,
    }


def get_admin_release(release_id: int) -> dict[str, Any] | None:
    with _connect() as connection:
        row = connection.execute(
            """
            SELECT r.*, u.name AS user_name, u.email AS user_email, u.username,
                   u.balance AS user_balance, u.is_blocked,
                   COUNT(DISTINCT rt.id) AS track_count,
                   COALESCE((SELECT SUM(l.count) FROM listens l WHERE l.release_id = r.id), 0) AS listens
            FROM releases AS r
            JOIN users AS u ON u.id = r.user_id
            LEFT JOIN release_tracks AS rt ON rt.release_id = r.id
            WHERE r.id = ?
            GROUP BY r.id
            """,
            (release_id,),
        ).fetchone()
        if not row:
            return None
        tracks = connection.execute(
            """
            SELECT rt.*,
                   COALESCE((SELECT SUM(tl.count) FROM track_listens tl WHERE tl.track_id = rt.id), 0) AS listens
            FROM release_tracks AS rt
            WHERE rt.release_id = ?
            ORDER BY rt.position, rt.id
            """,
            (release_id,),
        ).fetchall()
        events = connection.execute(
            """
            SELECT me.*, u.name AS admin_name
            FROM moderation_events AS me
            LEFT JOIN users AS u ON u.id = me.admin_user_id
            WHERE me.release_id = ?
            ORDER BY datetime(me.created_at) DESC, me.id DESC
            """,
            (release_id,),
        ).fetchall()
    release = _admin_release_row(row)
    release["metadata_language_label"] = _language_label(str(release.get("metadata_language") or "ru"))
    release["balance_label"] = _format_money(release.get("user_balance"))
    track_items = []
    for track in tracks:
        item = dict(track)
        item["language_label"] = _language_label(str(item.get("language") or "ru"))
        item["listens_label"] = f"{int(item.get('listens') or 0):,}".replace(",", " ")
        track_items.append(item)
    release["tracks"] = track_items
    release["events"] = [
        {**dict(event), "created_at_label": _date_time_label(event["created_at"])}
        for event in events
    ]
    return release


def get_admin_track(track_id: int) -> dict[str, Any] | None:
    """Return a single track with ownership and release metadata for admin actions."""
    with _connect() as connection:
        row = connection.execute(
            """
            SELECT rt.*, r.title AS release_title, r.user_id AS release_user_id,
                   u.email AS user_email, u.username,
                   COALESCE((SELECT SUM(tl.count) FROM track_listens tl WHERE tl.track_id = rt.id), 0) AS listens
            FROM release_tracks AS rt
            JOIN releases AS r ON r.id = rt.release_id
            JOIN users AS u ON u.id = r.user_id
            WHERE rt.id = ?
            """,
            (track_id,),
        ).fetchone()
    if not row:
        return None
    item = dict(row)
    item["language_label"] = _language_label(str(item.get("language") or "ru"))
    item["listens_label"] = f"{int(item.get('listens') or 0):,}".replace(",", " ")
    return item


def moderate_release(
    admin_user_id: int,
    release_id: int,
    *,
    decision: str,
    comment: str,
) -> tuple[bool, str]:
    mapping = {
        "approve": ("accepted", "Принят", "success"),
        "changes": ("changes_required", "Требуются изменения", "warning"),
        "reject": ("rejected", "Отклонён", "danger"),
        "message": (None, "Сообщение отправлено", "info"),
    }
    if decision not in mapping:
        return False, "Выберите корректное решение."
    if decision in {"changes", "reject", "message"} and len(comment.strip()) < 3:
        return False, "Добавьте понятный комментарий для пользователя."
    now = _now()
    with _connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT id, user_id, title, status FROM releases WHERE id = ?",
            (release_id,),
        ).fetchone()
        if not row:
            connection.rollback()
            return False, "Релиз не найден."
        from_status = str(row["status"])
        new_status, label, notification_type = mapping[decision]
        if decision == "message":
            to_status = from_status
            connection.execute(
                "UPDATE releases SET moderator_comment = ?, updated_at = ? WHERE id = ?",
                (comment.strip(), now, release_id),
            )
        else:
            to_status = str(new_status)
            rejection_reason = comment.strip() if decision in {"changes", "reject"} else None
            connection.execute(
                """
                UPDATE releases
                SET status = ?, moderation_decision = ?, moderator_comment = ?,
                    rejection_reason = ?, moderated_at = ?, moderator_id = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    new_status,
                    decision,
                    comment.strip() or None,
                    rejection_reason,
                    now,
                    admin_user_id,
                    now,
                    release_id,
                ),
            )
        connection.execute(
            """
            INSERT INTO moderation_events (
                release_id, admin_user_id, action, comment, from_status, to_status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (release_id, admin_user_id, decision, comment.strip() or None, from_status, to_status, now),
        )
        body = comment.strip() or (
            "Релиз прошёл модерацию и принят сервисом."
            if decision == "approve"
            else "Статус релиза обновлён."
        )
        create_notification(
            connection,
            int(row["user_id"]),
            title=f"{label}: {row['title']}",
            body=body,
            notification_type=notification_type,
            action_url=f"/account/releases/{release_id}/review",
        )
        _audit(
            connection,
            admin_user_id,
            f"release_{decision}",
            "release",
            release_id,
            {"from": from_status, "to": to_status, "comment": comment.strip()},
        )
        connection.commit()
    return True, label + "."


def set_release_listens(admin_user_id: int, release_id: int, target: int) -> tuple[bool, str]:
    if target < 0:
        return False, "Количество прослушиваний не может быть отрицательным."
    now = _now()
    today = date.today().isoformat()
    with _connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT user_id, title FROM releases WHERE id = ?", (release_id,)
        ).fetchone()
        if not row:
            connection.rollback()
            return False, "Релиз не найден."
        current = int(
            connection.execute(
                "SELECT COALESCE(SUM(count), 0) FROM listens WHERE release_id = ?",
                (release_id,),
            ).fetchone()[0]
        )
        delta = target - current
        if delta:
            connection.execute(
                """
                INSERT INTO listens (user_id, release_id, listen_date, count, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (int(row["user_id"]), release_id, today, delta, now),
            )
        _audit(
            connection,
            admin_user_id,
            "set_release_listens",
            "release",
            release_id,
            {"previous": current, "target": target, "delta": delta},
        )
        connection.commit()
    return True, f"Прослушивания релиза обновлены: {target:,}.".replace(",", " ")


def set_track_listens(admin_user_id: int, track_id: int, target: int) -> tuple[bool, str]:
    if target < 0:
        return False, "Количество прослушиваний не может быть отрицательным."
    now = _now()
    today = date.today().isoformat()
    with _connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT id, release_id, user_id, title FROM release_tracks WHERE id = ?",
            (track_id,),
        ).fetchone()
        if not row:
            connection.rollback()
            return False, "Трек не найден."
        current = int(
            connection.execute(
                "SELECT COALESCE(SUM(count), 0) FROM track_listens WHERE track_id = ?",
                (track_id,),
            ).fetchone()[0]
        )
        delta = target - current
        if delta:
            connection.execute(
                """
                INSERT INTO track_listens (
                    user_id, release_id, track_id, listen_date, count, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(row["user_id"]),
                    int(row["release_id"]),
                    track_id,
                    today,
                    delta,
                    now,
                    now,
                ),
            )
        _audit(
            connection,
            admin_user_id,
            "set_track_listens",
            "track",
            track_id,
            {"previous": current, "target": target, "delta": delta},
        )
        connection.commit()
    return True, f"Прослушивания трека обновлены: {target:,}.".replace(",", " ")


def list_admin_users(
    *,
    query: str = "",
    status_filter: str = "all",
    page: int = 1,
    per_page: int = 20,
) -> dict[str, Any]:
    query = query.strip()
    where = ["1 = 1"]
    params: list[Any] = []
    if status_filter == "active":
        where.append("u.is_blocked = 0")
    elif status_filter == "blocked":
        where.append("u.is_blocked = 1")
    else:
        status_filter = "all"
    if query:
        wildcard = f"%{query}%"
        where.append(
            """(
                u.name LIKE ? COLLATE NOCASE OR u.email LIKE ? COLLATE NOCASE
                OR u.username LIKE ? COLLATE NOCASE OR CAST(u.id AS TEXT) = ?
            )"""
        )
        params.extend([wildcard, wildcard, wildcard, query])
    with _connect() as connection:
        total = int(
            connection.execute(
                f"SELECT COUNT(*) FROM users u WHERE {' AND '.join(where)}",
                params,
            ).fetchone()[0]
        )
        pagination = _page_meta(total, page, per_page)
        rows = connection.execute(
            f"""
            SELECT u.id, u.name, u.email, u.username, u.balance, u.pending_balance,
                   u.is_blocked, u.is_admin, u.created_at, u.last_login_at,
                   COUNT(DISTINCT r.id) AS release_count,
                   COALESCE((SELECT SUM(l.count) FROM listens l WHERE l.user_id = u.id), 0) AS listens
            FROM users AS u
            LEFT JOIN releases AS r ON r.user_id = u.id
            WHERE {' AND '.join(where)}
            GROUP BY u.id
            ORDER BY datetime(u.created_at) DESC, u.id DESC
            LIMIT ? OFFSET ?
            """,
            [*params, per_page, (int(pagination["page"]) - 1) * per_page],
        ).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        item["created_at_label"] = _date_label(item.get("created_at"))
        item["last_login_label"] = _date_time_label(item.get("last_login_at"))
        item["balance_label"] = _format_money(item.get("balance"))
        item["pending_label"] = _format_money(item.get("pending_balance"))
        item["listens_label"] = f"{int(item.get('listens') or 0):,}".replace(",", " ")
        items.append(item)
    return {
        "items": items,
        "query": query,
        "status_filter": status_filter,
        "pagination": pagination,
    }


def get_admin_user(user_id: int) -> dict[str, Any] | None:
    with _connect() as connection:
        row = connection.execute(
            """
            SELECT u.*,
                   COUNT(DISTINCT r.id) AS release_count,
                   COALESCE((SELECT SUM(l.count) FROM listens l WHERE l.user_id = u.id), 0) AS listens
            FROM users AS u
            LEFT JOIN releases AS r ON r.user_id = u.id
            WHERE u.id = ?
            GROUP BY u.id
            """,
            (user_id,),
        ).fetchone()
        if not row:
            return None
        releases = connection.execute(
            """
            SELECT r.*, COUNT(DISTINCT rt.id) AS track_count,
                   COALESCE((SELECT SUM(l.count) FROM listens l WHERE l.release_id = r.id), 0) AS listens
            FROM releases AS r
            LEFT JOIN release_tracks AS rt ON rt.release_id = r.id
            WHERE r.user_id = ?
            GROUP BY r.id
            ORDER BY datetime(r.updated_at) DESC, r.id DESC
            """,
            (user_id,),
        ).fetchall()
        track_rows = connection.execute(
            """
            SELECT rt.*,
                   COALESCE((SELECT SUM(tl.count) FROM track_listens tl WHERE tl.track_id = rt.id), 0) AS listens
            FROM release_tracks AS rt
            WHERE rt.user_id = ?
            ORDER BY rt.release_id, rt.position, rt.id
            """,
            (user_id,),
        ).fetchall()
        transactions = connection.execute(
            """
            SELECT bt.*, r.title AS release_title
            FROM balance_transactions bt
            LEFT JOIN releases r ON r.id = bt.release_id
            WHERE bt.user_id = ?
            ORDER BY datetime(bt.created_at) DESC, bt.id DESC
            LIMIT 20
            """,
            (user_id,),
        ).fetchall()
    user = dict(row)
    user["created_at_label"] = _date_time_label(user.get("created_at"))
    user["last_login_label"] = _date_time_label(user.get("last_login_at"))
    user["balance_label"] = _format_money(user.get("balance"))
    user["pending_label"] = _format_money(user.get("pending_balance"))
    user["listens_label"] = f"{int(user.get('listens') or 0):,}".replace(",", " ")
    tracks_by_release: dict[int, list[dict[str, Any]]] = {}
    for row_item in track_rows:
        track = dict(row_item)
        track["language_label"] = _language_label(str(track.get("language") or "ru"))
        track["listens_label"] = f"{int(track.get('listens') or 0):,}".replace(",", " ")
        tracks_by_release.setdefault(int(track["release_id"]), []).append(track)
    release_items = []
    for release_row in releases:
        release = _admin_release_row(release_row)
        release["tracks"] = tracks_by_release.get(int(release["id"]), [])
        release_items.append(release)
    user["releases"] = release_items
    user["transactions"] = [
        {
            **dict(item),
            "created_at_label": _date_time_label(item["created_at"]),
            "status_meta": _operation_status_meta(str(item["status"])),
            "amount_label": _format_money(item["amount"], show_plus=True),
        }
        for item in transactions
    ]
    return user


def adjust_user_balance(
    admin_user_id: int,
    user_id: int,
    *,
    amount: int,
    reason: str,
) -> tuple[bool, str]:
    if amount == 0:
        return False, "Укажите сумму, отличную от нуля."
    if len(reason.strip()) < 3:
        return False, "Укажите причину изменения баланса."
    now = _now()
    with _connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT balance FROM users WHERE id = ?", (user_id,)).fetchone()
        if not row:
            connection.rollback()
            return False, "Пользователь не найден."
        current = int(row["balance"] or 0)
        target = current + amount
        if target < 0:
            connection.rollback()
            return False, "Баланс пользователя не может стать отрицательным."
        connection.execute(
            "UPDATE users SET balance = ?, updated_at = ? WHERE id = ?",
            (target, now, user_id),
        )
        connection.execute(
            """
            INSERT INTO balance_transactions (
                user_id, release_id, transaction_type, status, amount, title, created_at
            ) VALUES (?, NULL, 'admin_adjustment', 'completed', ?, ?, ?)
            """,
            (user_id, amount, reason.strip(), now),
        )
        create_notification(
            connection,
            user_id,
            title="Баланс обновлён",
            body=f"{reason.strip()}. Изменение: {_format_money(amount, show_plus=True)}.",
            notification_type="success" if amount > 0 else "warning",
            action_url="/account/balance",
        )
        _audit(
            connection,
            admin_user_id,
            "adjust_balance",
            "user",
            user_id,
            {"previous": current, "amount": amount, "target": target, "reason": reason.strip()},
        )
        connection.commit()
    return True, f"Баланс обновлён. Новое значение: {_format_money(target)}."


def set_user_blocked(
    admin_user_id: int,
    user_id: int,
    *,
    blocked: bool,
    reason: str = "",
) -> tuple[bool, str]:
    if blocked and len(reason.strip()) < 3:
        return False, "Укажите причину блокировки."
    if admin_user_id == user_id and blocked:
        return False, "Нельзя заблокировать собственный аккаунт администратора."
    now = _now()
    with _connect() as connection:
        cursor = connection.execute(
            """
            UPDATE users
            SET is_blocked = ?, blocked_reason = ?, updated_at = ?
            WHERE id = ?
            """,
            (1 if blocked else 0, reason.strip() if blocked else None, now, user_id),
        )
        if cursor.rowcount != 1:
            connection.rollback()
            return False, "Пользователь не найден."
        create_notification(
            connection,
            user_id,
            title="Аккаунт заблокирован" if blocked else "Аккаунт разблокирован",
            body=reason.strip() if blocked else "Доступ к платформе восстановлен.",
            notification_type="danger" if blocked else "success",
        )
        _audit(
            connection,
            admin_user_id,
            "block_user" if blocked else "unblock_user",
            "user",
            user_id,
            {"reason": reason.strip()},
        )
        connection.commit()
    return True, "Пользователь заблокирован." if blocked else "Пользователь разблокирован."


def list_service_news_admin() -> list[dict[str, Any]]:
    with _connect() as connection:
        rows = connection.execute(
            """
            SELECT * FROM service_news
            ORDER BY is_active DESC, sort_order DESC, datetime(published_at) DESC, id DESC
            """
        ).fetchall()
    return [
        {
            **dict(row),
            "published_at_label": _date_time_label(row["published_at"]),
            "updated_at_label": _date_time_label(row["updated_at"]),
        }
        for row in rows
    ]


def save_service_news(
    admin_user_id: int,
    *,
    news_id: int | None,
    title: str,
    body: str,
    published_at: str,
    sort_order: int,
    is_active: bool,
) -> tuple[bool, str]:
    title = " ".join(title.strip().split())[:140]
    body = "\n".join(line.strip() for line in body.strip().splitlines() if line.strip())[:3000]
    if len(title) < 3:
        return False, "Введите заголовок новости."
    if len(body) < 5:
        return False, "Введите текст новости."
    try:
        parsed = datetime.fromisoformat(published_at)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        published_value = parsed.isoformat()
    except ValueError:
        published_value = _now()
    now = _now()
    with _connect() as connection:
        if news_id:
            cursor = connection.execute(
                """
                UPDATE service_news
                SET title = ?, body = ?, published_at = ?, sort_order = ?,
                    is_active = ?, updated_at = ?
                WHERE id = ?
                """,
                (title, body, published_value, sort_order, 1 if is_active else 0, now, news_id),
            )
            if cursor.rowcount != 1:
                return False, "Новость не найдена."
            entity_id = news_id
            action = "update_news"
        else:
            cursor = connection.execute(
                """
                INSERT INTO service_news (
                    title, body, published_at, is_active, sort_order, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (title, body, published_value, 1 if is_active else 0, sort_order, now),
            )
            entity_id = int(cursor.lastrowid)
            action = "create_news"
        _audit(connection, admin_user_id, action, "service_news", entity_id, {"title": title})
        connection.commit()
    return True, "Новость сохранена."


def delete_service_news(admin_user_id: int, news_id: int) -> tuple[bool, str]:
    with _connect() as connection:
        row = connection.execute("SELECT title FROM service_news WHERE id = ?", (news_id,)).fetchone()
        if not row:
            return False, "Новость не найдена."
        connection.execute("DELETE FROM service_news WHERE id = ?", (news_id,))
        _audit(connection, admin_user_id, "delete_news", "service_news", news_id, {"title": row["title"]})
        connection.commit()
    return True, "Новость удалена."


def list_payout_requests(status_filter: str = "all") -> dict[str, Any]:
    valid = {"all", "pending", "processing", "paid", "rejected", "cancelled"}
    if status_filter not in valid:
        status_filter = "all"
    where = ["1 = 1"]
    params: list[Any] = []
    if status_filter != "all":
        where.append("pr.status = ?")
        params.append(status_filter)
    with _connect() as connection:
        rows = connection.execute(
            f"""
            SELECT pr.*, u.name AS user_name, u.email AS user_email, u.username,
                   u.balance AS current_balance, u.pending_balance
            FROM payout_requests pr
            JOIN users u ON u.id = pr.user_id
            WHERE {' AND '.join(where)}
            ORDER BY CASE pr.status WHEN 'pending' THEN 0 WHEN 'processing' THEN 1 ELSE 2 END,
                     datetime(pr.created_at) DESC
            """,
            params,
        ).fetchall()
        count_rows = connection.execute(
            "SELECT status, COUNT(*) AS total FROM payout_requests GROUP BY status"
        ).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        item["created_at_label"] = _date_time_label(item.get("created_at"))
        item["status_meta"] = _payout_status_meta(str(item.get("status") or "pending"))
        item["amount_label"] = _format_money(item.get("amount"))
        item["balance_label"] = _format_money(item.get("current_balance"))
        items.append(item)
    counts = {str(row["status"]): int(row["total"]) for row in count_rows}
    counts["all"] = sum(counts.values())
    return {"items": items, "counts": counts, "status_filter": status_filter}


def process_payout_request(
    admin_user_id: int,
    payout_id: int,
    *,
    status_value: str,
    admin_comment: str,
) -> tuple[bool, str]:
    if status_value not in {"processing", "paid", "rejected"}:
        return False, "Выберите корректный статус."
    now = _now()
    with _connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT * FROM payout_requests WHERE id = ?", (payout_id,)).fetchone()
        if not row:
            connection.rollback()
            return False, "Заявка не найдена."
        current_status = str(row["status"])
        if current_status in {"paid", "rejected", "cancelled"}:
            connection.rollback()
            return False, "Эта заявка уже завершена."
        user_id = int(row["user_id"])
        amount = int(row["amount"])
        transaction_id = row["transaction_id"]
        if status_value == "paid":
            connection.execute(
                """
                UPDATE users
                SET pending_balance = MAX(0, pending_balance - ?),
                    last_payout_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (amount, now, now, user_id),
            )
            if transaction_id:
                connection.execute(
                    "UPDATE balance_transactions SET status = 'completed' WHERE id = ?",
                    (transaction_id,),
                )
        elif status_value == "rejected":
            connection.execute(
                """
                UPDATE users
                SET pending_balance = MAX(0, pending_balance - ?),
                    balance = balance + ?, updated_at = ?
                WHERE id = ?
                """,
                (amount, amount, now, user_id),
            )
            if transaction_id:
                connection.execute(
                    "UPDATE balance_transactions SET status = 'cancelled' WHERE id = ?",
                    (transaction_id,),
                )
        connection.execute(
            """
            UPDATE payout_requests
            SET status = ?, admin_comment = ?, updated_at = ?,
                processed_at = CASE WHEN ? IN ('paid', 'rejected') THEN ? ELSE processed_at END,
                processed_by = ?
            WHERE id = ?
            """,
            (status_value, admin_comment.strip() or None, now, status_value, now, admin_user_id, payout_id),
        )
        label = _payout_status_meta(status_value)["label"]
        create_notification(
            connection,
            user_id,
            title=f"Выплата: {label}",
            body=admin_comment.strip() or f"Статус заявки на {_format_money(amount)} обновлён.",
            notification_type="success" if status_value == "paid" else "warning",
            action_url="/account/balance",
        )
        _audit(
            connection,
            admin_user_id,
            "process_payout",
            "payout",
            payout_id,
            {"from": current_status, "to": status_value, "amount": amount},
        )
        connection.commit()
    return True, "Статус выплаты обновлён."


def get_pitching_page_data_v2(user_id: int) -> dict[str, Any]:
    minimum_date = date.today() + timedelta(days=PITCHING_MIN_DAYS)
    with _connect() as connection:
        release_rows = connection.execute(
            """
            SELECT r.id, r.title, r.artist_name, r.release_date, r.cover_path, r.status,
                   COUNT(rt.id) AS track_count
            FROM releases r
            LEFT JOIN release_tracks rt ON rt.release_id = r.id
            WHERE r.user_id = ?
            GROUP BY r.id
            ORDER BY datetime(r.updated_at) DESC, r.id DESC
            """,
            (user_id,),
        ).fetchall()
        platform_rows = connection.execute(
            """
            SELECT id, name, slug, logo_path, description, instructions
            FROM pitching_platforms
            WHERE is_active = 1
            ORDER BY sort_order, name
            """
        ).fetchall()
        request_rows = connection.execute(
            """
            SELECT pr.*, r.title AS release_title, r.artist_name, r.release_date,
                   pp.name AS platform_name, pp.logo_path, pp.slug
            FROM pitching_requests pr
            JOIN releases r ON r.id = pr.release_id
            JOIN pitching_platforms pp ON pp.id = pr.platform_id
            WHERE pr.user_id = ?
            ORDER BY datetime(pr.updated_at) DESC, pr.id DESC
            """,
            (user_id,),
        ).fetchall()
    releases = []
    eligible = []
    for row in release_rows:
        item = dict(row)
        item["cover_url"] = _cover_url(int(item["id"]), item.get("cover_path"))
        item["release_date_label"] = _date_label(item.get("release_date")) if item.get("release_date") else "Не назначена"
        reason = None
        if item["status"] not in {"accepted", "published"}:
            reason = "Сначала релиз должен быть принят модерацией."
        elif not item.get("release_date"):
            reason = "Укажите планируемую дату выхода."
        else:
            try:
                release_date = date.fromisoformat(str(item["release_date"])[:10])
            except ValueError:
                reason = "Дата выхода указана некорректно."
            else:
                if release_date < minimum_date:
                    reason = f"До выхода должно оставаться не менее {PITCHING_MIN_DAYS} дней."
        item["eligible"] = reason is None
        item["eligibility_reason"] = reason
        releases.append(item)
        if reason is None:
            eligible.append(item)
    requests = []
    for row in request_rows:
        item = dict(row)
        item["public_id"] = f"P-{int(item['id']):06d}"
        item["status_meta"] = {
            "submitted": {"label": "Отправлена", "class": "warning"},
            "in_review": {"label": "Рассматривается", "class": "warning"},
            "approved": {"label": "Одобрена", "class": "success"},
            "rejected": {"label": "Не принята", "class": "danger"},
            "cancelled": {"label": "Отменена", "class": "draft"},
        }.get(str(item.get("status")), {"label": str(item.get("status")), "class": "draft"})
        item["updated_at_label"] = _date_time_label(item.get("updated_at"))
        item["release_date_label"] = _date_label(item.get("release_date"))
        requests.append(item)
    return {
        "releases": releases,
        "eligible_releases": eligible,
        "platforms": [dict(row) for row in platform_rows],
        "requests": requests,
        "minimum_days": PITCHING_MIN_DAYS,
        "minimum_date_label": minimum_date.strftime("%d.%m.%Y"),
    }


def list_admin_pitching_v2(status_filter: str = "all") -> dict[str, Any]:
    valid = {"all", "submitted", "in_review", "approved", "rejected", "cancelled"}
    if status_filter not in valid:
        status_filter = "all"
    where = ["1 = 1"]
    params: list[Any] = []
    if status_filter != "all":
        where.append("pr.status = ?")
        params.append(status_filter)
    with _connect() as connection:
        rows = connection.execute(
            f"""
            SELECT pr.*, r.title AS release_title, r.artist_name, r.release_date,
                   r.cover_path, pp.name AS platform_name, pp.logo_path, pp.slug,
                   u.name AS user_name, u.email AS user_email, u.username
            FROM pitching_requests pr
            JOIN releases r ON r.id = pr.release_id
            JOIN pitching_platforms pp ON pp.id = pr.platform_id
            JOIN users u ON u.id = pr.user_id
            WHERE {' AND '.join(where)}
            ORDER BY CASE pr.status WHEN 'submitted' THEN 0 WHEN 'in_review' THEN 1 ELSE 2 END,
                     datetime(pr.updated_at) DESC, pr.id DESC
            """,
            params,
        ).fetchall()
        count_rows = connection.execute(
            "SELECT status, COUNT(*) AS total FROM pitching_requests GROUP BY status"
        ).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        item["cover_url"] = _cover_url(int(item["release_id"]), item.get("cover_path"))
        item["status_meta"] = {
            "submitted": {"label": "Новая", "class": "warning"},
            "in_review": {"label": "В работе", "class": "warning"},
            "approved": {"label": "Одобрена", "class": "success"},
            "rejected": {"label": "Отклонена", "class": "danger"},
            "cancelled": {"label": "Отменена", "class": "draft"},
        }.get(str(item["status"]), {"label": str(item["status"]), "class": "draft"})
        item["updated_at_label"] = _date_time_label(item.get("updated_at"))
        item["release_date_label"] = _date_label(item.get("release_date"))
        items.append(item)
    counts = {str(row["status"]): int(row["total"]) for row in count_rows}
    counts["all"] = sum(counts.values())
    return {"items": items, "counts": counts, "status_filter": status_filter}


def get_admin_pitching_request(request_id: int) -> dict[str, Any] | None:
    data = list_admin_pitching_v2("all")
    return next((item for item in data["items"] if int(item["id"]) == request_id), None)


def update_admin_pitching_request(
    admin_user_id: int,
    request_id: int,
    *,
    status_value: str,
    admin_comment: str,
) -> tuple[bool, str]:
    if status_value not in {"submitted", "in_review", "approved", "rejected", "cancelled"}:
        return False, "Выберите корректный статус."
    if status_value in {"approved", "rejected"} and len(admin_comment.strip()) < 3:
        return False, "Добавьте комментарий к решению."
    now = _now()
    with _connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            """
            SELECT pr.user_id, pr.status, r.title, pp.name AS platform_name
            FROM pitching_requests pr
            JOIN releases r ON r.id = pr.release_id
            JOIN pitching_platforms pp ON pp.id = pr.platform_id
            WHERE pr.id = ?
            """,
            (request_id,),
        ).fetchone()
        if not row:
            connection.rollback()
            return False, "Заявка не найдена."
        connection.execute(
            """
            UPDATE pitching_requests
            SET status = ?, admin_comment = ?, reviewed_at = ?, reviewed_by = ?, updated_at = ?
            WHERE id = ?
            """,
            (status_value, admin_comment.strip() or None, now, admin_user_id, now, request_id),
        )
        labels = {
            "submitted": "Заявка возвращена в очередь",
            "in_review": "Заявка взята в работу",
            "approved": "Питчинг одобрен",
            "rejected": "Питчинг не одобрен",
            "cancelled": "Заявка отменена",
        }
        create_notification(
            connection,
            int(row["user_id"]),
            title=f"{labels[status_value]}: {row['platform_name']}",
            body=admin_comment.strip() or f"Релиз «{row['title']}»: статус заявки изменён.",
            notification_type="success" if status_value == "approved" else "warning",
            action_url="/account/pitching",
        )
        _audit(
            connection,
            admin_user_id,
            "update_pitching",
            "pitching_request",
            request_id,
            {"from": row["status"], "to": status_value},
        )
        connection.commit()
    return True, "Заявка на питчинг обновлена."


def list_admin_support_summary(status_filter: str = "all") -> dict[str, Any]:
    valid = {"all", "open", "in_progress", "answered", "closed"}
    if status_filter not in valid:
        status_filter = "all"
    where = ["1 = 1"]
    params: list[Any] = []
    if status_filter != "all":
        where.append("t.status = ?")
        params.append(status_filter)
    with _connect() as connection:
        rows = connection.execute(
            f"""
            SELECT t.*, u.name AS user_name, u.email AS user_email, u.username,
                   COUNT(m.id) AS message_count,
                   SUM(CASE WHEN m.sender_type = 'user' AND m.is_read = 0 THEN 1 ELSE 0 END) AS unread_count
            FROM support_tickets t
            JOIN users u ON u.id = t.user_id
            LEFT JOIN support_messages m ON m.ticket_id = t.id
            WHERE {' AND '.join(where)}
            GROUP BY t.id
            ORDER BY CASE t.status WHEN 'open' THEN 0 WHEN 'in_progress' THEN 1 ELSE 2 END,
                     datetime(t.last_message_at) DESC, t.id DESC
            """,
            params,
        ).fetchall()
        count_rows = connection.execute(
            "SELECT status, COUNT(*) AS total FROM support_tickets GROUP BY status"
        ).fetchall()
    status_meta = {
        "open": {"label": "Новая", "class": "warning"},
        "in_progress": {"label": "В работе", "class": "warning"},
        "answered": {"label": "Ответ отправлен", "class": "success"},
        "closed": {"label": "Закрыта", "class": "draft"},
    }
    items = []
    for row in rows:
        item = dict(row)
        item["status_meta"] = status_meta.get(str(item["status"]), {"label": item["status"], "class": "draft"})
        item["updated_at_label"] = _date_time_label(item.get("updated_at"))
        items.append(item)
    counts = {str(row["status"]): int(row["total"]) for row in count_rows}
    counts["all"] = sum(counts.values())
    return {"items": items, "counts": counts, "status_filter": status_filter}


def get_admin_imports() -> list[dict[str, Any]]:
    with _connect() as connection:
        rows = connection.execute(
            """
            SELECT ai.*, u.name AS admin_name
            FROM analytics_imports ai
            LEFT JOIN users u ON u.id = ai.uploaded_by
            ORDER BY datetime(ai.created_at) DESC, ai.id DESC
            LIMIT 30
            """
        ).fetchall()
    return [
        {
            **dict(row),
            "created_at_label": _date_time_label(row["created_at"]),
        }
        for row in rows
    ]
