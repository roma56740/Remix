from __future__ import annotations

import io
import json
import zipfile
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from auth_utils import hash_password, is_valid_email, normalize_email, verify_password
from config import ADMIN_EMAILS, BOT_USERNAME, COOKIE_HTTPS_ONLY, SESSION_SECRET
from database import (
    RELEASE_EDITABLE_STATUSES,
    RELEASE_LANGUAGES,
    RELEASE_TYPES,
    TRACK_LANGUAGES,
    USERNAME_PATTERN,
    SUPPORT_CATEGORIES,
    add_user_support_message,
    admin_reply_support_ticket,
    admin_update_pitching_request,
    admin_update_support_ticket_status,
    close_user_support_ticket,
    create_pitching_request,
    create_release,
    create_release_track,
    create_support_ticket,
    create_user,
    delete_release,
    delete_release_track,
    email_is_taken,
    get_admin_support_ticket,
    get_dashboard_data,
    get_pitching_page_data,
    get_release_for_user,
    get_track_for_user,
    get_user_by_email,
    get_user_by_id,
    get_user_support_ticket,
    init_database,
    list_admin_pitching_requests,
    list_admin_support_tickets,
    list_release_rule_sections,
    list_pitching_resources,
    list_release_tracks,
    list_support_faqs,
    list_user_releases,
    list_user_support_tickets,
    submit_release_for_moderation,
    update_release_details,
    update_release_track,
    update_user_password,
    update_user_profile,
    username_is_taken,
)
from release_service import (
    UploadValidationError,
    delete_upload,
    ensure_upload_directories,
    format_timecode,
    parse_timecode,
    resolve_upload,
    save_audio_upload,
    save_cover_upload,
)
from platform_service import (
    adjust_user_balance,
    create_payout_request,
    delete_service_news,
    get_admin_imports,
    get_admin_overview,
    get_admin_pitching_request,
    get_admin_release,
    get_admin_track,
    get_admin_sidebar_counts,
    get_admin_user,
    get_balance_page_data,
    get_pitching_page_data_v2,
    get_statistics_page_data,
    get_unread_notification_count,
    list_admin_pitching_v2,
    list_admin_releases,
    list_admin_support_summary,
    list_admin_users,
    list_notifications,
    list_payout_requests,
    list_service_news_admin,
    mark_notifications_read,
    mark_user_login,
    moderate_release,
    process_payout_request,
    save_service_news,
    set_release_listens,
    set_release_upc,
    set_track_listens,
    set_user_blocked,
    update_admin_pitching_request,
)
from xlsx_service import (
    build_import_template,
    build_quarter_report,
    process_import_file,
    resolve_import_file,
)
from telegram_repository import (
    disconnect_telegram_account,
    queue_account_access,
    queue_balance_change,
    queue_latest_pitching_request,
    queue_new_payout,
    queue_new_registration,
    queue_new_release,
    queue_new_support_ticket,
    queue_payout_status,
    queue_pitching_status,
    queue_release_message,
    queue_release_status,
    queue_release_upc,
    queue_support_reply,
    queue_support_status,
)
from telegram_service import telegram_runtime

BASE_DIR = Path(__file__).resolve().parent

ensure_upload_directories()
init_database()


@asynccontextmanager
async def app_lifespan(_: FastAPI):
    await telegram_runtime.start()
    try:
        yield
    finally:
        await telegram_runtime.stop()


app = FastAPI(title="RAMIX MUSIC", version="2.1.0", lifespan=app_lifespan)
app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET,
    max_age=60 * 60 * 24 * 30,
    same_site="lax",
    https_only=COOKIE_HTTPS_ONLY,
)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")


def current_user(request: Request) -> dict[str, Any] | None:
    user_id = request.session.get("user_id")
    if not isinstance(user_id, int):
        return None
    user = get_user_by_id(user_id)
    if not user:
        return None
    user["is_admin"] = bool(user.get("is_admin")) or str(user.get("email", "")).lower() in ADMIN_EMAILS
    return user


def template_context(request: Request, **extra: Any) -> dict[str, Any]:
    user = current_user(request)
    return {
        "request": request,
        "current_user": user,
        "is_authenticated": user is not None,
        **extra,
    }


def account_context(
    request: Request,
    user: dict[str, Any],
    *,
    active_section: str,
    **extra: Any,
) -> dict[str, Any]:
    bot_username = telegram_runtime.bot_username or BOT_USERNAME
    return template_context(
        request,
        dashboard=get_dashboard_data(int(user["id"])),
        notifications_unread=get_unread_notification_count(int(user["id"])),
        active_section=active_section,
        telegram_bot_username=bot_username,
        telegram_bot_url=(f"https://t.me/{bot_username}" if bot_username else None),
        **extra,
    )


def admin_context(
    request: Request,
    user: dict[str, Any],
    *,
    active_admin: str,
    **extra: Any,
) -> dict[str, Any]:
    return template_context(
        request,
        admin_counts=get_admin_sidebar_counts(),
        active_admin=active_admin,
        **extra,
    )


def settings_context(
    request: Request,
    user: dict[str, Any],
    *,
    profile_form: dict[str, str] | None = None,
    profile_error: str | None = None,
    password_error: str | None = None,
    notice: str | None = None,
) -> dict[str, Any]:
    return account_context(
        request,
        user,
        active_section="settings",
        profile_form=profile_form
        or {
            "name": str(user["name"]),
            "email": str(user["email"]),
            "username": str(user["username"] or ""),
        },
        profile_error=profile_error,
        password_error=password_error,
        notice=notice,
    )


SUPPORT_CATEGORY_OPTIONS = ("Общий вопрос", "Релизы", "Аудио", "Права", "Питчинг", "Аккаунт")


def support_page_context(
    request: Request,
    user: dict[str, Any],
    *,
    status_filter: str = "all",
    ticket_id: int | None = None,
    form_values: dict[str, str] | None = None,
    form_errors: dict[str, str] | None = None,
    notice: str | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    ticket_data = list_user_support_tickets(int(user["id"]), status_filter=status_filter)
    selected_ticket = None
    selected_id = ticket_id
    if selected_id is None and ticket_data["items"]:
        selected_id = int(ticket_data["items"][0]["id"])
    if selected_id is not None:
        selected_ticket = get_user_support_ticket(int(user["id"]), selected_id)
        ticket_data = list_user_support_tickets(int(user["id"]), status_filter=status_filter)
    return account_context(
        request,
        user,
        active_section="support",
        faq_items=list_support_faqs(),
        ticket_data=ticket_data,
        selected_ticket=selected_ticket,
        support_categories=SUPPORT_CATEGORY_OPTIONS,
        form_values=form_values or {"subject": "", "category": "Общий вопрос", "message": ""},
        form_errors=form_errors or {},
        notice=notice,
        error=error,
    )


def pitching_page_context(
    request: Request,
    user: dict[str, Any],
    *,
    form_values: dict[str, str] | None = None,
    form_errors: dict[str, str] | None = None,
    notice: str | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    return account_context(
        request,
        user,
        active_section="pitching",
        pitching=get_pitching_page_data_v2(int(user["id"])),
        form_values=form_values or {"release_id": "", "platform_id": "", "message": ""},
        form_errors=form_errors or {},
        notice=notice,
        error=error,
    )


def _require_user(request: Request) -> dict[str, Any] | RedirectResponse:
    user = current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    if bool(user.get("is_blocked")):
        request.session.clear()
        return RedirectResponse(url="/login?blocked=1", status_code=status.HTTP_303_SEE_OTHER)
    return user


def _require_admin(request: Request) -> dict[str, Any] | RedirectResponse:
    user = _require_user(request)
    if isinstance(user, RedirectResponse):
        return user
    if not bool(user.get("is_admin")):
        return RedirectResponse(url="/account", status_code=status.HTTP_303_SEE_OTHER)
    return user


def _clean_line(value: str, max_length: int) -> str:
    return " ".join(value.strip().split())[:max_length]


def _clean_multiline(value: str, max_length: int) -> str:
    lines = [" ".join(line.strip().split()) for line in value.strip().splitlines()]
    return "\n".join(line for line in lines if line)[:max_length]


def _release_form_values(
    *,
    title: str = "",
    release_type: str = "single",
    release_version: str = "",
    genre: str = "",
    metadata_language: str = "ru",
    release_date: str = "",
    is_explicit: bool = False,
) -> dict[str, Any]:
    return {
        "title": title,
        "release_type": release_type,
        "release_version": release_version,
        "genre": genre,
        "metadata_language": metadata_language,
        "release_date": release_date,
        "is_explicit": is_explicit,
    }


def _validate_release_form(form: dict[str, Any]) -> dict[str, str]:
    errors: dict[str, str] = {}
    if len(form["title"]) < 1:
        errors["title"] = "Введите название релиза."
    elif len(form["title"]) > 160:
        errors["title"] = "Название должно быть не длиннее 160 символов."
    if form["release_type"] not in RELEASE_TYPES:
        errors["release_type"] = "Выберите тип релиза."
    if len(form["release_version"]) > 80:
        errors["release_version"] = "Версия должна быть не длиннее 80 символов."
    if len(form["genre"]) < 2:
        errors["genre"] = "Укажите основной жанр."
    elif len(form["genre"]) > 80:
        errors["genre"] = "Название жанра слишком длинное."
    if form["metadata_language"] not in RELEASE_LANGUAGES:
        errors["metadata_language"] = "Выберите язык метаданных."
    release_date_value = str(form.get("release_date") or "")
    try:
        parsed_release_date = datetime.strptime(release_date_value, "%Y-%m-%d").date()
        if parsed_release_date <= date.today():
            errors["release_date"] = "Дата выхода должна быть позже сегодняшнего дня."
    except ValueError:
        errors["release_date"] = "Выберите планируемую дату выхода."
    return errors


def _track_form_values(
    *,
    title: str = "",
    artists: str = "",
    version: str = "",
    lyrics: str = "",
    language: str = "ru",
    lyricist: str = "",
    composer: str = "",
    is_explicit: bool = False,
    tiktok_start: str = "00:00",
    tiktok_end: str = "01:00",
) -> dict[str, Any]:
    return {
        "title": title,
        "artists": artists,
        "version": version,
        "lyrics": lyrics,
        "language": language,
        "lyricist": lyricist,
        "composer": composer,
        "is_explicit": is_explicit,
        "tiktok_start": tiktok_start,
        "tiktok_end": tiktok_end,
    }


def _validate_track_form(form: dict[str, Any]) -> tuple[dict[str, str], int, int]:
    errors: dict[str, str] = {}
    rules = (
        ("title", 1, 160, "Введите название трека."),
        ("artists", 1, 200, "Укажите исполнителя или исполнителей."),
        ("lyricist", 2, 200, "Укажите автора слов."),
        ("composer", 2, 200, "Укажите автора музыки."),
    )
    for field, minimum, maximum, message in rules:
        length = len(str(form[field]))
        if length < minimum:
            errors[field] = message
        elif length > maximum:
            errors[field] = f"Поле должно быть не длиннее {maximum} символов."
    if len(form["version"]) > 80:
        errors["version"] = "Версия должна быть не длиннее 80 символов."
    if len(form["lyrics"]) > 20000:
        errors["lyrics"] = "Текст песни должен быть не длиннее 20 000 символов."
    if form["language"] not in TRACK_LANGUAGES:
        errors["language"] = "Выберите язык исполнения."

    start_seconds = 0
    end_seconds = 60
    try:
        start_seconds = parse_timecode(form["tiktok_start"], field_label="Начало отрывка")
    except ValueError as exc:
        errors["tiktok_start"] = str(exc)
    try:
        end_seconds = parse_timecode(form["tiktok_end"], field_label="Конец отрывка")
    except ValueError as exc:
        errors["tiktok_end"] = str(exc)
    if "tiktok_start" not in errors and "tiktok_end" not in errors:
        if end_seconds <= start_seconds:
            errors["tiktok_end"] = "Конец отрывка должен быть позже начала."
        elif end_seconds - start_seconds > 180:
            errors["tiktok_end"] = "Отрывок должен быть не длиннее 3 минут."
    return errors, start_seconds, end_seconds


def _release_form_context(
    request: Request,
    user: dict[str, Any],
    *,
    form: dict[str, Any],
    errors: dict[str, str] | None = None,
    release: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return account_context(
        request,
        user,
        active_section="releases",
        form=form,
        errors=errors or {},
        release=release,
        minimum_release_date=(date.today() + timedelta(days=1)).isoformat(),
        pitching_recommended_date=(date.today() + timedelta(days=12)).isoformat(),
        pitching_recommended_date_label=(date.today() + timedelta(days=12)).strftime("%d.%m.%Y"),
        step=1,
    )


def _tracks_context(
    request: Request,
    user: dict[str, Any],
    release: dict[str, Any],
    *,
    track_form: dict[str, Any] | None = None,
    track_errors: dict[str, str] | None = None,
    editing_track: dict[str, Any] | None = None,
    notice: str | None = None,
) -> dict[str, Any]:
    return account_context(
        request,
        user,
        active_section="releases",
        release=release,
        tracks=list_release_tracks(int(user["id"]), int(release["id"])),
        track_form=track_form or _track_form_values(artists=str(release["artist_name"])),
        track_errors=track_errors or {},
        editing_track=editing_track,
        open_track_modal=bool(track_errors or editing_track),
        notice=notice,
        step=2,
    )


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context=template_context(request),
    )


@app.get("/register", response_class=HTMLResponse)
async def register_page(request: Request) -> Response:
    if current_user(request):
        return RedirectResponse(url="/account", status_code=status.HTTP_303_SEE_OTHER)
    return templates.TemplateResponse(
        request=request,
        name="register.html",
        context=template_context(request, form={"name": "", "email": ""}, error=None),
    )


@app.post("/register", response_class=HTMLResponse)
async def register(
    request: Request,
    name: str = Form(default=""),
    email: str = Form(default=""),
    password: str = Form(default=""),
    password_confirm: str = Form(default=""),
    accept_policy: str | None = Form(default=None),
) -> Response:
    clean_name = " ".join(name.strip().split())
    clean_email = normalize_email(email)
    form = {"name": clean_name, "email": clean_email}

    error: str | None = None
    if len(clean_name) < 2:
        error = "Введите имя — минимум 2 символа."
    elif len(clean_name) > 80:
        error = "Имя слишком длинное."
    elif not is_valid_email(clean_email):
        error = "Проверьте правильность email."
    elif len(password) < 8:
        error = "Пароль должен содержать минимум 8 символов."
    elif password != password_confirm:
        error = "Пароли не совпадают."
    elif accept_policy != "on":
        error = "Подтвердите согласие с политикой конфиденциальности."
    elif get_user_by_email(clean_email):
        error = "Аккаунт с таким email уже существует."

    if error:
        return templates.TemplateResponse(
            request=request,
            name="register.html",
            context=template_context(request, form=form, error=error),
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )

    user = create_user(clean_name, clean_email, hash_password(password))
    if not user:
        return templates.TemplateResponse(
            request=request,
            name="register.html",
            context=template_context(
                request,
                form=form,
                error="Аккаунт с таким email уже существует.",
            ),
            status_code=status.HTTP_409_CONFLICT,
        )

    queue_new_registration(int(user["id"]))
    request.session.clear()
    request.session["user_id"] = int(user["id"])
    return RedirectResponse(url="/account", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request) -> Response:
    if current_user(request):
        return RedirectResponse(url="/account", status_code=status.HTTP_303_SEE_OTHER)
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context=template_context(request, form={"email": ""}, error="Доступ к аккаунту ограничен администратором." if request.query_params.get("blocked") else None),
    )


@app.post("/login", response_class=HTMLResponse)
async def login(
    request: Request,
    email: str = Form(default=""),
    password: str = Form(default=""),
) -> Response:
    clean_email = normalize_email(email)
    user = get_user_by_email(clean_email)

    if not user or not verify_password(password, str(user["password_hash"])):
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context=template_context(
                request,
                form={"email": clean_email},
                error="Неверный email или пароль.",
            ),
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    if bool(user.get("is_blocked")):
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context=template_context(
                request,
                form={"email": clean_email},
                error=str(user.get("blocked_reason") or "Доступ к аккаунту ограничен администратором."),
            ),
            status_code=status.HTTP_403_FORBIDDEN,
        )

    mark_user_login(int(user["id"]))
    request.session.clear()
    request.session["user_id"] = int(user["id"])
    return RedirectResponse(url="/account", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/account", response_class=HTMLResponse)
async def account(request: Request) -> Response:
    user = _require_user(request)
    if isinstance(user, RedirectResponse):
        return user
    return templates.TemplateResponse(
        request=request,
        name="account.html",
        context=account_context(request, user, active_section="home"),
    )


@app.get("/account/settings", response_class=HTMLResponse)
async def account_settings(request: Request) -> Response:
    user = _require_user(request)
    if isinstance(user, RedirectResponse):
        return user
    notice = request.session.pop("settings_notice", None)
    return templates.TemplateResponse(
        request=request,
        name="settings.html",
        context=settings_context(request, user, notice=notice),
    )


@app.post("/account/settings/profile", response_class=HTMLResponse)
async def account_settings_profile(
    request: Request,
    name: str = Form(default=""),
    email: str = Form(default=""),
    username: str = Form(default=""),
) -> Response:
    user = _require_user(request)
    if isinstance(user, RedirectResponse):
        return user

    clean_name = " ".join(name.strip().split())
    clean_email = normalize_email(email)
    clean_username = username.strip().lower()
    profile_form = {"name": clean_name, "email": clean_email, "username": clean_username}

    error: str | None = None
    if len(clean_name) < 2:
        error = "Введите имя — минимум 2 символа."
    elif len(clean_name) > 80:
        error = "Имя слишком длинное."
    elif not is_valid_email(clean_email):
        error = "Проверьте правильность email."
    elif not USERNAME_PATTERN.fullmatch(clean_username):
        error = "Логин: 3–32 символа, только латинские буквы, цифры и _."
    elif email_is_taken(clean_email, exclude_user_id=int(user["id"])):
        error = "Этот email уже используется другим аккаунтом."
    elif username_is_taken(clean_username, exclude_user_id=int(user["id"])):
        error = "Этот логин уже занят."

    if error:
        return templates.TemplateResponse(
            request=request,
            name="settings.html",
            context=settings_context(
                request,
                user,
                profile_form=profile_form,
                profile_error=error,
            ),
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )

    updated_user = update_user_profile(
        int(user["id"]),
        name=clean_name,
        email=clean_email,
        username=clean_username,
    )
    if not updated_user:
        return templates.TemplateResponse(
            request=request,
            name="settings.html",
            context=settings_context(
                request,
                user,
                profile_form=profile_form,
                profile_error="Не удалось сохранить изменения. Проверьте введённые данные.",
            ),
            status_code=status.HTTP_409_CONFLICT,
        )

    request.session["settings_notice"] = "Личные данные сохранены."
    return RedirectResponse(url="/account/settings", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/account/settings/password", response_class=HTMLResponse)
async def account_settings_password(
    request: Request,
    current_password: str = Form(default=""),
    new_password: str = Form(default=""),
    new_password_confirm: str = Form(default=""),
) -> Response:
    user = _require_user(request)
    if isinstance(user, RedirectResponse):
        return user

    error: str | None = None
    if not verify_password(current_password, str(user["password_hash"])):
        error = "Текущий пароль указан неверно."
    elif len(new_password) < 8:
        error = "Новый пароль должен содержать минимум 8 символов."
    elif new_password != new_password_confirm:
        error = "Новые пароли не совпадают."
    elif current_password == new_password:
        error = "Новый пароль должен отличаться от текущего."

    if error:
        return templates.TemplateResponse(
            request=request,
            name="settings.html",
            context=settings_context(request, user, password_error=error),
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )

    if not update_user_password(int(user["id"]), hash_password(new_password)):
        return templates.TemplateResponse(
            request=request,
            name="settings.html",
            context=settings_context(
                request,
                user,
                password_error="Не удалось изменить пароль. Попробуйте ещё раз.",
            ),
            status_code=status.HTTP_409_CONFLICT,
        )

    request.session["settings_notice"] = "Пароль успешно изменён."
    return RedirectResponse(url="/account/settings", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/account/settings/telegram/disconnect")
async def account_settings_telegram_disconnect(request: Request) -> Response:
    user = _require_user(request)
    if isinstance(user, RedirectResponse):
        return user
    disconnected = disconnect_telegram_account(user_id=int(user["id"]))
    request.session["settings_notice"] = (
        "Telegram отключён от аккаунта."
        if disconnected
        else "Telegram не был подключён к аккаунту."
    )
    return RedirectResponse(url="/account/settings", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/account/releases", response_class=HTMLResponse)
async def releases_page(
    request: Request,
    q: str = Query(default="", max_length=160),
    release_status: str = Query(default="all", alias="status", max_length=30),
) -> Response:
    user = _require_user(request)
    if isinstance(user, RedirectResponse):
        return user
    data = list_user_releases(int(user["id"]), query=q, status_filter=release_status)
    notice = request.session.pop("releases_notice", None)
    return templates.TemplateResponse(
        request=request,
        name="releases.html",
        context=account_context(
            request,
            user,
            active_section="releases",
            releases_data=data,
            notice=notice,
        ),
    )


@app.get("/account/releases/new", response_class=HTMLResponse)
async def release_create_page(request: Request) -> Response:
    user = _require_user(request)
    if isinstance(user, RedirectResponse):
        return user
    return templates.TemplateResponse(
        request=request,
        name="release_form.html",
        context=_release_form_context(
            request,
            user,
            form=_release_form_values(),
        ),
    )


@app.post("/account/releases/new", response_class=HTMLResponse)
async def release_create(
    request: Request,
    title: str = Form(default=""),
    release_type: str = Form(default="single"),
    release_version: str = Form(default=""),
    genre: str = Form(default=""),
    metadata_language: str = Form(default="ru"),
    release_date: str = Form(default=""),
    is_explicit: str | None = Form(default=None),
    cover: UploadFile | None = File(default=None),
) -> Response:
    user = _require_user(request)
    if isinstance(user, RedirectResponse):
        return user

    form = _release_form_values(
        title=_clean_line(title, 200),
        release_type=release_type.strip().lower(),
        release_version=_clean_line(release_version, 100),
        genre=_clean_line(genre, 100),
        metadata_language=metadata_language.strip().lower(),
        release_date=release_date.strip(),
        is_explicit=is_explicit == "on",
    )
    errors = _validate_release_form(form)
    if not cover or not cover.filename:
        errors["cover"] = "Загрузите квадратную обложку релиза."

    if errors:
        if cover:
            await cover.close()
        return templates.TemplateResponse(
            request=request,
            name="release_form.html",
            context=_release_form_context(request, user, form=form, errors=errors),
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )

    saved_cover: dict[str, Any] | None = None
    try:
        saved_cover = await save_cover_upload(cover)
    except UploadValidationError as exc:
        errors["cover"] = str(exc)
    if errors or not saved_cover:
        return templates.TemplateResponse(
            request=request,
            name="release_form.html",
            context=_release_form_context(request, user, form=form, errors=errors),
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )

    try:
        release = create_release(
            int(user["id"]),
            title=form["title"],
            artist_name=str(user["name"]),
            release_type=form["release_type"],
            release_version=form["release_version"],
            genre=form["genre"],
            metadata_language=form["metadata_language"],
            release_date=form["release_date"],
            is_explicit=bool(form["is_explicit"]),
            cover=saved_cover,
        )
    except Exception:
        delete_upload(saved_cover["path"])
        errors["form"] = "Не удалось сохранить релиз. Попробуйте ещё раз."
        return templates.TemplateResponse(
            request=request,
            name="release_form.html",
            context=_release_form_context(request, user, form=form, errors=errors),
            status_code=status.HTTP_409_CONFLICT,
        )

    request.session["track_notice"] = "Основная информация сохранена. Теперь добавьте треки."
    return RedirectResponse(
        url=f"/account/releases/{release['id']}/tracks",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.get("/account/releases/{release_id}/edit", response_class=HTMLResponse)
async def release_edit_page(request: Request, release_id: int) -> Response:
    user = _require_user(request)
    if isinstance(user, RedirectResponse):
        return user
    release = get_release_for_user(int(user["id"]), release_id)
    if not release:
        return RedirectResponse(url="/account/releases", status_code=status.HTTP_303_SEE_OTHER)
    if not release["is_editable"]:
        return RedirectResponse(
            url=f"/account/releases/{release_id}/review",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    form = _release_form_values(
        title=str(release["title"]),
        release_type=str(release["release_type"]),
        release_version=str(release.get("release_version") or ""),
        genre=str(release.get("genre") or ""),
        metadata_language=str(release.get("metadata_language") or "ru"),
        release_date=str(release.get("release_date") or ""),
        is_explicit=bool(release.get("is_explicit")),
    )
    return templates.TemplateResponse(
        request=request,
        name="release_form.html",
        context=_release_form_context(request, user, form=form, release=release),
    )


@app.post("/account/releases/{release_id}/edit", response_class=HTMLResponse)
async def release_edit(
    request: Request,
    release_id: int,
    title: str = Form(default=""),
    release_type: str = Form(default="single"),
    release_version: str = Form(default=""),
    genre: str = Form(default=""),
    metadata_language: str = Form(default="ru"),
    release_date: str = Form(default=""),
    is_explicit: str | None = Form(default=None),
    cover: UploadFile | None = File(default=None),
) -> Response:
    user = _require_user(request)
    if isinstance(user, RedirectResponse):
        return user
    release = get_release_for_user(int(user["id"]), release_id)
    if not release or not release["is_editable"]:
        return RedirectResponse(url="/account/releases", status_code=status.HTTP_303_SEE_OTHER)

    form = _release_form_values(
        title=_clean_line(title, 200),
        release_type=release_type.strip().lower(),
        release_version=_clean_line(release_version, 100),
        genre=_clean_line(genre, 100),
        metadata_language=metadata_language.strip().lower(),
        release_date=release_date.strip(),
        is_explicit=is_explicit == "on",
    )
    errors = _validate_release_form(form)
    if not release.get("cover_path") and (not cover or not cover.filename):
        errors["cover"] = "Загрузите квадратную обложку релиза."
    if errors:
        if cover:
            await cover.close()
        return templates.TemplateResponse(
            request=request,
            name="release_form.html",
            context=_release_form_context(
                request,
                user,
                form=form,
                errors=errors,
                release=release,
            ),
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )

    saved_cover: dict[str, Any] | None = None
    if cover and cover.filename:
        try:
            saved_cover = await save_cover_upload(cover)
        except UploadValidationError as exc:
            errors["cover"] = str(exc)
            return templates.TemplateResponse(
                request=request,
                name="release_form.html",
                context=_release_form_context(
                    request,
                    user,
                    form=form,
                    errors=errors,
                    release=release,
                ),
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )
    elif cover:
        await cover.close()

    old_cover = str(release.get("cover_path") or "")
    updated = update_release_details(
        int(user["id"]),
        release_id,
        title=form["title"],
        release_type=form["release_type"],
        release_version=form["release_version"],
        genre=form["genre"],
        metadata_language=form["metadata_language"],
        release_date=form["release_date"],
        is_explicit=bool(form["is_explicit"]),
        cover=saved_cover,
    )
    if not updated:
        if saved_cover:
            delete_upload(saved_cover["path"])
        return RedirectResponse(url="/account/releases", status_code=status.HTTP_303_SEE_OTHER)
    if saved_cover and old_cover and not old_cover.startswith("/"):
        delete_upload(old_cover)

    request.session["track_notice"] = "Основная информация релиза обновлена."
    return RedirectResponse(
        url=f"/account/releases/{release_id}/tracks",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.get("/account/releases/{release_id}/tracks", response_class=HTMLResponse)
async def release_tracks_page(
    request: Request,
    release_id: int,
    edit: int | None = Query(default=None),
) -> Response:
    user = _require_user(request)
    if isinstance(user, RedirectResponse):
        return user
    release = get_release_for_user(int(user["id"]), release_id)
    if not release:
        return RedirectResponse(url="/account/releases", status_code=status.HTTP_303_SEE_OTHER)
    if not release["is_editable"]:
        return RedirectResponse(
            url=f"/account/releases/{release_id}/review",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    editing_track = None
    track_form = None
    if edit is not None:
        editing_track = get_track_for_user(int(user["id"]), release_id, edit)
        if editing_track:
            track_form = _track_form_values(
                title=str(editing_track["title"]),
                artists=str(editing_track["artists"]),
                version=str(editing_track.get("version") or ""),
                lyrics=str(editing_track.get("lyrics") or ""),
                language=str(editing_track.get("language") or "ru"),
                lyricist=str(editing_track["lyricist"]),
                composer=str(editing_track["composer"]),
                is_explicit=bool(editing_track.get("is_explicit")),
                tiktok_start=format_timecode(editing_track.get("tiktok_start_seconds")),
                tiktok_end=format_timecode(editing_track.get("tiktok_end_seconds")),
            )

    notice = request.session.pop("track_notice", None)
    return templates.TemplateResponse(
        request=request,
        name="release_tracks.html",
        context=_tracks_context(
            request,
            user,
            release,
            track_form=track_form,
            editing_track=editing_track,
            notice=notice,
        ),
    )


@app.post("/account/releases/{release_id}/tracks", response_class=HTMLResponse)
async def release_track_create(
    request: Request,
    release_id: int,
    title: str = Form(default=""),
    artists: str = Form(default=""),
    version: str = Form(default=""),
    lyrics: str = Form(default=""),
    language: str = Form(default="ru"),
    lyricist: str = Form(default=""),
    composer: str = Form(default=""),
    is_explicit: str | None = Form(default=None),
    tiktok_start: str = Form(default="00:00"),
    tiktok_end: str = Form(default="01:00"),
    audio: UploadFile | None = File(default=None),
) -> Response:
    user = _require_user(request)
    if isinstance(user, RedirectResponse):
        return user
    release = get_release_for_user(int(user["id"]), release_id)
    if not release or not release["is_editable"]:
        return RedirectResponse(url="/account/releases", status_code=status.HTTP_303_SEE_OTHER)

    form = _track_form_values(
        title=_clean_line(title, 220),
        artists=_clean_line(artists, 240),
        version=_clean_line(version, 100),
        lyrics=lyrics.strip(),
        language=language.strip().lower(),
        lyricist=_clean_line(lyricist, 240),
        composer=_clean_line(composer, 240),
        is_explicit=is_explicit == "on",
        tiktok_start=tiktok_start.strip(),
        tiktok_end=tiktok_end.strip(),
    )
    errors, start_seconds, end_seconds = _validate_track_form(form)
    if not audio or not audio.filename:
        errors["audio"] = "Загрузите аудиофайл в формате WAV или FLAC."
    if errors:
        if audio:
            await audio.close()
        return templates.TemplateResponse(
            request=request,
            name="release_tracks.html",
            context=_tracks_context(
                request,
                user,
                release,
                track_form=form,
                track_errors=errors,
            ),
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )

    saved_audio: dict[str, Any] | None = None
    try:
        saved_audio = await save_audio_upload(audio)
    except UploadValidationError as exc:
        errors["audio"] = str(exc)
    if errors or not saved_audio:
        return templates.TemplateResponse(
            request=request,
            name="release_tracks.html",
            context=_tracks_context(
                request,
                user,
                release,
                track_form=form,
                track_errors=errors,
            ),
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )

    track = create_release_track(
        int(user["id"]),
        release_id,
        title=form["title"],
        artists=form["artists"],
        version=form["version"],
        lyrics=form["lyrics"],
        language=form["language"],
        lyricist=form["lyricist"],
        composer=form["composer"],
        is_explicit=bool(form["is_explicit"]),
        audio=saved_audio,
        tiktok_start_seconds=start_seconds,
        tiktok_end_seconds=end_seconds,
    )
    if not track:
        delete_upload(saved_audio["path"])
        return RedirectResponse(url="/account/releases", status_code=status.HTTP_303_SEE_OTHER)

    request.session["track_notice"] = "Трек добавлен в релиз."
    return RedirectResponse(
        url=f"/account/releases/{release_id}/tracks",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.post("/account/releases/{release_id}/tracks/{track_id}", response_class=HTMLResponse)
async def release_track_update(
    request: Request,
    release_id: int,
    track_id: int,
    title: str = Form(default=""),
    artists: str = Form(default=""),
    version: str = Form(default=""),
    lyrics: str = Form(default=""),
    language: str = Form(default="ru"),
    lyricist: str = Form(default=""),
    composer: str = Form(default=""),
    is_explicit: str | None = Form(default=None),
    tiktok_start: str = Form(default="00:00"),
    tiktok_end: str = Form(default="01:00"),
    audio: UploadFile | None = File(default=None),
) -> Response:
    user = _require_user(request)
    if isinstance(user, RedirectResponse):
        return user
    release = get_release_for_user(int(user["id"]), release_id)
    existing_track = get_track_for_user(int(user["id"]), release_id, track_id)
    if not release or not release["is_editable"] or not existing_track:
        return RedirectResponse(url="/account/releases", status_code=status.HTTP_303_SEE_OTHER)

    form = _track_form_values(
        title=_clean_line(title, 220),
        artists=_clean_line(artists, 240),
        version=_clean_line(version, 100),
        lyrics=lyrics.strip(),
        language=language.strip().lower(),
        lyricist=_clean_line(lyricist, 240),
        composer=_clean_line(composer, 240),
        is_explicit=is_explicit == "on",
        tiktok_start=tiktok_start.strip(),
        tiktok_end=tiktok_end.strip(),
    )
    errors, start_seconds, end_seconds = _validate_track_form(form)
    if errors:
        if audio:
            await audio.close()
        return templates.TemplateResponse(
            request=request,
            name="release_tracks.html",
            context=_tracks_context(
                request,
                user,
                release,
                track_form=form,
                track_errors=errors,
                editing_track=existing_track,
            ),
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )

    saved_audio: dict[str, Any] | None = None
    if audio and audio.filename:
        try:
            saved_audio = await save_audio_upload(audio)
        except UploadValidationError as exc:
            errors["audio"] = str(exc)
            return templates.TemplateResponse(
                request=request,
                name="release_tracks.html",
                context=_tracks_context(
                    request,
                    user,
                    release,
                    track_form=form,
                    track_errors=errors,
                    editing_track=existing_track,
                ),
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )
    elif audio:
        await audio.close()

    old_audio = str(existing_track.get("audio_path") or "")
    updated = update_release_track(
        int(user["id"]),
        release_id,
        track_id,
        title=form["title"],
        artists=form["artists"],
        version=form["version"],
        lyrics=form["lyrics"],
        language=form["language"],
        lyricist=form["lyricist"],
        composer=form["composer"],
        is_explicit=bool(form["is_explicit"]),
        audio=saved_audio,
        tiktok_start_seconds=start_seconds,
        tiktok_end_seconds=end_seconds,
    )
    if not updated:
        if saved_audio:
            delete_upload(saved_audio["path"])
        return RedirectResponse(url="/account/releases", status_code=status.HTTP_303_SEE_OTHER)
    if saved_audio and old_audio:
        delete_upload(old_audio)

    request.session["track_notice"] = "Данные трека обновлены."
    return RedirectResponse(
        url=f"/account/releases/{release_id}/tracks",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.post("/account/releases/{release_id}/tracks/{track_id}/delete")
async def release_track_delete(request: Request, release_id: int, track_id: int) -> Response:
    user = _require_user(request)
    if isinstance(user, RedirectResponse):
        return user
    deleted = delete_release_track(int(user["id"]), release_id, track_id)
    if deleted:
        delete_upload(str(deleted.get("audio_path") or ""))
        request.session["track_notice"] = "Трек удалён из релиза."
    return RedirectResponse(
        url=f"/account/releases/{release_id}/tracks",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.get("/account/releases/{release_id}/review", response_class=HTMLResponse)
async def release_review_page(request: Request, release_id: int) -> Response:
    user = _require_user(request)
    if isinstance(user, RedirectResponse):
        return user
    release = get_release_for_user(int(user["id"]), release_id)
    if not release:
        return RedirectResponse(url="/account/releases", status_code=status.HTTP_303_SEE_OTHER)
    tracks = list_release_tracks(int(user["id"]), release_id)
    notice = request.session.pop("review_notice", None)
    return templates.TemplateResponse(
        request=request,
        name="release_review.html",
        context=account_context(
            request,
            user,
            active_section="releases",
            release=release,
            tracks=tracks,
            step=3,
            notice=notice,
        ),
    )


@app.post("/account/releases/{release_id}/submit", response_class=HTMLResponse)
async def release_submit(
    request: Request,
    release_id: int,
    moderator_comment: str = Form(default=""),
    release_soon: str | None = Form(default=None),
) -> Response:
    user = _require_user(request)
    if isinstance(user, RedirectResponse):
        return user
    comment = moderator_comment.strip()
    if len(comment) > 2000:
        request.session["review_notice"] = "Комментарий должен быть не длиннее 2000 символов."
        return RedirectResponse(
            url=f"/account/releases/{release_id}/review",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    success, message = submit_release_for_moderation(
        int(user["id"]),
        release_id,
        moderator_comment=comment,
        release_soon=release_soon == "on",
    )
    if not success:
        request.session["review_notice"] = message
        return RedirectResponse(
            url=f"/account/releases/{release_id}/review",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    queue_new_release(release_id)
    request.session["releases_notice"] = message
    return RedirectResponse(url="/account/releases", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/account/releases/{release_id}/delete")
async def release_delete(request: Request, release_id: int) -> Response:
    user = _require_user(request)
    if isinstance(user, RedirectResponse):
        return user
    deleted = delete_release(int(user["id"]), release_id)
    if deleted:
        cover_path = str(deleted.get("cover_path") or "")
        if cover_path and not cover_path.startswith("/"):
            delete_upload(cover_path)
        for audio_path in deleted.get("audio_paths", []):
            delete_upload(str(audio_path))
        request.session["releases_notice"] = "Черновик удалён."
    return RedirectResponse(url="/account/releases", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/account/releases/{release_id}/cover")
async def release_cover(request: Request, release_id: int) -> Response:
    user = _require_user(request)
    if isinstance(user, RedirectResponse):
        return user
    release = get_release_for_user(int(user["id"]), release_id)
    if not release or not release.get("cover_path"):
        raise HTTPException(status_code=404)
    cover_path = str(release["cover_path"])
    if cover_path.startswith("/"):
        return RedirectResponse(url=cover_path, status_code=status.HTTP_307_TEMPORARY_REDIRECT)
    path = resolve_upload(cover_path)
    if not path:
        raise HTTPException(status_code=404)
    return FileResponse(path, headers={"Cache-Control": "private, max-age=3600"})


@app.get("/account/releases/{release_id}/tracks/{track_id}/audio")
async def release_track_audio(request: Request, release_id: int, track_id: int) -> Response:
    user = _require_user(request)
    if isinstance(user, RedirectResponse):
        return user
    track = get_track_for_user(int(user["id"]), release_id, track_id)
    if not track:
        raise HTTPException(status_code=404)
    path = resolve_upload(str(track.get("audio_path") or ""))
    if not path:
        raise HTTPException(status_code=404)
    media_type = "audio/flac" if path.suffix.lower() == ".flac" else "audio/wav"
    return FileResponse(
        path,
        media_type=media_type,
        filename=str(track.get("audio_filename") or path.name),
        content_disposition_type="inline",
        headers={"Cache-Control": "private, max-age=900"},
    )


@app.get("/account/support", response_class=HTMLResponse)
async def support_page(request: Request) -> Response:
    user = _require_user(request)
    if isinstance(user, RedirectResponse):
        return user
    return templates.TemplateResponse(
        request=request,
        name="support.html",
        context=account_context(request, user, active_section="support"),
    )


@app.post("/account/support/tickets", response_class=HTMLResponse)
async def support_ticket_create(
    request: Request,
    subject: str = Form(default=""),
    category: str = Form(default="Общий вопрос"),
    message: str = Form(default=""),
) -> Response:
    user = _require_user(request)
    if isinstance(user, RedirectResponse):
        return user
    values = {
        "subject": _clean_line(subject, 140),
        "category": _clean_line(category, 60),
        "message": _clean_multiline(message, 4200),
    }
    errors: dict[str, str] = {}
    if len(values["subject"]) < 3:
        errors["subject"] = "Укажите тему минимум из 3 символов."
    elif len(values["subject"]) > 120:
        errors["subject"] = "Тема должна быть не длиннее 120 символов."
    if values["category"] not in SUPPORT_CATEGORIES:
        errors["category"] = "Выберите категорию из списка."
    if len(values["message"]) < 10:
        errors["message"] = "Опишите вопрос минимум в 10 символах."
    elif len(values["message"]) > 4000:
        errors["message"] = "Сообщение должно быть не длиннее 4000 символов."
    if errors:
        return templates.TemplateResponse(
            request=request,
            name="support.html",
            context=support_page_context(
                request,
                user,
                form_values=values,
                form_errors=errors,
                error="Проверьте поля новой заявки.",
            ),
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )
    ticket_id = create_support_ticket(
        int(user["id"]),
        subject=values["subject"],
        category=values["category"],
        message=values["message"],
    )
    queue_new_support_ticket(ticket_id)
    request.session["support_notice"] = "Заявка создана. Ответ администратора появится в этом диалоге."
    return RedirectResponse(
        url=f"/account/support?ticket={ticket_id}#conversation",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.post("/account/support/tickets/{ticket_id}/messages")
async def support_ticket_message(
    request: Request,
    ticket_id: int,
    message: str = Form(default=""),
) -> Response:
    user = _require_user(request)
    if isinstance(user, RedirectResponse):
        return user
    clean_message = _clean_multiline(message, 4200)
    if len(clean_message) < 2:
        request.session["support_error"] = "Введите сообщение минимум из 2 символов."
    elif len(clean_message) > 4000:
        request.session["support_error"] = "Сообщение должно быть не длиннее 4000 символов."
    else:
        ok, result_message = add_user_support_message(int(user["id"]), ticket_id, clean_message)
        if ok:
            queue_new_support_ticket(ticket_id, new_message=True)
        request.session["support_notice" if ok else "support_error"] = result_message
    return RedirectResponse(
        url=f"/account/support?ticket={ticket_id}#conversation",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.post("/account/support/tickets/{ticket_id}/close")
async def support_ticket_close(request: Request, ticket_id: int) -> Response:
    user = _require_user(request)
    if isinstance(user, RedirectResponse):
        return user
    if close_user_support_ticket(int(user["id"]), ticket_id):
        request.session["support_notice"] = "Заявка закрыта. История переписки сохранена."
    else:
        request.session["support_error"] = "Не удалось закрыть заявку."
    return RedirectResponse(
        url=f"/account/support?ticket={ticket_id}#conversation",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.get("/account/pitching", response_class=HTMLResponse)
async def pitching_page(request: Request) -> Response:
    user = _require_user(request)
    if isinstance(user, RedirectResponse):
        return user
    return templates.TemplateResponse(
        request=request,
        name="pitching.html",
        context=account_context(
            request,
            user,
            active_section="pitching",
            pitching_resources=list_pitching_resources(),
        ),
    )


@app.post("/account/pitching", response_class=HTMLResponse)
async def pitching_request_create(request: Request) -> Response:
    user = _require_user(request)
    if isinstance(user, RedirectResponse):
        return user
    return RedirectResponse(url="/account/pitching", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/account/rules", response_class=HTMLResponse)
async def release_rules_page(request: Request) -> Response:
    user = _require_user(request)
    if isinstance(user, RedirectResponse):
        return user
    return templates.TemplateResponse(
        request=request,
        name="release_rules.html",
        context=account_context(
            request,
            user,
            active_section="rules",
            rule_sections=list_release_rule_sections(),
        ),
    )


@app.get("/account/statistics", response_class=HTMLResponse)
async def account_statistics(
    request: Request,
    period: str = Query(default="30"),
) -> Response:
    user = _require_user(request)
    if isinstance(user, RedirectResponse):
        return user
    return templates.TemplateResponse(
        request=request,
        name="statistics.html",
        context=account_context(
            request,
            user,
            active_section="statistics",
            statistics=get_statistics_page_data(int(user["id"]), period),
        ),
    )


@app.get("/account/balance", response_class=HTMLResponse)
async def account_balance(
    request: Request,
    operation_status: str = Query(default="all", alias="status"),
) -> Response:
    user = _require_user(request)
    if isinstance(user, RedirectResponse):
        return user
    return templates.TemplateResponse(
        request=request,
        name="balance.html",
        context=account_context(
            request,
            user,
            active_section="balance",
            balance_data=get_balance_page_data(int(user["id"]), operation_status),
            notice=request.session.pop("balance_notice", None),
            error=request.session.pop("balance_error", None),
        ),
    )


@app.post("/account/balance/payout")
async def account_balance_payout(
    request: Request,
    amount: str = Form(default=""),
    method: str = Form(default="telegram"),
    details: str = Form(default=""),
) -> Response:
    user = _require_user(request)
    if isinstance(user, RedirectResponse):
        return user
    try:
        amount_value = int(amount.strip())
    except ValueError:
        amount_value = 0
    ok, message = create_payout_request(
        int(user["id"]),
        amount=amount_value,
        method=method.strip(),
        details=_clean_multiline(details, 900),
    )
    if ok:
        queue_new_payout(int(user["id"]))
    request.session["balance_notice" if ok else "balance_error"] = message
    return RedirectResponse(url="/account/balance#payout", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/account/notifications", response_class=HTMLResponse)
async def account_notifications(
    request: Request,
    page: int = Query(default=1, ge=1),
) -> Response:
    user = _require_user(request)
    if isinstance(user, RedirectResponse):
        return user
    return templates.TemplateResponse(
        request=request,
        name="notifications.html",
        context=account_context(
            request,
            user,
            active_section="notifications",
            notifications=list_notifications(int(user["id"]), page=page),
        ),
    )


@app.post("/account/notifications/read")
async def account_notifications_read(request: Request) -> Response:
    user = _require_user(request)
    if isinstance(user, RedirectResponse):
        return user
    mark_notifications_read(int(user["id"]))
    return RedirectResponse(url="/account/notifications", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/admin", response_class=HTMLResponse)
async def admin_dashboard(request: Request) -> Response:
    user = _require_admin(request)
    if isinstance(user, RedirectResponse):
        return user
    return templates.TemplateResponse(
        request=request,
        name="admin_dashboard.html",
        context=admin_context(
            request,
            user,
            active_admin="overview",
            overview=get_admin_overview(),
            notice=request.session.pop("admin_notice", None),
            error=request.session.pop("admin_error", None),
        ),
    )


@app.get("/admin/releases", response_class=HTMLResponse)
async def admin_releases_page(
    request: Request,
    q: str = Query(default=""),
    release_status: str = Query(default="all", alias="status"),
    sort: str = Query(default="new"),
    page: int = Query(default=1, ge=1),
) -> Response:
    user = _require_admin(request)
    if isinstance(user, RedirectResponse):
        return user
    return templates.TemplateResponse(
        request=request,
        name="admin_releases.html",
        context=admin_context(
            request,
            user,
            active_admin="releases",
            releases=list_admin_releases(query=q, status_filter=release_status, sort=sort, page=page),
            notice=request.session.pop("admin_releases_notice", None),
            error=request.session.pop("admin_releases_error", None),
        ),
    )


@app.get("/admin/moderation", response_class=HTMLResponse)
async def admin_moderation_page(request: Request, page: int = Query(default=1, ge=1)) -> Response:
    user = _require_admin(request)
    if isinstance(user, RedirectResponse):
        return user
    return templates.TemplateResponse(
        request=request,
        name="admin_moderation.html",
        context=admin_context(
            request,
            user,
            active_admin="moderation",
            releases=list_admin_releases(status_filter="moderation", sort="old", page=page, per_page=24),
            notice=request.session.pop("admin_moderation_notice", None),
            error=request.session.pop("admin_moderation_error", None),
        ),
    )


@app.get("/admin/releases/{release_id}", response_class=HTMLResponse)
async def admin_release_detail(request: Request, release_id: int) -> Response:
    user = _require_admin(request)
    if isinstance(user, RedirectResponse):
        return user
    release = get_admin_release(release_id)
    if not release:
        raise HTTPException(status_code=404, detail="Release not found")
    return templates.TemplateResponse(
        request=request,
        name="admin_release_detail.html",
        context=admin_context(
            request,
            user,
            active_admin="moderation" if release["status"] == "moderation" else "releases",
            release=release,
            notice=request.session.pop("admin_release_notice", None),
            error=request.session.pop("admin_release_error", None),
        ),
    )


@app.post("/admin/releases/{release_id}/moderate")
async def admin_release_moderate(
    request: Request,
    release_id: int,
    decision: str = Form(default="message"),
    comment: str = Form(default=""),
) -> Response:
    user = _require_admin(request)
    if isinstance(user, RedirectResponse):
        return user
    clean_comment = _clean_multiline(comment, 3200)
    ok, message = moderate_release(
        int(user["id"]), release_id, decision=decision, comment=clean_comment
    )
    if ok:
        status_map = {"approve": "accepted", "changes": "changes_required", "reject": "rejected"}
        if decision == "message":
            queue_release_message(release_id, clean_comment)
        elif decision in status_map:
            queue_release_status(release_id, status_map[decision], clean_comment)
    request.session["admin_release_notice" if ok else "admin_release_error"] = message
    return RedirectResponse(url=f"/admin/releases/{release_id}", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/admin/releases/{release_id}/upc")
async def admin_release_upc(
    request: Request,
    release_id: int,
    upc: str = Form(default=""),
) -> Response:
    user = _require_admin(request)
    if isinstance(user, RedirectResponse):
        return user
    clean_upc = "".join(ch for ch in upc if ch.isdigit())
    ok, message = set_release_upc(int(user["id"]), release_id, clean_upc)
    if ok:
        queue_release_upc(release_id, clean_upc)
    request.session["admin_release_notice" if ok else "admin_release_error"] = message
    return RedirectResponse(url=f"/admin/releases/{release_id}", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/admin/releases/{release_id}/listens")
async def admin_release_listens(
    request: Request,
    release_id: int,
    target: str = Form(default="0"),
) -> Response:
    user = _require_admin(request)
    if isinstance(user, RedirectResponse):
        return user
    try:
        target_value = int(target.strip())
    except ValueError:
        target_value = -1
    ok, message = set_release_listens(int(user["id"]), release_id, target_value)
    request.session["admin_release_notice" if ok else "admin_release_error"] = message
    referer = request.headers.get("referer") or f"/admin/releases/{release_id}"
    return RedirectResponse(url=referer, status_code=status.HTTP_303_SEE_OTHER)


@app.post("/admin/tracks/{track_id}/listens")
async def admin_track_listens(
    request: Request,
    track_id: int,
    target: str = Form(default="0"),
    release_id: int = Form(default=0),
) -> Response:
    user = _require_admin(request)
    if isinstance(user, RedirectResponse):
        return user
    try:
        target_value = int(target.strip())
    except ValueError:
        target_value = -1
    ok, message = set_track_listens(int(user["id"]), track_id, target_value)
    request.session["admin_release_notice" if ok else "admin_release_error"] = message
    return RedirectResponse(url=f"/admin/releases/{release_id}#tracks", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/admin/releases/{release_id}/cover")
async def admin_release_cover(request: Request, release_id: int) -> Response:
    user = _require_admin(request)
    if isinstance(user, RedirectResponse):
        return user
    release = get_admin_release(release_id)
    if not release:
        raise HTTPException(status_code=404, detail="Release not found")
    path = resolve_upload(release.get("cover_path"))
    if not path:
        raise HTTPException(status_code=404, detail="Cover not found")
    return FileResponse(path, filename=str(release.get("cover_filename") or path.name))


@app.get("/admin/tracks/{track_id}/audio")
async def admin_track_audio(request: Request, track_id: int) -> Response:
    user = _require_admin(request)
    if isinstance(user, RedirectResponse):
        return user
    track = get_admin_track(track_id)
    if not track:
        raise HTTPException(status_code=404, detail="Track not found")
    path = resolve_upload(track.get("audio_path"))
    if not path:
        raise HTTPException(status_code=404, detail="Audio not found")
    return FileResponse(path, filename=str(track.get("audio_filename") or path.name))


@app.get("/admin/releases/{release_id}/tracks.zip")
async def admin_release_tracks_archive(request: Request, release_id: int) -> Response:
    user = _require_admin(request)
    if isinstance(user, RedirectResponse):
        return user
    release = get_admin_release(release_id)
    if not release:
        raise HTTPException(status_code=404, detail="Release not found")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for index, track in enumerate(release["tracks"], start=1):
            path = resolve_upload(track.get("audio_path"))
            if not path:
                continue
            safe_title = "".join(char if char.isalnum() or char in " _-" else "_" for char in str(track["title"]))[:80]
            archive.write(path, arcname=f"{index:02d} - {safe_title}{path.suffix.lower()}")
    return Response(
        content=buffer.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="release-{release_id}-tracks.zip"'},
    )


@app.get("/admin/releases/{release_id}/metadata.json")
async def admin_release_metadata_legacy(request: Request, release_id: int) -> Response:
    return RedirectResponse(url=f"/admin/releases/{release_id}/metadata.txt", status_code=status.HTTP_307_TEMPORARY_REDIRECT)


@app.get("/admin/releases/{release_id}/metadata.txt")
async def admin_release_metadata(request: Request, release_id: int) -> Response:
    user = _require_admin(request)
    if isinstance(user, RedirectResponse):
        return user
    release = get_admin_release(release_id)
    if not release:
        raise HTTPException(status_code=404, detail="Release not found")

    lines = [
        "RAMIX MUSIC — ДАННЫЕ РЕЛИЗА",
        "=" * 54,
        f"ID релиза: {release.get('id')}",
        f"Название: {release.get('title') or '—'}",
        f"Артист: {release.get('artist_name') or '—'}",
        f"Тип релиза: {release.get('release_type_label') or release.get('release_type') or '—'}",
        f"Версия: {release.get('release_version') or 'Оригинальная версия'}",
        f"Жанр: {release.get('genre') or '—'}",
        f"Язык метаданных: {release.get('metadata_language_label') or release.get('metadata_language') or '—'}",
        f"Дата выхода: {release.get('release_date_label') or release.get('release_date') or '—'}",
        f"Explicit: {'Да' if release.get('is_explicit') else 'Нет'}",
        f"UPC: {release.get('upc') or 'Не назначен'}",
        f"Статус: {release.get('status_meta', {}).get('label', release.get('status') or '—')}",
        "",
        "ПОЛЬЗОВАТЕЛЬ",
        "-" * 54,
        f"ID пользователя: {release.get('user_id')}",
        f"Имя: {release.get('user_name') or '—'}",
        f"Логин: @{release.get('username') or '—'}",
        f"Email: {release.get('user_email') or '—'}",
        "",
        "ТРЕКИ",
        "-" * 54,
    ]
    for index, track in enumerate(release.get("tracks") or [], start=1):
        lines.extend([
            f"{index}. {track.get('title') or 'Без названия'}",
            f"   Артисты: {track.get('artists') or '—'}",
            f"   Версия: {track.get('version') or 'Оригинальная'}",
            f"   Язык: {track.get('language_label') or track.get('language') or '—'}",
            f"   Автор слов: {track.get('lyricist') or '—'}",
            f"   Автор музыки: {track.get('composer') or '—'}",
            f"   Explicit: {'Да' if track.get('is_explicit') else 'Нет'}",
            f"   ISRC: {track.get('isrc') or 'Не назначен'}",
            f"   Аудиофайл: {track.get('audio_filename') or '—'}",
            f"   Отрывок: {track.get('tiktok_start_label') or '00:00'}–{track.get('tiktok_end_label') or '—'}",
            "",
        ])
    if not release.get("tracks"):
        lines.append("Треки не добавлены.")
    lines.extend([
        "",
        "КОММЕНТАРИИ",
        "-" * 54,
        f"Комментарий пользователя: {release.get('moderator_comment') or '—'}",
        f"Причина отклонения / правок: {release.get('rejection_reason') or '—'}",
    ])
    payload = "\ufeff" + "\n".join(str(line) for line in lines)
    return Response(
        content=payload.encode("utf-8"),
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="release-{release_id}-metadata.txt"'},
    )


@app.get("/admin/users", response_class=HTMLResponse)
async def admin_users_page(
    request: Request,
    q: str = Query(default=""),
    user_status: str = Query(default="all", alias="status"),
    page: int = Query(default=1, ge=1),
) -> Response:
    user = _require_admin(request)
    if isinstance(user, RedirectResponse):
        return user
    return templates.TemplateResponse(
        request=request,
        name="admin_users.html",
        context=admin_context(
            request,
            user,
            active_admin="users",
            users=list_admin_users(query=q, status_filter=user_status, page=page),
            notice=request.session.pop("admin_users_notice", None),
            error=request.session.pop("admin_users_error", None),
        ),
    )


@app.get("/admin/users/{user_id}", response_class=HTMLResponse)
async def admin_user_detail(request: Request, user_id: int) -> Response:
    user = _require_admin(request)
    if isinstance(user, RedirectResponse):
        return user
    profile = get_admin_user(user_id)
    if not profile:
        raise HTTPException(status_code=404, detail="User not found")
    return templates.TemplateResponse(
        request=request,
        name="admin_user_detail.html",
        context=admin_context(
            request,
            user,
            active_admin="users",
            profile=profile,
            notice=request.session.pop("admin_user_notice", None),
            error=request.session.pop("admin_user_error", None),
        ),
    )


@app.post("/admin/users/{user_id}/balance")
async def admin_user_balance_update(
    request: Request,
    user_id: int,
    amount: str = Form(default="0"),
    reason: str = Form(default=""),
) -> Response:
    user = _require_admin(request)
    if isinstance(user, RedirectResponse):
        return user
    try:
        amount_value = int(amount.strip())
    except ValueError:
        amount_value = 0
    clean_reason = _clean_line(reason, 180)
    ok, message = adjust_user_balance(
        int(user["id"]), user_id, amount=amount_value, reason=clean_reason
    )
    if ok:
        queue_balance_change(user_id, amount_value, clean_reason)
    request.session["admin_user_notice" if ok else "admin_user_error"] = message
    return RedirectResponse(url=f"/admin/users/{user_id}", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/admin/users/{user_id}/block")
async def admin_user_block_update(
    request: Request,
    user_id: int,
    blocked: int = Form(default=1),
    reason: str = Form(default=""),
) -> Response:
    user = _require_admin(request)
    if isinstance(user, RedirectResponse):
        return user
    clean_reason = _clean_multiline(reason, 700)
    ok, message = set_user_blocked(
        int(user["id"]), user_id, blocked=bool(blocked), reason=clean_reason
    )
    if ok:
        queue_account_access(user_id, blocked=bool(blocked), reason=clean_reason)
    request.session["admin_user_notice" if ok else "admin_user_error"] = message
    return RedirectResponse(url=f"/admin/users/{user_id}", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/admin/imports", response_class=HTMLResponse)
async def admin_imports_page(request: Request) -> Response:
    user = _require_admin(request)
    if isinstance(user, RedirectResponse):
        return user
    return templates.TemplateResponse(
        request=request,
        name="admin_imports.html",
        context=admin_context(
            request,
            user,
            active_admin="imports",
            imports=get_admin_imports(),
            notice=request.session.pop("admin_import_notice", None),
            error=request.session.pop("admin_import_error", None),
        ),
    )


@app.post("/admin/imports")
async def admin_imports_upload(
    request: Request,
    file: UploadFile | None = File(default=None),
    period_label: str = Form(default=""),
) -> Response:
    user = _require_admin(request)
    if isinstance(user, RedirectResponse):
        return user
    ok, message, _ = await process_import_file(
        int(user["id"]), file, period_label=_clean_line(period_label, 80)
    )
    request.session["admin_import_notice" if ok else "admin_import_error"] = message
    return RedirectResponse(url="/admin/imports", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/admin/imports/template.xlsx")
async def admin_import_template(request: Request) -> Response:
    user = _require_admin(request)
    if isinstance(user, RedirectResponse):
        return user
    return Response(
        content=build_import_template(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="ramix-import-template.xlsx"'},
    )


@app.get("/admin/imports/report.xlsx")
async def admin_quarter_report(request: Request) -> Response:
    user = _require_admin(request)
    if isinstance(user, RedirectResponse):
        return user
    return Response(
        content=build_quarter_report(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="ramix-quarter-report.xlsx"'},
    )


@app.get("/admin/imports/{import_id}/file")
async def admin_import_file(request: Request, import_id: int) -> Response:
    user = _require_admin(request)
    if isinstance(user, RedirectResponse):
        return user
    item = next((row for row in get_admin_imports() if int(row["id"]) == import_id), None)
    if not item:
        raise HTTPException(status_code=404, detail="Import not found")
    path = resolve_import_file(item.get("stored_filename"))
    if not path:
        raise HTTPException(status_code=404, detail="Import file not found")
    return FileResponse(path, filename=str(item.get("original_filename") or path.name))


@app.get("/admin/news", response_class=HTMLResponse)
async def admin_news_page(request: Request, edit: int | None = Query(default=None)) -> Response:
    user = _require_admin(request)
    if isinstance(user, RedirectResponse):
        return user
    items = list_service_news_admin()
    editing = next((item for item in items if int(item["id"]) == edit), None) if edit else None
    return templates.TemplateResponse(
        request=request,
        name="admin_news.html",
        context=admin_context(
            request,
            user,
            active_admin="news",
            news_items=items,
            editing_news=editing,
            notice=request.session.pop("admin_news_notice", None),
            error=request.session.pop("admin_news_error", None),
        ),
    )


@app.post("/admin/news")
async def admin_news_save(
    request: Request,
    news_id: str = Form(default=""),
    title: str = Form(default=""),
    body: str = Form(default=""),
    published_at: str = Form(default=""),
    sort_order: str = Form(default="0"),
    is_active: str | None = Form(default=None),
) -> Response:
    user = _require_admin(request)
    if isinstance(user, RedirectResponse):
        return user
    try:
        news_id_value = int(news_id) if news_id.strip() else None
    except ValueError:
        news_id_value = None
    try:
        sort_value = int(sort_order)
    except ValueError:
        sort_value = 0
    ok, message = save_service_news(
        int(user["id"]),
        news_id=news_id_value,
        title=title,
        body=body,
        published_at=published_at,
        sort_order=sort_value,
        is_active=is_active == "1",
    )
    request.session["admin_news_notice" if ok else "admin_news_error"] = message
    return RedirectResponse(url="/admin/news", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/admin/news/{news_id}/delete")
async def admin_news_delete(request: Request, news_id: int) -> Response:
    user = _require_admin(request)
    if isinstance(user, RedirectResponse):
        return user
    ok, message = delete_service_news(int(user["id"]), news_id)
    request.session["admin_news_notice" if ok else "admin_news_error"] = message
    return RedirectResponse(url="/admin/news", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/admin/payouts", response_class=HTMLResponse)
async def admin_payouts_page(
    request: Request,
    payout_status: str = Query(default="all", alias="status"),
) -> Response:
    user = _require_admin(request)
    if isinstance(user, RedirectResponse):
        return user
    return templates.TemplateResponse(
        request=request,
        name="admin_payouts.html",
        context=admin_context(
            request,
            user,
            active_admin="payouts",
            payouts=list_payout_requests(payout_status),
            notice=request.session.pop("admin_payout_notice", None),
            error=request.session.pop("admin_payout_error", None),
        ),
    )


@app.post("/admin/payouts/{payout_id}")
async def admin_payout_update(
    request: Request,
    payout_id: int,
    status_value: str = Form(default="processing"),
    admin_comment: str = Form(default=""),
) -> Response:
    user = _require_admin(request)
    if isinstance(user, RedirectResponse):
        return user
    clean_comment = _clean_multiline(admin_comment, 700)
    ok, message = process_payout_request(
        int(user["id"]), payout_id, status_value=status_value,
        admin_comment=clean_comment,
    )
    if ok:
        queue_payout_status(payout_id, status_value, clean_comment)
    request.session["admin_payout_notice" if ok else "admin_payout_error"] = message
    return RedirectResponse(url="/admin/payouts", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/admin/support", response_class=HTMLResponse)
async def admin_support_page(
    request: Request,
    support_status: str = Query(default="all", alias="status"),
) -> Response:
    user = _require_admin(request)
    if isinstance(user, RedirectResponse):
        return user
    return templates.TemplateResponse(
        request=request,
        name="admin_support.html",
        context=admin_context(
            request,
            user,
            active_admin="support",
            tickets=list_admin_support_summary(support_status),
            notice=request.session.pop("admin_support_notice", None),
            error=request.session.pop("admin_support_error", None),
        ),
    )


@app.get("/admin/support/{ticket_id}", response_class=HTMLResponse)
async def admin_support_detail(request: Request, ticket_id: int) -> Response:
    user = _require_admin(request)
    if isinstance(user, RedirectResponse):
        return user
    ticket = get_admin_support_ticket(ticket_id)
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")
    return templates.TemplateResponse(
        request=request,
        name="admin_support_detail.html",
        context=admin_context(
            request,
            user,
            active_admin="support",
            ticket=ticket,
            notice=request.session.pop("admin_support_notice", None),
            error=request.session.pop("admin_support_error", None),
        ),
    )


@app.post("/admin/support/{ticket_id}/reply")
async def admin_support_reply(
    request: Request,
    ticket_id: int,
    message: str = Form(default=""),
    ticket_status: str = Form(default="answered"),
) -> Response:
    user = _require_admin(request)
    if isinstance(user, RedirectResponse):
        return user
    clean_message = _clean_multiline(message, 4200)
    if len(clean_message) < 2:
        ok, result_message = False, "Введите ответ минимум из 2 символов."
    else:
        ok, result_message = admin_reply_support_ticket(
            int(user["id"]), ticket_id, body=clean_message, status_value=ticket_status
        )
        if ok:
            queue_support_reply(ticket_id, clean_message, ticket_status)
    request.session["admin_support_notice" if ok else "admin_support_error"] = result_message
    return RedirectResponse(url=f"/admin/support/{ticket_id}", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/admin/support/{ticket_id}/status")
async def admin_support_status(
    request: Request,
    ticket_id: int,
    ticket_status: str = Form(default="in_progress"),
) -> Response:
    user = _require_admin(request)
    if isinstance(user, RedirectResponse):
        return user
    ok = admin_update_support_ticket_status(ticket_id, ticket_status)
    if ok:
        queue_support_status(ticket_id, ticket_status)
    request.session["admin_support_notice" if ok else "admin_support_error"] = (
        "Статус заявки обновлён." if ok else "Не удалось изменить статус."
    )
    return RedirectResponse(url=f"/admin/support/{ticket_id}", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/admin/pitching", response_class=HTMLResponse)
async def admin_pitching_page(request: Request) -> Response:
    user = _require_admin(request)
    if isinstance(user, RedirectResponse):
        return user
    return RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/admin/pitching/{pitching_request_id}", response_class=HTMLResponse)
async def admin_pitching_detail(request: Request, pitching_request_id: int) -> Response:
    user = _require_admin(request)
    if isinstance(user, RedirectResponse):
        return user
    return RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/admin/pitching/{pitching_request_id}")
async def admin_pitching_update(request: Request, pitching_request_id: int) -> Response:
    user = _require_admin(request)
    if isinstance(user, RedirectResponse):
        return user
    return RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/account/{section:path}", response_class=HTMLResponse)
async def account_section(request: Request, section: str) -> Response:
    user = _require_user(request)
    if isinstance(user, RedirectResponse):
        return user
    sections = {
        "statistics": ("Статистика", "statistics"),
        "balance": ("Баланс", "balance"),
    }
    title, active_section = sections.get(section, ("Раздел платформы", "home"))
    return templates.TemplateResponse(
        request=request,
        name="account_placeholder.html",
        context=account_context(
            request,
            user,
            active_section=active_section,
            section_title=title,
        ),
    )


@app.post("/logout")
async def logout(request: Request) -> RedirectResponse:
    request.session.clear()
    return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/privacy", response_class=HTMLResponse)
async def privacy(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name="privacy.html",
        context=template_context(request),
    )


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "project": "RAMIX MUSIC",
        "telegram": telegram_runtime.status(),
    }
