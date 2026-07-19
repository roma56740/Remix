from __future__ import annotations

import asyncio
import contextlib
import html
import logging

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
    BOT_ENABLED,
    BOT_TOKEN,
    BOT_USERNAME,
    SITE_URL,
    TELEGRAM_OUTBOX_INTERVAL,
    TELEGRAM_POLLING_TIMEOUT,
)
from telegram_repository import (
    authenticate_telegram_user,
    clear_dialog_session,
    disconnect_telegram_account,
    fetch_pending_outbox,
    find_user_for_telegram_login,
    get_dialog_session,
    get_user_by_telegram_id,
    link_telegram_account,
    mark_outbox_error,
    mark_outbox_sent,
    set_dialog_session,
)

logger = logging.getLogger("ramix.telegram")
router = Router(name="ramix_music_notifications")


def _keyboard(connected: bool) -> ReplyKeyboardMarkup:
    rows = (
        [[KeyboardButton(text="🌐 Открыть личный кабинет")], [KeyboardButton(text="🔌 Отключить уведомления")]]
        if connected
        else [[KeyboardButton(text="🔗 Подключить аккаунт")], [KeyboardButton(text="🌐 Открыть сайт")]]
    )
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True, is_persistent=True)


def _url_keyboard(text: str, path: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=text, url=f"{SITE_URL}{path}")]]
    )


async def _delete_message(message: Message) -> None:
    with contextlib.suppress(TelegramAPIError):
        await message.delete()


@router.message(CommandStart())
async def command_start(message: Message) -> None:
    if not message.from_user:
        return
    clear_dialog_session(message.from_user.id)
    user = get_user_by_telegram_id(message.from_user.id)
    if user:
        await message.answer(
            "🎵 <b>RAMIX MUSIC</b>\n\n"
            f"Аккаунт <b>@{html.escape(str(user.get('username') or 'artist'))}</b> подключён.\n"
            "Сюда будут приходить уведомления о проверке релизов, комментариях модератора, UPC и выплатах.",
            reply_markup=_keyboard(True),
        )
        return
    await message.answer(
        "🎵 <b>Уведомления RAMIX MUSIC</b>\n\n"
        "Подключите аккаунт с сайта, чтобы получать короткие уведомления об изменениях по вашим релизам.\n\n"
        "Пароль используется один раз для проверки, сразу удаляется из чата и не сохраняется ботом.",
        reply_markup=_keyboard(False),
    )


@router.message(Command("connect"))
@router.message(F.text == "🔗 Подключить аккаунт")
async def connect_account(message: Message) -> None:
    if not message.from_user:
        return
    user = get_user_by_telegram_id(message.from_user.id)
    if user:
        await message.answer(
            f"✅ Уже подключён аккаунт <b>@{html.escape(str(user.get('username') or 'artist'))}</b>.",
            reply_markup=_keyboard(True),
        )
        return
    set_dialog_session(message.from_user.id, message.chat.id, "await_login")
    await message.answer(
        "🔗 <b>Подключение уведомлений</b>\n\n"
        "Отправьте логин или email от личного кабинета RAMIX MUSIC.\n\n"
        "Отмена: /cancel",
        reply_markup=ReplyKeyboardRemove(),
    )


@router.message(Command("cancel"))
async def cancel_action(message: Message) -> None:
    if not message.from_user:
        return
    clear_dialog_session(message.from_user.id)
    await message.answer(
        "Действие отменено.",
        reply_markup=_keyboard(bool(get_user_by_telegram_id(message.from_user.id))),
    )


@router.message(Command("disconnect"))
@router.message(F.text == "🔌 Отключить уведомления")
async def disconnect_account(message: Message) -> None:
    if not message.from_user:
        return
    disconnected = disconnect_telegram_account(telegram_id=message.from_user.id)
    clear_dialog_session(message.from_user.id)
    await message.answer(
        "🔌 Уведомления отключены." if disconnected else "Аккаунт не был подключён.",
        reply_markup=_keyboard(False),
    )


@router.message(F.text == "🌐 Открыть сайт")
async def open_site(message: Message) -> None:
    await message.answer("RAMIX MUSIC", reply_markup=_url_keyboard("Открыть сайт", "/"))


@router.message(F.text == "🌐 Открыть личный кабинет")
async def open_account(message: Message) -> None:
    await message.answer(
        "Личный кабинет RAMIX MUSIC",
        reply_markup=_url_keyboard("Открыть личный кабинет", "/account"),
    )


@router.message(F.photo | F.document | F.video | F.audio | F.voice)
async def remove_unused_files(message: Message) -> None:
    await _delete_message(message)
    await message.answer(
        "🗑 Файл удалён. Этот бот используется только для подключения аккаунта и уведомлений."
    )


@router.message(F.text)
async def text_handler(message: Message) -> None:
    if not message.from_user or not message.text:
        return
    session = get_dialog_session(message.from_user.id)
    if not session:
        await message.answer(
            "Этот бот отправляет уведомления RAMIX MUSIC. Выберите действие в меню.",
            reply_markup=_keyboard(bool(get_user_by_telegram_id(message.from_user.id))),
        )
        return

    step = str(session.get("step") or "")
    value = message.text.strip()
    if step == "await_login":
        user = find_user_for_telegram_login(value)
        if not user:
            await message.answer("Аккаунт не найден. Проверьте логин или email и отправьте ещё раз.")
            return
        if bool(user.get("is_blocked")):
            clear_dialog_session(message.from_user.id)
            await message.answer("⛔ Этот аккаунт заблокирован.", reply_markup=_keyboard(False))
            return
        set_dialog_session(
            message.from_user.id,
            message.chat.id,
            "await_password",
            login_value=value,
        )
        await message.answer(
            "🔑 Отправьте пароль. Сообщение будет удалено сразу после проверки."
        )
        return

    if step == "await_password":
        login_value = str(session.get("login_value") or "")
        await _delete_message(message)
        user = authenticate_telegram_user(login_value, value)
        if not user:
            await message.answer("❌ Неверный пароль. Попробуйте ещё раз или используйте /cancel.")
            return
        link_telegram_account(
            int(user["id"]),
            telegram_id=message.from_user.id,
            chat_id=message.chat.id,
            telegram_username=message.from_user.username,
        )
        clear_dialog_session(message.from_user.id)
        await message.answer(
            "✅ <b>Уведомления подключены</b>\n\n"
            f"Аккаунт: <b>@{html.escape(str(user.get('username') or 'artist'))}</b>\n"
            "Теперь сюда будут приходить изменения по вашим релизам.",
            reply_markup=_keyboard(True),
        )
        return

    clear_dialog_session(message.from_user.id)
    await message.answer("Сессия завершена.", reply_markup=_keyboard(False))


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
            self.bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
            self.dispatcher = Dispatcher()
            self.dispatcher.include_router(router)
            await self.bot.delete_webhook(drop_pending_updates=False)
            me = await self.bot.get_me()
            self.bot_username = me.username or self.bot_username
            await self.bot.set_my_commands(
                [
                    BotCommand(command="start", description="Открыть бота"),
                    BotCommand(command="connect", description="Подключить уведомления"),
                    BotCommand(command="disconnect", description="Отключить уведомления"),
                    BotCommand(command="cancel", description="Отменить подключение"),
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
            if str(item.get("recipient_type")) != "user":
                mark_outbox_sent(outbox_id)
                continue
            if not bool(item.get("telegram_connected")) or not bool(item.get("telegram_notifications_enabled", 1)):
                mark_outbox_sent(outbox_id)
                continue
            chat_id = str(item.get("telegram_chat_id") or item.get("telegram_id") or "")
            if not chat_id:
                mark_outbox_sent(outbox_id)
                continue
            reply_markup = None
            if item.get("action_url"):
                reply_markup = InlineKeyboardMarkup(
                    inline_keyboard=[[InlineKeyboardButton(text="Открыть в RAMIX MUSIC", url=str(item["action_url"]))]]
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

    def status(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "running": self.running,
            "username": self.bot_username or None,
            "last_error": self.last_error,
        }


telegram_runtime = TelegramRuntime()
