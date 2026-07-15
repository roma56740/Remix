from __future__ import annotations

import asyncio
import contextlib
import html
import logging
from typing import Any

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

from config import (
    ADMIN_TELEGRAM_IDS,
    BOT_ENABLED,
    BOT_TOKEN,
    BOT_USERNAME,
    MIN_PAYOUT_RUB,
    SITE_URL,
    TELEGRAM_OUTBOX_INTERVAL,
    TELEGRAM_POLLING_TIMEOUT,
)
from database import _connect
from platform_service import create_payout_request
from telegram_repository import (
    authenticate_telegram_user,
    clear_dialog_session,
    disconnect_telegram_account,
    fetch_pending_outbox,
    get_bot_dashboard,
    get_bot_notifications,
    get_bot_pitching,
    get_bot_releases,
    get_dialog_session,
    get_payout_context,
    get_user_by_telegram_id,
    link_telegram_account,
    mark_outbox_error,
    mark_outbox_sent,
    queue_new_payout,
    set_dialog_session,
)

logger = logging.getLogger("ramix.telegram")
router = Router(name="ramix_music_bot")

RELEASE_STATUS = {
    "draft": "📝 Черновик",
    "moderation": "🕘 На модерации",
    "accepted": "✅ Принят",
    "published": "🚀 Опубликован",
    "changes_required": "🛠 Нужны правки",
    "rejected": "❌ Отклонён",
}

PITCHING_STATUS = {
    "draft": "📝 Черновик",
    "pending": "🕘 Отправлена",
    "submitted": "🕘 Отправлена",
    "in_review": "🔎 Рассматривается",
    "accepted": "✅ Принята",
    "approved": "✅ Одобрена",
    "rejected": "❌ Отклонена",
    "completed": "🏁 Завершена",
}


def _money(value: int | None) -> str:
    return f"{int(value or 0):,}".replace(",", " ") + " ₽"


def _is_admin_telegram(telegram_id: int) -> bool:
    return telegram_id in ADMIN_TELEGRAM_IDS


def _main_keyboard(*, connected: bool, is_admin: bool = False) -> ReplyKeyboardMarkup:
    if not connected:
        rows = [
            [KeyboardButton(text="🔗 Подключить аккаунт")],
            [KeyboardButton(text="🌐 Открыть сайт"), KeyboardButton(text="ℹ️ Помощь")],
        ]
    else:
        rows = [
            [KeyboardButton(text="🏠 Мой кабинет"), KeyboardButton(text="💿 Мои релизы")],
            [KeyboardButton(text="💸 Запросить выплату"), KeyboardButton(text="🎯 Питчинг")],
            [KeyboardButton(text="🔔 Уведомления"), KeyboardButton(text="🆘 Поддержка")],
        ]
        if is_admin:
            rows.append([KeyboardButton(text="🛡 Админ-панель"), KeyboardButton(text="📥 Очереди")])
        rows.append([KeyboardButton(text="⚙️ Настройки Telegram")])
    return ReplyKeyboardMarkup(
        keyboard=rows,
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Выберите действие",
    )


def _url_keyboard(text: str, path: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=text, url=f"{SITE_URL}{path}")]
        ]
    )


async def _delete_private_message(message: Message) -> None:
    with contextlib.suppress(TelegramAPIError):
        await message.delete()


def _linked_user(message: Message) -> dict[str, Any] | None:
    if not message.from_user:
        return None
    return get_user_by_telegram_id(message.from_user.id)


async def _require_linked(message: Message) -> dict[str, Any] | None:
    user = _linked_user(message)
    if not user:
        await message.answer(
            "🔐 <b>Сначала подключите аккаунт RAMIX MUSIC.</b>\n\n"
            "Нажмите «🔗 Подключить аккаунт» и следуйте подсказкам.",
            reply_markup=_main_keyboard(
                connected=False,
                is_admin=_is_admin_telegram(message.from_user.id if message.from_user else 0),
            ),
        )
        return None
    if bool(user.get("is_blocked")):
        await message.answer(
            "⛔ Доступ к аккаунту ограничен. Обратитесь в поддержку на сайте.",
            reply_markup=_url_keyboard("Открыть поддержку", "/account/support"),
        )
        return None
    return user


@router.message(CommandStart())
async def command_start(message: Message) -> None:
    if not message.from_user:
        return
    clear_dialog_session(message.from_user.id)
    user = _linked_user(message)
    if user:
        await message.answer(
            "🎵 <b>RAMIX MUSIC</b>\n\n"
            f"С возвращением, <b>{html.escape(str(user.get('name') or 'артист'))}</b>!\n"
            "Здесь можно следить за релизами, получать уведомления и создавать заявки на выплату.",
            reply_markup=_main_keyboard(
                connected=True,
                is_admin=bool(user.get("is_admin")) or _is_admin_telegram(message.from_user.id),
            ),
        )
    else:
        await message.answer(
            "🎵 <b>Добро пожаловать в RAMIX MUSIC</b>\n\n"
            "Подключите аккаунт платформы, чтобы получать уведомления о релизах, питчинге, поддержке и выплатах.\n\n"
            "Пароль нужен только для разовой проверки и <b>не сохраняется</b>.",
            reply_markup=_main_keyboard(
                connected=False,
                is_admin=_is_admin_telegram(message.from_user.id),
            ),
        )


@router.message(Command("cancel"))
@router.message(F.text == "Отмена")
async def command_cancel(message: Message) -> None:
    if not message.from_user:
        return
    clear_dialog_session(message.from_user.id)
    user = _linked_user(message)
    await message.answer(
        "Действие отменено.",
        reply_markup=_main_keyboard(
            connected=bool(user),
            is_admin=bool(user and user.get("is_admin")) or _is_admin_telegram(message.from_user.id),
        ),
    )


@router.message(Command("connect"))
@router.message(F.text == "🔗 Подключить аккаунт")
async def connect_account(message: Message) -> None:
    if not message.from_user:
        return
    current = _linked_user(message)
    if current:
        await message.answer(
            f"✅ Уже подключён аккаунт <b>@{html.escape(str(current.get('username') or 'artist'))}</b>.",
            reply_markup=_main_keyboard(
                connected=True,
                is_admin=bool(current.get("is_admin")) or _is_admin_telegram(message.from_user.id),
            ),
        )
        return
    set_dialog_session(message.from_user.id, message.chat.id, "await_login")
    await message.answer(
        "🔗 <b>Подключение аккаунта</b>\n\n"
        "Отправьте логин или email, который используете на сайте.\n\n"
        "Для отмены: /cancel",
        reply_markup=ReplyKeyboardRemove(),
    )


@router.message(F.text == "🏠 Мой кабинет")
async def user_dashboard(message: Message) -> None:
    user = await _require_linked(message)
    if not user:
        return
    data = get_bot_dashboard(int(user["id"]))
    await message.answer(
        "🏠 <b>Мой кабинет</b>\n\n"
        f"👤 @{html.escape(str(user.get('username') or 'artist'))}\n"
        f"💿 Релизов: <b>{data['release_count']}</b>\n"
        f"🕘 На модерации: <b>{data['moderation_count']}</b>\n"
        f"🚀 Принято и опубликовано: <b>{data['published_count']}</b>\n"
        f"💳 Доступно: <b>{_money(int(user.get('balance') or 0))}</b>\n"
        f"🔔 Непрочитанных уведомлений: <b>{data['unread']}</b>",
        reply_markup=_url_keyboard("Открыть личный кабинет", "/account"),
    )


@router.message(F.text == "💿 Мои релизы")
async def user_releases(message: Message) -> None:
    user = await _require_linked(message)
    if not user:
        return
    items = get_bot_releases(int(user["id"]))
    if not items:
        await message.answer(
            "💿 <b>Релизов пока нет</b>\n\nСоздайте первый релиз в личном кабинете.",
            reply_markup=_url_keyboard("Создать релиз", "/account/releases/new"),
        )
        return
    lines = ["💿 <b>Последние релизы</b>"]
    for item in items:
        status_label = RELEASE_STATUS.get(str(item.get("status")), str(item.get("status") or "—"))
        lines.append(
            "\n"
            f"<b>{html.escape(str(item.get('title') or 'Без названия'))}</b>\n"
            f"{status_label} · треков: {int(item.get('track_count') or 0)}"
        )
    await message.answer("\n".join(lines), reply_markup=_url_keyboard("Все релизы", "/account/releases"))


@router.message(F.text == "🎯 Питчинг")
async def user_pitching(message: Message) -> None:
    user = await _require_linked(message)
    if not user:
        return
    items = get_bot_pitching(int(user["id"]))
    lines = [
        "🎯 <b>Питчинг релизов</b>",
        "\nПитчинг — это заявка редакции музыкальной площадки на рассмотрение будущего принятого релиза.",
    ]
    if items:
        lines.append("\n<b>Последние заявки:</b>")
        for item in items:
            lines.append(
                "\n"
                f"<b>{html.escape(str(item.get('release_title') or 'Релиз'))}</b> → "
                f"{html.escape(str(item.get('platform_name') or 'Площадка'))}\n"
                f"{PITCHING_STATUS.get(str(item.get('status')), str(item.get('status') or '—'))}"
            )
    else:
        lines.append("\nУ вас пока нет заявок на питчинг.")
    await message.answer("\n".join(lines), reply_markup=_url_keyboard("Открыть питчинг", "/account/pitching"))


@router.message(F.text == "🔔 Уведомления")
async def user_notifications(message: Message) -> None:
    user = await _require_linked(message)
    if not user:
        return
    items = get_bot_notifications(int(user["id"]))
    if not items:
        await message.answer("🔔 Уведомлений пока нет.")
        return
    lines = ["🔔 <b>Последние уведомления</b>"]
    for item in items:
        marker = "🟠" if not bool(item.get("is_read")) else "⚫"
        lines.append(
            f"\n{marker} <b>{html.escape(str(item.get('title') or 'Уведомление'))}</b>\n"
            f"{html.escape(str(item.get('body') or ''))[:600]}"
        )
    await message.answer("\n".join(lines), reply_markup=_url_keyboard("Все уведомления", "/account/notifications"))


@router.message(F.text == "🆘 Поддержка")
async def user_support(message: Message) -> None:
    user = await _require_linked(message)
    if not user:
        return
    await message.answer(
        "🆘 <b>Поддержка RAMIX MUSIC</b>\n\n"
        "На сайте доступен быстрый FAQ, создание заявки и полная история переписки с администратором.",
        reply_markup=_url_keyboard("Открыть поддержку", "/account/support"),
    )


@router.message(F.text == "🌐 Открыть сайт")
async def open_site(message: Message) -> None:
    await message.answer("🌐 RAMIX MUSIC", reply_markup=_url_keyboard("Открыть сайт", "/"))


@router.message(F.text == "ℹ️ Помощь")
async def bot_help(message: Message) -> None:
    await message.answer(
        "ℹ️ <b>Как пользоваться ботом</b>\n\n"
        "1. Нажмите «Подключить аккаунт».\n"
        "2. Введите логин или email.\n"
        "3. Введите пароль — сообщение будет удалено после проверки.\n"
        "4. Используйте постоянное меню внизу.\n\n"
        "Отмена текущего действия: /cancel",
    )


@router.message(F.text == "💸 Запросить выплату")
async def start_payout(message: Message) -> None:
    user = await _require_linked(message)
    if not user or not message.from_user:
        return
    context = get_payout_context(int(user["id"]))
    if context["active"]:
        await message.answer(
            "🕘 У вас уже есть активная заявка на выплату.\n"
            f"Сумма: <b>{_money(context['active']['amount'])}</b>",
            reply_markup=_url_keyboard("История выплат", "/account/balance"),
        )
        return
    if context["balance"] < MIN_PAYOUT_RUB:
        await message.answer(
            "💳 Недостаточно средств для выплаты.\n\n"
            f"Доступно: <b>{_money(context['balance'])}</b>\n"
            f"Минимальная сумма: <b>{_money(MIN_PAYOUT_RUB)}</b>",
            reply_markup=_url_keyboard("Открыть баланс", "/account/balance"),
        )
        return
    set_dialog_session(message.from_user.id, message.chat.id, "payout_amount")
    await message.answer(
        "💸 <b>Новая заявка на выплату</b>\n\n"
        f"Доступно: <b>{_money(context['balance'])}</b>\n"
        f"Введите сумму от {MIN_PAYOUT_RUB} ₽ целым числом.\n\n"
        "Для отмены: /cancel",
        reply_markup=ReplyKeyboardRemove(),
    )


@router.message(F.text == "⚙️ Настройки Telegram")
async def telegram_settings(message: Message) -> None:
    user = await _require_linked(message)
    if not user or not message.from_user:
        return
    await message.answer(
        "⚙️ <b>Настройки Telegram</b>\n\n"
        f"Подключён аккаунт: <b>@{html.escape(str(user.get('username') or 'artist'))}</b>\n"
        "Уведомления о релизах, питчинге, поддержке, балансе и выплатах включены.\n\n"
        "Для отключения используйте команду /disconnect.",
        reply_markup=_main_keyboard(
            connected=True,
            is_admin=bool(user.get("is_admin")) or _is_admin_telegram(message.from_user.id),
        ),
    )


@router.message(Command("disconnect"))
async def disconnect_command(message: Message) -> None:
    if not message.from_user:
        return
    disconnected = disconnect_telegram_account(telegram_id=message.from_user.id)
    clear_dialog_session(message.from_user.id)
    await message.answer(
        "🔌 Аккаунт отключён от Telegram." if disconnected else "Аккаунт Telegram не был подключён.",
        reply_markup=_main_keyboard(
            connected=False,
            is_admin=_is_admin_telegram(message.from_user.id),
        ),
    )


@router.message(F.text == "🛡 Админ-панель")
async def admin_panel(message: Message) -> None:
    if not message.from_user:
        return
    user = _linked_user(message)
    if not (_is_admin_telegram(message.from_user.id) or (user and bool(user.get("is_admin")))):
        await message.answer("Доступно только администратору.")
        return
    await message.answer("🛡 <b>Админ-панель RAMIX MUSIC</b>", reply_markup=_url_keyboard("Открыть админ-панель", "/admin"))


@router.message(F.text == "📥 Очереди")
async def admin_queues(message: Message) -> None:
    if not message.from_user:
        return
    user = _linked_user(message)
    if not (_is_admin_telegram(message.from_user.id) or (user and bool(user.get("is_admin")))):
        await message.answer("Доступно только администратору.")
        return
    with _connect() as connection:
        moderation = int(connection.execute("SELECT COUNT(*) FROM releases WHERE status = 'moderation'").fetchone()[0])
        support = int(connection.execute("SELECT COUNT(*) FROM support_tickets WHERE status NOT IN ('closed')").fetchone()[0])
        pitching = int(connection.execute("SELECT COUNT(*) FROM pitching_requests WHERE status IN ('pending','submitted','in_review')").fetchone()[0])
        payouts = int(connection.execute("SELECT COUNT(*) FROM payout_requests WHERE status IN ('pending','processing')").fetchone()[0])
    await message.answer(
        "📥 <b>Текущие очереди</b>\n\n"
        f"💿 Модерация релизов: <b>{moderation}</b>\n"
        f"🆘 Поддержка: <b>{support}</b>\n"
        f"🎯 Питчинг: <b>{pitching}</b>\n"
        f"💸 Выплаты: <b>{payouts}</b>",
        reply_markup=_url_keyboard("Открыть админ-панель", "/admin"),
    )


@router.message(F.photo | F.document)
async def unsupported_file(message: Message) -> None:
    if not message.from_user:
        return
    await _delete_private_message(message)
    session = get_dialog_session(message.from_user.id)
    await message.answer(
        "Для этого шага отправьте текст. Файл удалён из диалога."
        if session
        else "Бот принимает команды и текстовые данные. Ненужный файл удалён из диалога."
    )


@router.message(F.text)
async def dialog_text(message: Message) -> None:
    if not message.from_user or not message.text:
        return
    session = get_dialog_session(message.from_user.id)
    if not session:
        await message.answer(
            "Выберите действие в меню.",
            reply_markup=_main_keyboard(
                connected=bool(_linked_user(message)),
                is_admin=_is_admin_telegram(message.from_user.id),
            ),
        )
        return

    step = str(session.get("step") or "")
    text = message.text.strip()
    if step == "await_login":
        from telegram_repository import find_user_for_telegram_login

        user = find_user_for_telegram_login(text)
        if not user:
            await message.answer(
                "Не нашёл аккаунт с таким логином или email. Проверьте данные и отправьте ещё раз.\n\n"
                "Для отмены: /cancel"
            )
            return
        if bool(user.get("is_blocked")):
            clear_dialog_session(message.from_user.id)
            await message.answer("⛔ Этот аккаунт заблокирован. Обратитесь в поддержку.")
            return
        set_dialog_session(
            message.from_user.id,
            message.chat.id,
            "await_password",
            login_value=text,
        )
        await message.answer(
            "🔑 Теперь отправьте пароль от аккаунта.\n\n"
            "Сообщение с паролем будет удалено сразу после проверки. Пароль не сохраняется."
        )
        return

    if step == "await_password":
        login_value = str(session.get("login_value") or "")
        await _delete_private_message(message)
        user = authenticate_telegram_user(login_value, text)
        if not user:
            await message.answer(
                "❌ Неверный пароль. Попробуйте ещё раз или отмените действие: /cancel"
            )
            return
        link_telegram_account(
            int(user["id"]),
            telegram_id=message.from_user.id,
            chat_id=message.chat.id,
            telegram_username=message.from_user.username,
        )
        clear_dialog_session(message.from_user.id)
        await message.answer(
            "✅ <b>Аккаунт успешно подключён</b>\n\n"
            f"Пользователь: <b>@{html.escape(str(user.get('username') or 'artist'))}</b>\n"
            "Теперь уведомления будут приходить в этот чат.",
            reply_markup=_main_keyboard(
                connected=True,
                is_admin=bool(user.get("is_admin")) or _is_admin_telegram(message.from_user.id),
            ),
        )
        return

    if step == "payout_amount":
        compact = text.replace(" ", "").replace("₽", "")
        try:
            amount = int(compact)
        except ValueError:
            amount = 0
        user = _linked_user(message)
        if not user:
            clear_dialog_session(message.from_user.id)
            await message.answer("Сначала подключите аккаунт.")
            return
        context = get_payout_context(int(user["id"]))
        if amount < MIN_PAYOUT_RUB:
            await message.answer(f"Минимальная сумма выплаты — {_money(MIN_PAYOUT_RUB)}. Введите другую сумму.")
            return
        if amount > context["balance"]:
            await message.answer(f"Недостаточно средств. Доступно: {_money(context['balance'])}.")
            return
        set_dialog_session(
            message.from_user.id,
            message.chat.id,
            "payout_details",
            payload={"amount": amount},
        )
        await message.answer(
            "📨 Отправьте Telegram username или другой контакт для связи по выплате.\n\n"
            "Не отправляйте пароль, коды подтверждения и полный номер банковской карты."
        )
        return

    if step == "payout_details":
        user = _linked_user(message)
        if not user:
            clear_dialog_session(message.from_user.id)
            await message.answer("Сначала подключите аккаунт.")
            return
        amount = int((session.get("payload") or {}).get("amount") or 0)
        details = text[:800]
        await _delete_private_message(message)
        ok, result = create_payout_request(
            int(user["id"]), amount=amount, method="telegram", details=details
        )
        clear_dialog_session(message.from_user.id)
        if ok:
            queue_new_payout(int(user["id"]))
            await message.answer(
                "✅ <b>Заявка на выплату создана</b>\n\n"
                f"Сумма: <b>{_money(amount)}</b>\n"
                "Администратор получил уведомление. Статус придёт в этот чат.",
                reply_markup=_main_keyboard(
                    connected=True,
                    is_admin=bool(user.get("is_admin")) or _is_admin_telegram(message.from_user.id),
                ),
            )
        else:
            await message.answer(
                f"❌ {html.escape(result)}",
                reply_markup=_main_keyboard(
                    connected=True,
                    is_admin=bool(user.get("is_admin")) or _is_admin_telegram(message.from_user.id),
                ),
            )
        return

    clear_dialog_session(message.from_user.id)
    await message.answer("Сессия завершена. Выберите действие в меню.")


class TelegramRuntime:
    def __init__(self) -> None:
        self.bot: Bot | None = None
        self.dispatcher: Dispatcher | None = None
        self.polling_task: asyncio.Task[None] | None = None
        self.outbox_task: asyncio.Task[None] | None = None
        self.enabled = bool(BOT_ENABLED and BOT_TOKEN)
        self.running = False
        self.bot_username = BOT_USERNAME
        self.last_error: str | None = None

    async def start(self) -> None:
        if not self.enabled or self.running:
            return
        try:
            self.bot = Bot(
                BOT_TOKEN,
                default=DefaultBotProperties(parse_mode=ParseMode.HTML),
            )
            self.dispatcher = Dispatcher()
            self.dispatcher.include_router(router)
            await self.bot.delete_webhook(drop_pending_updates=False)
            me = await self.bot.get_me()
            self.bot_username = me.username or self.bot_username
            await self.bot.set_my_commands(
                [
                    BotCommand(command="start", description="Открыть главное меню"),
                    BotCommand(command="connect", description="Подключить аккаунт"),
                    BotCommand(command="disconnect", description="Отключить аккаунт"),
                    BotCommand(command="cancel", description="Отменить текущее действие"),
                ]
            )
            self.polling_task = asyncio.create_task(
                self.dispatcher.start_polling(
                    self.bot,
                    handle_signals=False,
                    close_bot_session=False,
                    polling_timeout=TELEGRAM_POLLING_TIMEOUT,
                    allowed_updates=self.dispatcher.resolve_used_update_types(),
                ),
                name="telegram-polling",
            )
            self.polling_task.add_done_callback(self._polling_finished)
            self.outbox_task = asyncio.create_task(self._outbox_worker(), name="telegram-outbox")
            self.running = True
            logger.info("Telegram bot @%s started", self.bot_username)
        except Exception as exc:  # noqa: BLE001
            self.last_error = str(exc)
            logger.exception("Telegram bot failed to start")
            await self.stop()

    def _polling_finished(self, task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        try:
            error = task.exception()
        except asyncio.CancelledError:
            return
        if error:
            self.last_error = str(error)
            self.running = False
            logger.error("Telegram polling stopped: %s", error)

    async def stop(self) -> None:
        self.running = False
        if self.dispatcher and self.polling_task and not self.polling_task.done():
            with contextlib.suppress(Exception):
                await self.dispatcher.stop_polling()
        for task in (self.outbox_task, self.polling_task):
            if task and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        if self.bot:
            with contextlib.suppress(Exception):
                await self.bot.session.close()
        self.bot = None
        self.dispatcher = None
        self.polling_task = None
        self.outbox_task = None

    async def _outbox_worker(self) -> None:
        while True:
            try:
                await self._deliver_outbox()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.exception("Telegram outbox worker error")
            await asyncio.sleep(TELEGRAM_OUTBOX_INTERVAL)

    async def _deliver_outbox(self) -> None:
        if not self.bot:
            return
        for item in fetch_pending_outbox():
            outbox_id = int(item["id"])
            chat_id: str | None
            if str(item.get("recipient_type")) == "user":
                if not bool(item.get("telegram_connected")) or not bool(item.get("telegram_notifications_enabled", 1)):
                    mark_outbox_sent(outbox_id)
                    continue
                chat_id = str(item.get("telegram_chat_id") or item.get("telegram_id") or "")
            else:
                chat_id = str(item.get("chat_id") or "")
            if not chat_id:
                mark_outbox_sent(outbox_id)
                continue
            reply_markup = None
            if item.get("action_url"):
                reply_markup = InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(text="Открыть в RAMIX MUSIC", url=str(item["action_url"]))]
                    ]
                )
            try:
                await self.bot.send_message(
                    chat_id=chat_id,
                    text=str(item["message_text"]),
                    reply_markup=reply_markup,
                    disable_web_page_preview=True,
                )
                mark_outbox_sent(outbox_id)
            except TelegramAPIError as exc:
                attempts = int(item.get("attempts") or 0) + 1
                mark_outbox_error(outbox_id, str(exc), attempts)

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "running": self.running,
            "username": self.bot_username or None,
            "last_error": self.last_error,
        }


telegram_runtime = TelegramRuntime()
