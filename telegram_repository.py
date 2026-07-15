from __future__ import annotations

import html
import json
from datetime import date, datetime, timedelta, timezone
from typing import Any

from auth_utils import verify_password
from config import ADMIN_TELEGRAM_IDS, SITE_URL, TELEGRAM_AUTH_TTL_MINUTES
from database import _connect


def _h(value: Any) -> str:
    return html.escape(str(value or ""))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None = None) -> str:
    return (value or _now()).isoformat()


def find_user_for_telegram_login(login: str) -> dict[str, Any] | None:
    value = login.strip().lower().lstrip("@")
    if not value:
        return None
    with _connect() as connection:
        row = connection.execute(
            """
            SELECT id, name, email, username, password_hash, balance, pending_balance,
                   is_admin, is_blocked, blocked_reason, telegram_connected, telegram_id
            FROM users
            WHERE lower(email) = ? OR lower(username) = ?
            LIMIT 1
            """,
            (value, value),
        ).fetchone()
    return dict(row) if row else None


def authenticate_telegram_user(login: str, password: str) -> dict[str, Any] | None:
    user = find_user_for_telegram_login(login)
    if not user or not verify_password(password, str(user["password_hash"])):
        return None
    return user


def get_user_by_telegram_id(telegram_id: int | str) -> dict[str, Any] | None:
    with _connect() as connection:
        row = connection.execute(
            """
            SELECT id, name, email, username, balance, pending_balance, last_payout_at,
                   telegram_id, telegram_chat_id, telegram_username, telegram_connected,
                   telegram_notifications_enabled, is_admin, is_blocked, blocked_reason
            FROM users WHERE telegram_id = ? LIMIT 1
            """,
            (str(telegram_id),),
        ).fetchone()
    return dict(row) if row else None


def link_telegram_account(
    user_id: int,
    *,
    telegram_id: int,
    chat_id: int,
    telegram_username: str | None,
) -> None:
    now = _iso()
    with _connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            UPDATE users
            SET telegram_id = NULL, telegram_chat_id = NULL, telegram_username = NULL,
                telegram_connected = 0, telegram_connected_at = NULL
            WHERE telegram_id = ? AND id != ?
            """,
            (str(telegram_id), user_id),
        )
        connection.execute(
            """
            UPDATE users
            SET telegram_id = ?, telegram_chat_id = ?, telegram_username = ?,
                telegram_connected = 1, telegram_connected_at = ?,
                telegram_notifications_enabled = 1, updated_at = ?
            WHERE id = ?
            """,
            (
                str(telegram_id),
                str(chat_id),
                (telegram_username or "")[:64] or None,
                now,
                now,
                user_id,
            ),
        )
        connection.commit()


def disconnect_telegram_account(*, user_id: int | None = None, telegram_id: int | str | None = None) -> bool:
    if user_id is None and telegram_id is None:
        return False
    now = _iso()
    where = "id = ?" if user_id is not None else "telegram_id = ?"
    value: int | str = user_id if user_id is not None else str(telegram_id)
    with _connect() as connection:
        cursor = connection.execute(
            f"""
            UPDATE users
            SET telegram_id = NULL, telegram_chat_id = NULL, telegram_username = NULL,
                telegram_connected = 0, telegram_connected_at = NULL, updated_at = ?
            WHERE {where}
            """,
            (now, value),
        )
        connection.execute(
            "DELETE FROM telegram_dialog_sessions WHERE telegram_id = ?",
            (str(telegram_id),),
        ) if telegram_id is not None else None
        connection.commit()
        return cursor.rowcount > 0


def set_dialog_session(
    telegram_id: int,
    chat_id: int,
    step: str,
    *,
    login_value: str | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    now = _now()
    expires = now + timedelta(minutes=TELEGRAM_AUTH_TTL_MINUTES)
    with _connect() as connection:
        connection.execute(
            """
            INSERT INTO telegram_dialog_sessions (
                telegram_id, chat_id, step, login_value, payload_json,
                created_at, updated_at, expires_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET
                chat_id = excluded.chat_id,
                step = excluded.step,
                login_value = excluded.login_value,
                payload_json = excluded.payload_json,
                updated_at = excluded.updated_at,
                expires_at = excluded.expires_at
            """,
            (
                str(telegram_id),
                str(chat_id),
                step,
                login_value,
                json.dumps(payload or {}, ensure_ascii=False),
                _iso(now),
                _iso(now),
                _iso(expires),
            ),
        )
        connection.commit()


def get_dialog_session(telegram_id: int) -> dict[str, Any] | None:
    now = _iso()
    with _connect() as connection:
        connection.execute(
            "DELETE FROM telegram_dialog_sessions WHERE expires_at < ?", (now,)
        )
        row = connection.execute(
            "SELECT * FROM telegram_dialog_sessions WHERE telegram_id = ?",
            (str(telegram_id),),
        ).fetchone()
        connection.commit()
    if not row:
        return None
    item = dict(row)
    try:
        item["payload"] = json.loads(str(item.get("payload_json") or "{}"))
    except json.JSONDecodeError:
        item["payload"] = {}
    return item


def clear_dialog_session(telegram_id: int) -> None:
    with _connect() as connection:
        connection.execute(
            "DELETE FROM telegram_dialog_sessions WHERE telegram_id = ?",
            (str(telegram_id),),
        )
        connection.commit()


def _insert_outbox(
    *,
    recipient_type: str,
    user_id: int | None,
    chat_id: int | str | None,
    event_type: str,
    text: str,
    action_url: str | None = None,
) -> None:
    now = _iso()
    with _connect() as connection:
        connection.execute(
            """
            INSERT INTO telegram_outbox (
                recipient_type, user_id, chat_id, event_type, message_text,
                action_url, status, attempts, available_at, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?)
            """,
            (
                recipient_type,
                user_id,
                str(chat_id) if chat_id is not None else None,
                event_type[:80],
                text[:3900],
                action_url,
                now,
                now,
            ),
        )
        connection.commit()


def queue_admin_message(event_type: str, text: str, action_path: str | None = None) -> None:
    action_url = f"{SITE_URL}{action_path}" if action_path else None
    for chat_id in ADMIN_TELEGRAM_IDS:
        _insert_outbox(
            recipient_type="admin",
            user_id=None,
            chat_id=chat_id,
            event_type=event_type,
            text=text,
            action_url=action_url,
        )


def queue_user_message(user_id: int, event_type: str, text: str, action_path: str | None = None) -> None:
    action_url = f"{SITE_URL}{action_path}" if action_path else None
    _insert_outbox(
        recipient_type="user",
        user_id=user_id,
        chat_id=None,
        event_type=event_type,
        text=text,
        action_url=action_url,
    )


def fetch_pending_outbox(limit: int = 25) -> list[dict[str, Any]]:
    now = _iso()
    with _connect() as connection:
        rows = connection.execute(
            """
            SELECT o.*, u.telegram_chat_id, u.telegram_id, u.telegram_connected,
                   u.telegram_notifications_enabled
            FROM telegram_outbox o
            LEFT JOIN users u ON u.id = o.user_id
            WHERE o.status = 'pending' AND o.available_at <= ?
            ORDER BY o.id ASC
            LIMIT ?
            """,
            (now, limit),
        ).fetchall()
    return [dict(row) for row in rows]


def mark_outbox_sent(outbox_id: int) -> None:
    with _connect() as connection:
        connection.execute(
            "UPDATE telegram_outbox SET status = 'sent', sent_at = ?, last_error = NULL WHERE id = ?",
            (_iso(), outbox_id),
        )
        connection.commit()


def mark_outbox_error(outbox_id: int, error: str, attempts: int) -> None:
    retry_at = _now() + timedelta(seconds=min(300, 15 * max(1, attempts)))
    status = "failed" if attempts >= 5 else "pending"
    with _connect() as connection:
        connection.execute(
            """
            UPDATE telegram_outbox
            SET status = ?, attempts = ?, available_at = ?, last_error = ?
            WHERE id = ?
            """,
            (status, attempts, _iso(retry_at), error[:500], outbox_id),
        )
        connection.commit()


def get_bot_dashboard(user_id: int) -> dict[str, Any]:
    with _connect() as connection:
        user = connection.execute(
            "SELECT id, name, username, balance, pending_balance FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
        status_rows = connection.execute(
            "SELECT status, COUNT(*) total FROM releases WHERE user_id = ? GROUP BY status",
            (user_id,),
        ).fetchall()
        unread = int(
            connection.execute(
                "SELECT COUNT(*) FROM user_notifications WHERE user_id = ? AND is_read = 0",
                (user_id,),
            ).fetchone()[0]
        )
    statuses = {str(row["status"]): int(row["total"]) for row in status_rows}
    return {
        "user": dict(user) if user else {},
        "release_count": sum(statuses.values()),
        "moderation_count": statuses.get("moderation", 0),
        "published_count": statuses.get("published", 0) + statuses.get("accepted", 0),
        "unread": unread,
    }


def get_bot_releases(user_id: int, limit: int = 8) -> list[dict[str, Any]]:
    with _connect() as connection:
        rows = connection.execute(
            """
            SELECT r.id, r.title, r.artist_name, r.status, r.release_date,
                   COUNT(rt.id) AS track_count
            FROM releases r
            LEFT JOIN release_tracks rt ON rt.release_id = r.id
            WHERE r.user_id = ?
            GROUP BY r.id
            ORDER BY datetime(r.updated_at) DESC, r.id DESC
            LIMIT ?
            """,
            (user_id, limit),
        ).fetchall()
    return [dict(row) for row in rows]


def get_bot_pitching(user_id: int, limit: int = 8) -> list[dict[str, Any]]:
    with _connect() as connection:
        rows = connection.execute(
            """
            SELECT pr.id, pr.status, pr.admin_comment, pr.created_at,
                   r.title AS release_title, pp.name AS platform_name
            FROM pitching_requests pr
            JOIN releases r ON r.id = pr.release_id
            JOIN pitching_platforms pp ON pp.id = pr.platform_id
            WHERE pr.user_id = ?
            ORDER BY datetime(pr.updated_at) DESC, pr.id DESC
            LIMIT ?
            """,
            (user_id, limit),
        ).fetchall()
    return [dict(row) for row in rows]


def get_bot_notifications(user_id: int, limit: int = 8) -> list[dict[str, Any]]:
    with _connect() as connection:
        rows = connection.execute(
            """
            SELECT id, title, body, notification_type, action_url, is_read, created_at
            FROM user_notifications
            WHERE user_id = ?
            ORDER BY datetime(created_at) DESC, id DESC
            LIMIT ?
            """,
            (user_id, limit),
        ).fetchall()
    return [dict(row) for row in rows]


def get_payout_context(user_id: int) -> dict[str, Any]:
    with _connect() as connection:
        user = connection.execute(
            "SELECT balance, pending_balance FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        active = connection.execute(
            """
            SELECT id, amount, status FROM payout_requests
            WHERE user_id = ? AND status IN ('pending', 'processing')
            ORDER BY id DESC LIMIT 1
            """,
            (user_id,),
        ).fetchone()
    return {
        "balance": int(user["balance"] or 0) if user else 0,
        "pending": int(user["pending_balance"] or 0) if user else 0,
        "active": dict(active) if active else None,
    }


def queue_new_registration(user_id: int) -> None:
    with _connect() as connection:
        row = connection.execute(
            "SELECT id, name, email, username FROM users WHERE id = ?", (user_id,)
        ).fetchone()
    if row:
        queue_admin_message(
            "registration",
            "👤 <b>Новый пользователь</b>\n"
            f"Имя: {_h(row['name'])}\n"
            f"Логин: @{_h(row['username'])}\n"
            f"Email: {_h(row['email'])}\n"
            f"ID: {row['id']}",
            f"/admin/users/{row['id']}",
        )


def queue_new_release(release_id: int) -> None:
    with _connect() as connection:
        row = connection.execute(
            """
            SELECT r.id, r.title, r.artist_name, r.release_type, r.release_date,
                   u.id user_id, u.name user_name, u.username
            FROM releases r JOIN users u ON u.id = r.user_id WHERE r.id = ?
            """,
            (release_id,),
        ).fetchone()
    if row:
        queue_admin_message(
            "release_submitted",
            "💿 <b>Новый релиз на модерации</b>\n"
            f"Релиз: {_h(row['title'])}\nАртист: {_h(row['artist_name'])}\n"
            f"Пользователь: @{_h(row['username'])} · ID {row['user_id']}\n"
            f"Дата выхода: {_h(row['release_date'] or 'не указана')}",
            f"/admin/releases/{release_id}",
        )


def queue_release_status(release_id: int, status_value: str, comment: str = "") -> None:
    labels = {
        "accepted": "✅ Релиз принят",
        "published": "🚀 Релиз опубликован",
        "changes_required": "🛠 Нужны изменения",
        "rejected": "❌ Релиз отклонён",
        "moderation": "🕘 Релиз на модерации",
    }
    with _connect() as connection:
        row = connection.execute(
            "SELECT id, user_id, title FROM releases WHERE id = ?", (release_id,)
        ).fetchone()
    if row:
        text = f"{labels.get(status_value, '💿 Статус релиза изменён')}\n<b>{_h(row['title'])}</b>"
        if comment.strip():
            text += f"\n\nКомментарий: {_h(comment.strip()[:900])}"
        queue_user_message(int(row["user_id"]), "release_status", text, f"/account/releases")


def queue_new_support_ticket(ticket_id: int, *, new_message: bool = False) -> None:
    with _connect() as connection:
        row = connection.execute(
            """
            SELECT st.id, st.subject, st.category, u.id user_id, u.username
            FROM support_tickets st JOIN users u ON u.id = st.user_id
            WHERE st.id = ?
            """,
            (ticket_id,),
        ).fetchone()
    if row:
        heading = "💬 Новое сообщение в поддержке" if new_message else "🆘 Новая заявка в поддержку"
        queue_admin_message(
            "support_message" if new_message else "support_ticket",
            f"{heading}\n<b>#{row['id']} · {_h(row['subject'])}</b>\n"
            f"Категория: {_h(row['category'])}\nПользователь: @{_h(row['username'])}",
            f"/admin/support/{ticket_id}",
        )


def _support_status_label(value: str) -> str:
    return {
        "open": "Открыта",
        "in_progress": "В работе",
        "answered": "Есть ответ",
        "closed": "Закрыта",
    }.get(value, value)


def _pitching_status_label(value: str) -> str:
    return {
        "submitted": "Отправлена",
        "in_review": "На рассмотрении",
        "approved": "Одобрена",
        "rejected": "Отклонена",
        "cancelled": "Отменена",
    }.get(value, value)


def _payout_status_label(value: str) -> str:
    return {
        "pending": "Ожидает",
        "processing": "В обработке",
        "paid": "Выплачено",
        "rejected": "Отклонено",
        "cancelled": "Отменено",
    }.get(value, value)


def queue_support_reply(ticket_id: int, message: str, status_value: str) -> None:
    with _connect() as connection:
        row = connection.execute(
            "SELECT id, user_id, subject FROM support_tickets WHERE id = ?", (ticket_id,)
        ).fetchone()
    if row:
        queue_user_message(
            int(row["user_id"]),
            "support_reply",
            "💬 <b>Ответ поддержки</b>\n"
            f"Заявка #{row['id']}: {_h(row['subject'])}\n\n{_h(message.strip()[:1400])}\n\n"
            f"Статус: {_support_status_label(status_value)}",
            f"/account/support?ticket={ticket_id}#conversation",
        )


def queue_new_pitching_request(request_id: int) -> None:
    with _connect() as connection:
        row = connection.execute(
            """
            SELECT pr.id, r.title release_title, pp.name platform_name,
                   u.id user_id, u.username
            FROM pitching_requests pr
            JOIN releases r ON r.id = pr.release_id
            JOIN pitching_platforms pp ON pp.id = pr.platform_id
            JOIN users u ON u.id = pr.user_id
            WHERE pr.id = ?
            """,
            (request_id,),
        ).fetchone()
    if row:
        queue_admin_message(
            "pitching_request",
            "🎯 <b>Новая заявка на питчинг</b>\n"
            f"Релиз: {_h(row['release_title'])}\nПлощадка: {_h(row['platform_name'])}\n"
            f"Пользователь: @{_h(row['username'])}",
            f"/admin/pitching/{request_id}",
        )


def queue_pitching_status(request_id: int, status_value: str, comment: str = "") -> None:
    with _connect() as connection:
        row = connection.execute(
            """
            SELECT pr.user_id, r.title release_title, pp.name platform_name
            FROM pitching_requests pr
            JOIN releases r ON r.id = pr.release_id
            JOIN pitching_platforms pp ON pp.id = pr.platform_id
            WHERE pr.id = ?
            """,
            (request_id,),
        ).fetchone()
    if row:
        text = (
            "🎯 <b>Статус питчинга обновлён</b>\n"
            f"Релиз: {_h(row['release_title'])}\nПлощадка: {_h(row['platform_name'])}\n"
            f"Статус: {_pitching_status_label(status_value)}"
        )
        if comment.strip():
            text += f"\n\nКомментарий: {_h(comment.strip()[:1000])}"
        queue_user_message(int(row["user_id"]), "pitching_status", text, "/account/pitching")


def queue_new_payout(user_id: int) -> None:
    with _connect() as connection:
        row = connection.execute(
            """
            SELECT pr.id, pr.amount, pr.method, pr.details, u.username, u.name
            FROM payout_requests pr JOIN users u ON u.id = pr.user_id
            WHERE pr.user_id = ? AND pr.status = 'pending'
            ORDER BY pr.id DESC LIMIT 1
            """,
            (user_id,),
        ).fetchone()
    if row:
        queue_admin_message(
            "payout_request",
            "💸 <b>Новая заявка на выплату</b>\n"
            f"Пользователь: @{_h(row['username'])}\nСумма: {int(row['amount']):,} ₽\n"
            f"Способ: {_h(row['method'])}\nКонтакт: {_h(str(row['details'] or '')[:500])}",
            "/admin/payouts",
        )


def queue_payout_status(payout_id: int, status_value: str, comment: str = "") -> None:
    with _connect() as connection:
        row = connection.execute(
            "SELECT user_id, amount FROM payout_requests WHERE id = ?", (payout_id,)
        ).fetchone()
    if row:
        text = (
            "💸 <b>Статус выплаты обновлён</b>\n"
            f"Сумма: {int(row['amount']):,} ₽\nСтатус: {_payout_status_label(status_value)}"
        )
        if comment.strip():
            text += f"\n\nКомментарий: {_h(comment.strip()[:900])}"
        queue_user_message(int(row["user_id"]), "payout_status", text, "/account/balance")


def queue_balance_change(user_id: int, amount: int, reason: str) -> None:
    sign = "+" if amount > 0 else ""
    queue_user_message(
        user_id,
        "balance_change",
        "💳 <b>Баланс изменён</b>\n"
        f"Сумма: {sign}{amount:,} ₽\nПричина: {_h(reason or 'Корректировка администратором')}",
        "/account/balance",
    )


def queue_latest_pitching_request(user_id: int, release_id: int, platform_id: int) -> None:
    with _connect() as connection:
        row = connection.execute(
            """
            SELECT id FROM pitching_requests
            WHERE user_id = ? AND release_id = ? AND platform_id = ?
            ORDER BY id DESC LIMIT 1
            """,
            (user_id, release_id, platform_id),
        ).fetchone()
    if row:
        queue_new_pitching_request(int(row["id"]))


def queue_release_message(release_id: int, comment: str) -> None:
    with _connect() as connection:
        row = connection.execute(
            "SELECT user_id, title FROM releases WHERE id = ?", (release_id,)
        ).fetchone()
    if row:
        queue_user_message(
            int(row["user_id"]),
            "release_message",
            "💬 <b>Сообщение по релизу</b>\n"
            f"Релиз: {_h(row['title'])}\n\n{_h(comment.strip()[:1400])}",
            f"/account/releases/{release_id}/review",
        )


def queue_support_status(ticket_id: int, status_value: str) -> None:
    with _connect() as connection:
        row = connection.execute(
            "SELECT user_id, subject FROM support_tickets WHERE id = ?", (ticket_id,)
        ).fetchone()
    if row:
        queue_user_message(
            int(row["user_id"]),
            "support_status",
            "🆘 <b>Статус заявки поддержки изменён</b>\n"
            f"{_h(row['subject'])}\nСтатус: {_h(_support_status_label(status_value))}",
            f"/account/support?ticket={ticket_id}#conversation",
        )


def queue_account_access(user_id: int, *, blocked: bool, reason: str = "") -> None:
    text = "⛔ <b>Доступ к аккаунту ограничен</b>" if blocked else "✅ <b>Доступ к аккаунту восстановлен</b>"
    if reason.strip():
        text += f"\n\nПричина: {_h(reason.strip()[:900])}"
    queue_user_message(user_id, "account_access", text, "/account/support" if blocked else "/account")
