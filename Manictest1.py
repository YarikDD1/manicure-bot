"""
Telegram Nail Salon Bot — обновлённый:
- Надёжная загрузка фото (photo + document) с fallback через bot.get_file + bot.download_file + HTTP fallback
- Управление мастерами: добавить/удалить мастера (админ)
- При записи: выбор мастера; в списке мастеров показывается имя и телефон (если заполнены)
- Авто-миграция client_username (с бэкапом)
- Reviews, portfolio, reminders, FastAPI admin (авто-порт)
"""

import asyncio
import logging
import os
import shutil
import uuid
import traceback
import sqlite3
from datetime import datetime, timedelta
from typing import Optional, List, Tuple

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, StateFilter
from aiogram.types import (
    Message,
    CallbackQuery,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    InputFile,
)
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram import Router

from sqlmodel import Field, SQLModel, select
from sqlmodel.ext.asyncio.session import AsyncSession
from sqlalchemy.ext.asyncio import create_async_engine
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

# ================= CONFIG =================
PROJECT_FOLDER = "Manictest1"
DB_FILE = os.path.join(PROJECT_FOLDER, "Manictest1.db")
UPLOAD_PATH = os.path.join(PROJECT_FOLDER, "uploads")

API_TOKEN = os.getenv("8533781697:AAG4D_1Wk7ripyb7e6jvuRRCjHmd9IpxR_c")
_admin_raw = os.getenv("580493054", "")
ADMIN_IDS = [int(x) for x in _admin_raw.split(",") if x.strip().isdigit()]
MASTER_IDS = ["580493054"]  # <-- начальный список мастеров (можно добавлять/удалять в админ-панели)

TG_GROUP_URL = "https://t.me/testworkmanic"  # <-- ссылка на группу с работами/отзывами

WEB_HOST = "127.0.0.1"
WEB_PORT = 8000
REMINDER_HOURS = 24
PAST_STATUS = "past"

os.makedirs(PROJECT_FOLDER, exist_ok=True)
os.makedirs(UPLOAD_PATH, exist_ok=True)
# ==========================================

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ===== Aiogram init (v3) =====
bot = Bot(token=API_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
router = Router()
dp.include_router(router)

# ===== Models =====
class User(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    telegram_id: int
    name: Optional[str] = None
    phone: Optional[str] = None
    is_master: bool = False
    is_admin: bool = False


class Booking(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int
    chat_id: Optional[int] = None  # ✅ ВАЖНО
    client_name: str
    client_username: Optional[str] = None
    phone: str
    date: str  # YYYY-MM-DD
    time: str  # HH:MM
    status: str = Field(default="pending")  # pending / confirmed / cancelled / past
    master_id: Optional[int] = None
    created_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())


class Photo(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int
    file_path: str
    caption: Optional[str] = None
    uploaded_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())


class Review(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int
    user_name: Optional[str] = None
    text: str
    created_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())


# DB engine
DATABASE_URL = f"sqlite+aiosqlite:///{DB_FILE}"
engine = create_async_engine(DATABASE_URL, echo=False, future=True)


# ===== Helpers =====
def is_admin(tg_id: int) -> bool:
    return tg_id in ADMIN_IDS


def is_master_id(tg_id: int) -> bool:
    return tg_id in MASTER_IDS


def generate_dates(num_days=30):
    today = datetime.now().date()
    return [(today + timedelta(days=i)).isoformat() for i in range(num_days)]


def default_time_slots():
    return ["10:00", "11:00", "12:00", "13:00", "15:00", "16:00", "17:00"]


def build_inline_kb_from_pairs(pairs: List[Tuple[str, str]], row_width: int = 3) -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = []
    row: List[InlineKeyboardButton] = []
    for i, (text, cb) in enumerate(pairs):
        row.append(InlineKeyboardButton(text=text, callback_data=cb))
        if (i + 1) % row_width == 0:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_booking_kb(booking_id: int) -> InlineKeyboardMarkup:
    pairs = [("Подтвердить", f"admin_confirm:{booking_id}"), ("Отменить", f"admin_cancel:{booking_id}")]
    return build_inline_kb_from_pairs(pairs, row_width=2)


def build_reply_kb(rows: List[List[str]], resize: bool = True) -> ReplyKeyboardMarkup:
    keyboard: List[List[KeyboardButton]] = []
    for row in rows:
        kb_row: List[KeyboardButton] = []
        for item in row:
            kb_row.append(KeyboardButton(text=str(item)))
        keyboard.append(kb_row)
    return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=resize)


def format_date_rus(iso_date: str) -> str:
    try:
        d = datetime.fromisoformat(iso_date).date()
        return d.strftime("%d.%m.%Y")
    except Exception:
        return iso_date


# ===== DB migration helper (synchronous, with backup) =====
def ensure_client_username_column():
    if not os.path.exists(DB_FILE):
        logger.info("DB not found, skipping migration.")
        return
    try:
        conn = sqlite3.connect(DB_FILE)
        cur = conn.cursor()
        cur.execute("PRAGMA table_info(booking);")
        cols = [r[1] for r in cur.fetchall()]
        logger.info("booking columns: %s", cols)
        if "client_username" in cols:
            logger.info("client_username already present.")
        else:
            bak = DB_FILE + ".bak." + datetime.now().strftime("%Y%m%d%H%M%S")
            shutil.copy2(DB_FILE, bak)
            logger.info("Backup created: %s", bak)
            try:
                cur.execute("ALTER TABLE booking ADD COLUMN client_username TEXT;")
                conn.commit()
                logger.info("Added column client_username.")
            except Exception as e:
                logger.exception("ALTER TABLE failed: %s", e)
                conn.close()
                shutil.copy2(bak, DB_FILE)
                logger.info("Restored DB from backup.")
                raise
        conn.close()
    except Exception as e:
        logger.exception("Error during ensure_client_username_column: %s", e)


# ===== Async DB helpers =====
async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    logger.info("DB tables created (if not exists).")
    ensure_client_username_column()


async def get_booked_times_for_date(date_iso: str, master_id: Optional[int] = None) -> List[str]:
    async with AsyncSession(engine) as session:
        q = select(Booking.time).where(Booking.date == date_iso).where(Booking.status != "cancelled")
        if master_id:
            q = q.where(Booking.master_id == master_id)
        result = await session.exec(q)
        rows = result.all()
        return [r[0] if isinstance(r, tuple) else r for r in rows]


async def is_time_slot_free(date_iso: str, time_slot: str, master_id: Optional[int] = None) -> bool:
    async with AsyncSession(engine) as session:
        q = select(Booking).where(Booking.date == date_iso).where(Booking.time == time_slot).where(Booking.status != "cancelled")
        if master_id:
            q = q.where(Booking.master_id == master_id)
        booking = (await session.exec(q)).one_or_none()
        return booking is None


async def get_masters_list() -> List[Tuple[int, str, Optional[str]]]:
    """
    Return list of tuples (telegram_id, display_name, phone)
    Sources:
      - MASTER_IDS global list
      - Users table where is_master=True
    Dedupe by telegram_id.
    """
    masters = {}
    # from MASTER_IDS (without details)
    for mid in MASTER_IDS:
        masters[mid] = {"name": f"Мастер {mid}", "phone": None}
    # from DB
    async with AsyncSession(engine) as session:
        result = await session.exec(select(User).where(User.is_master == True))
        rows = result.all()
        for u in rows:
            masters[u.telegram_id] = {"name": u.name or f"@{u.telegram_id}", "phone": u.phone}
    # format list
    return [(mid, masters[mid]["name"], masters[mid]["phone"]) for mid in masters.keys()]


async def mark_past_bookings():
    now = datetime.utcnow()
    async with AsyncSession(engine) as session:
        result = await session.exec(select(Booking).where(Booking.status.notin_(["cancelled", PAST_STATUS])))
        rows = result.all()
        changed = False
        for b in rows:
            try:
                dt = datetime.fromisoformat(f"{b.date}T{b.time}:00")
            except Exception:
                try:
                    dt = datetime.strptime(f"{b.date} {b.time}", "%Y-%m-%d %H:%M")
                except Exception:
                    continue
            if dt < now:
                b.status = PAST_STATUS
                session.add(b)
                changed = True
        if changed:
            await session.commit()


# ===== FSM States =====
class BookingStates(StatesGroup):
    waiting_for_name = State()
    waiting_for_phone = State()
    waiting_for_master = State()
    waiting_for_date = State()
    waiting_for_time = State()


class PhotoStates(StatesGroup):
    waiting_for_photo = State()
    waiting_for_caption = State()


class ReviewStates(StatesGroup):
    waiting_for_text = State()


class MasterManageStates(StatesGroup):
    waiting_for_new_master_id = State()
    waiting_for_new_master_name = State()
    waiting_for_new_master_phone = State()


# ===== Bot handlers =====
WELCOME_TEXT = (
    "💅 Добро пожаловать в студию маникюра!\n\n"
    "Мы делаем маникюр, покрытие и дизайн — аккуратно и красиво. Работает запись.\n"
    "Нажмите «📅 Записаться», чтобы начать."
)

# start
@router.message(Command(commands=["start", "help"]))
async def cmd_start(message: Message):
    rows = [
        ["📅 Записаться"],
        ["📁 Портфолио", "✍️ Оставить отзыв"],
        ["Отзывы", "👤 Мои записи"],
    ]
    # check DB for user role as well
    is_m = message.from_user.id in MASTER_IDS
    is_a = message.from_user.id in ADMIN_IDS
    async with AsyncSession(engine) as session:
        result = await session.exec(select(User).where(User.telegram_id == message.from_user.id))
        u = result.one_or_none()
        if u:
            if u.is_master:
                is_m = True
            if u.is_admin:
                is_a = True

    if is_m:
        rows.append(["🔧 Панель мастера"])
    if is_a:
        rows.append(["🛠️ Админ-панель"])
    kb = build_reply_kb(rows)
    await message.answer(WELCOME_TEXT, reply_markup=kb)
    inline = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Группа с работами и отзывами", url=TG_GROUP_URL)]])
    await message.answer("Полезные ссылки:", reply_markup=inline)


# Booking flow: choose master option after phone
@router.message(F.text == "📅 Записаться")
async def start_booking(message: Message, state: FSMContext):
    await message.answer("Как вас зовут?")
    await state.set_state(BookingStates.waiting_for_name)


@router.message(StateFilter(BookingStates.waiting_for_name))
async def booking_name(message: Message, state: FSMContext):
    await state.update_data(client_name=message.text.strip())
    await message.answer("Телефон (пример: +79171234567):")
    await state.set_state(BookingStates.waiting_for_phone)


@router.message(StateFilter(BookingStates.waiting_for_phone))
async def booking_phone(message: Message, state: FSMContext):
    phone = message.text.strip()
    if not (phone.startswith("+") and len(phone) >= 9):
        await message.answer("Пожалуйста, укажите телефон в формате +71234567890")
        return
    await state.update_data(phone=phone)
    # present masters list + option "К любому мастеру"
    masters = await get_masters_list()
    pairs = [("К любому мастеру", "book_master:0")]
    for mid, name, phone_m in masters:
        label = f"{name}" + (f" ({phone_m})" if phone_m else "")
        pairs.append((label, f"book_master:{mid}"))
    kb = build_inline_kb_from_pairs(pairs, row_width=1)
    await message.answer("Выберите мастера или 'К любому мастеру':", reply_markup=kb)
    await state.set_state(BookingStates.waiting_for_master)


@router.callback_query(StateFilter(BookingStates.waiting_for_master), lambda c: c.data and c.data.startswith("book_master:"))
async def booking_master_chosen(callback: CallbackQuery, state: FSMContext):
    mid = int(callback.data.split(":", 1)[1])
    await state.update_data(master_id=mid if mid != 0 else None)
    # proceed to date selection
    dates = generate_dates(30)
    pairs = [(d, f"book_date:{d}") for d in dates[:14]]
    kb = build_inline_kb_from_pairs(pairs, row_width=3)
    await bot.send_message(callback.from_user.id, "Выберите дату:", reply_markup=kb)
    await state.set_state(BookingStates.waiting_for_date)
    await callback.answer()


@router.callback_query(StateFilter(BookingStates.waiting_for_date), lambda c: c.data and c.data.startswith("book_date:"))
async def booking_date_chosen(callback: CallbackQuery, state: FSMContext):
    date = callback.data.split(":", 1)[1]
    await state.update_data(date=date)
    data = await state.get_data()
    master_id = data.get("master_id")
    booked = await get_booked_times_for_date(date, master_id=master_id)
    slots = [t for t in default_time_slots() if t not in booked]
    if not slots:
        await bot.send_message(callback.from_user.id, f"На {format_date_rus(date)} нет свободных слотов для выбранного мастера. Выберите другую дату или мастера.")
        await callback.answer()
        return
    pairs = [(t, f"book_time:{t}") for t in slots]
    kb = build_inline_kb_from_pairs(pairs, row_width=3)
    await bot.send_message(callback.from_user.id, f"Вы выбрали {format_date_rus(date)}. Выберите время:", reply_markup=kb)
    await state.set_state(BookingStates.waiting_for_time)
    await callback.answer()


@router.callback_query(StateFilter(BookingStates.waiting_for_time), lambda c: c.data and c.data.startswith("book_time:"))
async def booking_time_chosen(callback: CallbackQuery, state: FSMContext):
    time_chosen = callback.data.split(":", 1)[1]
    data = await state.get_data()
    client_name = data.get("client_name")
    phone = data.get("phone")
    date = data.get("date")
    master_id = data.get("master_id")
    free = await is_time_slot_free(date, time_chosen, master_id=master_id)
    if not free:
        await bot.send_message(callback.from_user.id, f"Время {time_chosen} на {format_date_rus(date)} уже занято для выбранного мастера.")
        await callback.answer()
        return
    client_username = callback.from_user.username or callback.from_user.full_name or None
    async with AsyncSession(engine) as session:
        booking = Booking(
            user_id=callback.from_user.id,
            chat_id=callback.message.chat.id,  # ← ВСТАВЬ СЮДА
            client_name=client_name,
            client_username=client_username,
            phone=phone,
            date=date,
            time=time_chosen,
            master_id=master_id,
        )
        session.add(booking)
        await session.commit()
        await session.refresh(booking)
        booking_id = booking.id
    # notify admins
    master_text = "К любому мастеру" if not master_id else f"К мастеру {master_id}"
    for admin in ADMIN_IDS:
        try:
            await bot.send_message(admin, f"Новая запись #{booking_id}: {client_name} ({('@'+client_username) if client_username else ''}), {phone} — {format_date_rus(date)} в {time_chosen} — {master_text}", reply_markup=admin_booking_kb(booking_id))
        except Exception:
            logger.exception("notify admin failed")
    # notify master(s)
    if master_id:
        try:
            await bot.send_message(master_id, f"Новая запись #{booking_id}: {client_name} ({('@'+client_username) if client_username else ''}), {phone} — {format_date_rus(date)} в {time_chosen}")
        except Exception:
            logger.exception("notify selected master failed")
    else:
        for m in MASTER_IDS:
            try:
                await bot.send_message(m, f"Новая запись #{booking_id}: {client_name} ({('@'+client_username) if client_username else ''}), {phone} — {format_date_rus(date)} в {time_chosen}")
            except Exception:
                logger.exception("notify master failed")
    await bot.send_message(callback.from_user.id, f"Ваша запись #{booking_id} на {format_date_rus(date)} в {time_chosen} создана и ожидает подтверждения.")
    await state.clear()
    await callback.answer()


# My bookings
@router.message(F.text == "👤 Мои записи")
async def my_bookings(message: Message):
    await mark_past_bookings()
    async with AsyncSession(engine) as session:
        result = await session.exec(select(Booking).where(Booking.user_id == message.from_user.id).where(Booking.status.notin_(["cancelled", PAST_STATUS])).order_by(Booking.id.desc()))
        rows = result.all()
    if not rows:
        await message.answer("У вас нет записей.")
        return
    text = "Ваши записи:\n"
    for r in rows:
        uname = f"@{r.client_username}" if r.client_username else ""
        mid_text = f", мастер: {r.master_id}" if r.master_id else ""
        text += f"#{r.id} — {r.client_name} {uname}{mid_text}, {r.phone} — {format_date_rus(r.date)} в {r.time} — {r.status}\n"
    await message.answer(text)


# Portfolio & photo handlers (robust save)
@router.message(F.text == "📁 Портфолио")
async def portfolio_menu(message: Message):
    rows = [["📸 Загрузить фото", "📂 Просмотреть портфолио"], ["◀️ Назад"]]
    await message.answer("Портфолио:", reply_markup=build_reply_kb(rows))


@router.message(F.text == "📸 Загрузить фото")
async def upload_photo_start(message: Message, state: FSMContext):
    await message.answer("Пришлите фото работы (как изображение или как файл).")
    await state.set_state(PhotoStates.waiting_for_photo)


# --- PATCHED robust download and handler below ---
import aiohttp

ERROR_LOG = os.path.join(PROJECT_FOLDER, "error_traces.log")

async def _notify_admins_trace(exc: Exception, context: str = ""):
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    try:
        os.makedirs(PROJECT_FOLDER, exist_ok=True)
        with open(ERROR_LOG, "a", encoding="utf-8") as ef:
            ef.write(f"\n\n[{datetime.utcnow().isoformat()}] Context: {context}\n")
            ef.write(tb)
    except Exception:
        logger.exception("Failed to write error log file")
    snippet = tb[:1800] + ("\n\n(трейс обрезан)" if len(tb) > 1800 else "")
    for aid in ADMIN_IDS:
        try:
            await bot.send_message(aid, f"Ошибка в {context}:\n\n{snippet}")
        except Exception:
            logger.exception("Failed to notify admin %s", aid)

async def download_file_via_bot(file_id: str, destination_path: str) -> None:
    os.makedirs(os.path.dirname(destination_path) or ".", exist_ok=True)
    last_exc = None
    file_obj = None
    try:
        file_obj = await bot.get_file(file_id)
    except Exception as e:
        last_exc = e
        logger.exception("bot.get_file failed: %s", e)
    if file_obj is not None:
        try:
            data = await bot.download_file(file_obj.file_path)
            if not data:
                raise RuntimeError("download_file empty data (file_path)")
            with open(destination_path, "wb") as outf:
                outf.write(data)
            return
        except Exception as e:
            last_exc = e
            logger.exception("download_file(file_path) failed: %s", e)
        try:
            data = await bot.download_file(file_id)
            if not data:
                raise RuntimeError("download_file empty data (file_id)")
            with open(destination_path, "wb") as outf:
                outf.write(data)
            return
        except Exception as e:
            last_exc = e
            logger.exception("download_file(file_id) failed: %s", e)
    try:
        if file_obj is None or not getattr(file_obj, "file_path", None):
            raise RuntimeError("No file_path for HTTP fallback")
        url = f"https://api.telegram.org/file/bot{API_TOKEN}/{file_obj.file_path}"
        timeout = aiohttp.ClientTimeout(total=60)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"HTTP status {resp.status}")
                CHUNK = 65536
                with open(destination_path, "wb") as outf:
                    async for chunk in resp.content.iter_chunked(CHUNK):
                        outf.write(chunk)
        if not os.path.exists(destination_path) or os.path.getsize(destination_path) == 0:
            raise RuntimeError("HTTP fallback produced empty file")
        return
    except Exception as e:
        last_exc = e
        logger.exception("HTTP fallback failed: %s", e)
    await _notify_admins_trace(last_exc or RuntimeError("unknown"), context=f"download_file_via_bot({file_id})")
    raise RuntimeError(f"Failed to download {file_id}") from last_exc

@router.message(StateFilter(PhotoStates.waiting_for_photo))
async def photo_received_any(message: Message, state: FSMContext):
    try:
        os.makedirs(UPLOAD_PATH, exist_ok=True)
    except Exception as e:
        logger.exception("Cannot create upload path: %s", e)
        await message.answer("Ошибка сервера: не удаётся подготовить папку для загрузки.")
        await state.clear()
        return

    async def fail_user(msg: str, exc: Exception = None, context: str = ""):
        logger.error("photo_received_any fail: %s | %s", msg, context)
        if exc:
            await _notify_admins_trace(exc, context)
        await message.answer("Произшла ошибка при сохранении фото. Попробуйте ещё раз.")
        await state.clear()

    file_path = None

    if message.photo:
        ph = message.photo[-1]
        file_path = os.path.join(UPLOAD_PATH, f"{uuid.uuid4().hex}.jpg")
        try:
            await ph.download(destination_file=file_path)
        except Exception as e:
            logger.exception("photo.download failed: %s", e)
            try:
                await download_file_via_bot(ph.file_id, file_path)
            except Exception as exc:
                await fail_user("fallback photo download fail", exc, "photo fallback")
                return

    elif getattr(message, "document", None):
        doc = message.document
        original = doc.file_name or ""
        ext = os.path.splitext(original)[1] if "." in original else ".jpg"
        file_path = os.path.join(UPLOAD_PATH, f"{uuid.uuid4().hex}{ext}")
        try:
            await doc.download(destination_file=file_path)
        except Exception as e:
            logger.exception("doc.download failed: %s", e)
            try:
                await download_file_via_bot(doc.file_id, file_path)
            except Exception as exc:
                await fail_user("fallback doc download fail", exc, "document fallback")
                return
    else:
        await message.answer("Файл не распознан как изображение.")
        return

    try:
        if not os.path.exists(file_path) or os.path.getsize(file_path) == 0:
            raise RuntimeError("saved file missing or empty")
    except Exception as e:
        await fail_user("validation fail", e, "post-validate")
        return

    await state.update_data(file_path=file_path)
    await message.answer("Фото сохранено. Пришлите подпись или отправьте /skip.")
    await state.set_state(PhotoStates.waiting_for_caption)

@router.message(StateFilter(PhotoStates.waiting_for_caption))
async def photo_caption(message: Message, state: FSMContext):
    try:
        data = await state.get_data()
        file_path = data.get("file_path")
        if not file_path or not os.path.exists(file_path):
            await message.answer("Не найден загруженный файл. Пожалуйста, загрузите снова.")
            await state.clear()
            return
        caption = (message.text or "").strip()
        async with AsyncSession(engine) as session:
            photo = Photo(user_id=message.from_user.id, file_path=file_path, caption=caption)
            session.add(photo)
            await session.commit()
            await session.refresh(photo)
        await message.answer("Фото и подпись успешно сохранены.")
        await state.clear()
    except Exception:
        logger.exception("Error in photo_caption")
        await message.answer("Ошибка при сохранении подписи. Попробуйте ещё раз.")
        await state.clear()


@router.message(Command(commands=["skip"]), StateFilter(PhotoStates.waiting_for_caption))
async def photo_skip(message: Message, state: FSMContext):
    try:
        data = await state.get_data()
        file_path = data.get("file_path")
        if not file_path or not os.path.exists(file_path):
            await message.answer("Не найден файл. Пожалуйста, загрузите фото снова.")
            await state.clear()
            return
        async with AsyncSession(engine) as session:
            photo = Photo(user_id=message.from_user.id, file_path=file_path, caption="")
            session.add(photo)
            await session.commit()
            await session.refresh(photo)
        await message.answer("Фото сохранено без подписи.")
        await state.clear()
    except Exception:
        logger.exception("Error in photo_skip")
        await message.answer("Ошибка при сохранении фото. Попробуйте ещё раз.")
        await state.clear()


@router.message(F.text == "📂 Просмотреть портфолио")
async def view_portfolio(message: Message):
    async with AsyncSession(engine) as session:
        result = await session.exec(select(Photo).where(Photo.user_id == message.from_user.id).order_by(Photo.id.desc()))
        rows = result.all()
    if not rows:
        await message.answer("Портфолио пустое.")
        return
    for r in rows[:20]:
        try:
            # Use InputFile to satisfy aiogram/pydantic expectations
            file_obj = InputFile(r.file_path)
            await bot.send_photo(message.from_user.id, file_obj, caption=f"{r.caption}\n(загружено: {r.uploaded_at})")
        except FileNotFoundError:
            logger.exception("Portfolio: file not found %s", r.file_path)
            await message.answer(f"Файл не найден: {os.path.basename(r.file_path)}")
        except Exception as e:
            logger.exception("send photo failed for %s: %s", r.file_path, e)
            continue


# Reviews
@router.message(F.text == "✍️ Оставить отзыв")
async def start_review(message: Message, state: FSMContext):
    await message.answer("Напишите ваш отзыв:")
    await state.set_state(ReviewStates.waiting_for_text)


@router.message(StateFilter(ReviewStates.waiting_for_text))
async def review_receive(message: Message, state: FSMContext):
    txt = (message.text or "").strip()
    if not txt:
        await message.answer("Пустой отзыв не сохранён.")
        return
    async with AsyncSession(engine) as session:
        review = Review(user_id=message.from_user.id, user_name=message.from_user.username or message.from_user.full_name, text=txt)
        session.add(review)
        await session.commit()
        await session.refresh(review)
    await message.answer("Спасибо — отзыв опубликован. Админ может удалить отзыв.")
    for admin in ADMIN_IDS:
        try:
            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Удалить", callback_data=f"del_review:{review.id}")]])
            await bot.send_message(admin, f"Новый отзыв #{review.id} от @{review.user_name}:\n\n{txt}", reply_markup=kb)
        except Exception:
            logger.exception("notify admin review failed")
    await state.clear()


@router.message(F.text == "Отзывы")
async def show_reviews(message: Message):
    async with AsyncSession(engine) as session:
        result = await session.exec(select(Review).order_by(Review.created_at.desc()).limit(20))
        rows = result.all()
    if not rows:
        await message.answer("Пока нет отзывов.")
        return
    for r in rows:
        uname = r.user_name or "аноним"
        if is_admin(message.from_user.id):
            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Удалить", callback_data=f"del_review:{r.id}")]])
            await message.answer(f"#{r.id} — {uname}\n{r.text}\n(добавлен: {r.created_at})", reply_markup=kb)
        else:
            await message.answer(f"#{r.id} — {uname}\n{r.text}\n(добавлен: {r.created_at})")


@router.callback_query(lambda c: c.data and c.data.startswith("del_review:"))
async def admin_delete_review(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Доступ запрещён.")
        return
    rid = int(callback.data.split(":", 1)[1])
    async with AsyncSession(engine) as session:
        result = await session.exec(select(Review).where(Review.id == rid))
        review = result.one_or_none()
        if not review:
            await callback.answer("Отзыв не найден.")
            return
        await session.delete(review)
        await session.commit()
    await callback.answer("Отзыв удалён.")
    try:
        await bot.edit_message_reply_markup(callback.from_user.id, callback.message.message_id, reply_markup=None)
    except Exception:
        pass


# Master panel
@router.message(F.text == "🔧 Панель мастера")
async def master_panel(message: Message):
    # master by DB or constant list
    is_m = False
    if message.from_user.id in MASTER_IDS:
        is_m = True
    async with AsyncSession(engine) as session:
        result = await session.exec(select(User).where(User.telegram_id == message.from_user.id))
        u = result.one_or_none()
        if u and u.is_master:
            is_m = True
    if not is_m:
        await message.answer("Доступ запрещён.")
        return
    rows = [["📋 Просмотр записей", "⬆️ Добавить фото в портфолио"], ["◀️ Назад"]]
    await message.answer("Панель мастера:", reply_markup=build_reply_kb(rows))


@router.message(F.text == "📋 Просмотр записей")
async def master_view_bookings(message: Message):
    # show bookings assigned to this master or unassigned
    is_m = False
    if message.from_user.id in MASTER_IDS:
        is_m = True
    async with AsyncSession(engine) as session:
        result = await session.exec(select(User).where(User.telegram_id == message.from_user.id))
        u = result.one_or_none()
        if u and u.is_master:
            is_m = True
    if not is_m:
        await message.answer("Доступ запрещён.")
        return
    await mark_past_bookings()
    async with AsyncSession(engine) as session:
        result = await session.exec(select(Booking).where((Booking.master_id == message.from_user.id) | (Booking.master_id.is_(None))).where(Booking.status.notin_(["cancelled", PAST_STATUS])).order_by(Booking.date, Booking.time))
        rows = result.all()
    if not rows:
        await message.answer("Записей нет.")
        return
    for r in rows:
        uname = f"@{r.client_username}" if r.client_username else ""
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Отменить (мастер)", callback_data=f"master_cancel:{r.id}")]])
        await message.answer(f"#{r.id} — {r.client_name} {uname}, {r.phone} — {format_date_rus(r.date)} в {r.time} — {r.status}", reply_markup=kb)


@router.callback_query(lambda c: c.data and c.data.startswith("master_cancel:"))
async def master_cancel(callback: CallbackQuery):
    # check master
    is_m = False
    if callback.from_user.id in MASTER_IDS:
        is_m = True
    async with AsyncSession(engine) as session:
        result = await session.exec(select(User).where(User.telegram_id == callback.from_user.id))
        u = result.one_or_none()
        if u and u.is_master:
            is_m = True
    if not is_m:
        await callback.answer("Доступ запрещён.")
        return
    bid = int(callback.data.split(":", 1)[1])
    async with AsyncSession(engine) as session:
        result = await session.exec(select(Booking).where(Booking.id == bid))
        booking = result.one_or_none()
        if not booking:
            await callback.answer("Запись не найдена.")
            return
        booking.status = "cancelled"
        session.add(booking)
        await session.commit()
    try:
        await bot.send_message(booking.user_id, f"Ваша запись #{bid} отменена мастером.")
    except Exception:
        logger.exception("notify user master cancel failed")
    await callback.answer("Запись отменена мастером.")
    try:
        await bot.edit_message_reply_markup(callback.from_user.id, callback.message.message_id, reply_markup=None)
    except Exception:
        pass


# Admin panel & masters management
@router.message(F.text == "🛠️ Админ-панель")
async def admin_panel(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("Доступ запрещён.")
        return
    rows = [["📩 Все записи", "🔎 Поиск по ID"], ["🧑‍🔧 Управление мастерами", "◀️ Назад"]]
    await message.answer("Админ-панель:", reply_markup=build_reply_kb(rows))


@router.message(F.text == "🧑‍🔧 Управление мастерами")
async def manage_masters(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("Доступ запрещён.")
        return
    masters = await get_masters_list()
    # offer "Добавить мастера" button
    await message.answer("Управление мастерами:", reply_markup=build_reply_kb([["Добавить мастера"], ["◀️ Назад"]]))
    if not masters:
        await message.answer("Мастеров пока нет.")
        return
    for mid, name , phone in masters:
        kb = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="Удалить мастера", callback_data=f"del_master:{mid}")]]
        )
        await message.answer(
            f"Мастер: {name} (id: {mid})" + (f", {phone}" if phone else ""),
            reply_markup=kb
        )


@router.message(F.text == "Добавить мастера")
async def admin_add_master_start(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer("Доступ запрещён.")
        return
    await message.answer("Введите Telegram ID мастера (число):")
    await state.set_state(MasterManageStates.waiting_for_new_master_id)


@router.message(StateFilter(MasterManageStates.waiting_for_new_master_id))
async def admin_add_master_id(message: Message, state: FSMContext):
    txt = message.text.strip()
    if not txt.isdigit():
        await message.answer("ID должен быть числом. Попробуйте ещё раз.")
        return
    mid = int(txt)
    await state.update_data(new_master_id=mid)
    await message.answer("Введите имя/ник мастера:")
    await state.set_state(MasterManageStates.waiting_for_new_master_name)


@router.message(StateFilter(MasterManageStates.waiting_for_new_master_name))
async def admin_add_master_name(message: Message, state: FSMContext):
    await state.update_data(new_master_name=message.text.strip())
    await message.answer("Введите телефон мастера или /skip:")
    await state.set_state(MasterManageStates.waiting_for_new_master_phone)


@router.message(StateFilter(MasterManageStates.waiting_for_new_master_phone))
async def admin_add_master_phone(message: Message, state: FSMContext):
    phone = message.text.strip()
    data = await state.get_data()
    mid = data["new_master_id"]
    name = data["new_master_name"]

    async with AsyncSession(engine) as session:
        result = await session.exec(select(User).where(User.telegram_id == mid))
        u = result.one_or_none()
        if not u:
            u = User(telegram_id=mid, name=name, phone=phone, is_master=True)
        else:
            u.name = name
            u.phone = phone
            u.is_master = True
        session.add(u)
        await session.commit()

    if mid not in MASTER_IDS:
        MASTER_IDS.append(mid)

    await message.answer(f"Мастер {name} добавлен.")
    await state.clear()


@router.callback_query(lambda c: c.data and c.data.startswith("del_master:"))
async def admin_delete_master(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Доступ запрещён.")
        return

    mid = int(callback.data.split(":", 1)[1])

    if mid in MASTER_IDS:
        MASTER_IDS.remove(mid)

    async with AsyncSession(engine) as session:
        result = await session.exec(select(User).where(User.telegram_id == mid))
        u = result.one_or_none()
        if u:
            u.is_master = False
            session.add(u)
            await session.commit()

    await callback.answer("Мастер удалён.")


@router.message(F.text == "📩 Все записи")
async def admin_all_bookings(message: Message):
    if not is_admin(message.from_user.id):
        return
    await mark_past_bookings()
    async with AsyncSession(engine) as session:
        result = await session.exec(
            select(Booking)
            .where(Booking.status.notin_(["cancelled", PAST_STATUS]))
            .order_by(Booking.created_at.desc())
        )
        rows = result.all()

    if not rows:
        await message.answer("Нет записей.")
        return

    for r in rows:
        uname = f"@{r.client_username}" if r.client_username else ""
        await message.answer(
            f"#{r.id} — {r.client_name} {uname}, {r.phone} — "
            f"{format_date_rus(r.date)} {r.time} — {r.status}",
            reply_markup=admin_booking_kb(r.id),
        )


@router.callback_query(lambda c: c.data and c.data.startswith("admin_confirm:"))
async def admin_confirm(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Доступ запрещён.")
        return

    bid = int(callback.data.split(":", 1)[1])

    async with AsyncSession(engine) as session:
        result = await session.exec(select(Booking).where(Booking.id == bid))
        booking = result.one_or_none()
        if not booking:
            await callback.answer("Запись не найдена.")
            return

        # ✅ СОХРАНЯЕМ ВСЁ ЗАРАНЕЕ
        user_id = booking.user_id
        chat_id = booking.chat_id
        master_id = booking.master_id
        date = booking.date
        time_ = booking.time

        booking.status = "confirmed"
        session.add(booking)
        await session.commit()

    # 🔔 клиент
    if chat_id:
        try:
            await bot.send_message(
                chat_id,
                (
                    "✅ Ваша запись подтверждена!\n\n"
                    f"📅 {format_date_rus(date)}\n"
                    f"⏰ {time_}\n\n"
                    "Ждём вас 💅"
                )
            )
        except Exception:
            pass

    # 👩‍🔧 мастер
    if master_id:
        try:
            await bot.send_message(
                master_id,
                (
                    "📌 Запись подтверждена администратором\n\n"
                    f"📅 {format_date_rus(date)}\n"
                    f"⏰ {time_}\n"
                    f"🆔 Запись #{bid}"
                )
            )
        except Exception:
            pass

    await callback.answer("Запись подтверждена.")

    try:
        await bot.edit_message_reply_markup(
            callback.from_user.id,
            callback.message.message_id,
            reply_markup=None
        )
    except Exception:
        pass

    @router.callback_query(lambda c: c.data and c.data.startswith("admin_cancel:"))
    async def admin_cancel(callback: CallbackQuery):
        if not is_admin(callback.from_user.id):
            await callback.answer("Доступ запрещён.")
            return

        bid = int(callback.data.split(":", 1)[1])

        async with AsyncSession(engine) as session:
            result = await session.exec(select(Booking).where(Booking.id == bid))
            booking = result.one_or_none()
            if not booking:
                await callback.answer("Запись не найдена.")
                return

            chat_id = booking.chat_id

            booking.status = "cancelled"
            session.add(booking)
            await session.commit()

        if chat_id:
            try:
                await bot.send_message(
                    chat_id,
                    "❌ Ваша запись была отменена администратором."
                )
            except Exception:
                pass

        await callback.answer("Запись отменена.")

        try:
            await bot.edit_message_reply_markup(
                callback.from_user.id,
                callback.message.message_id,
                reply_markup=None
            )
        except Exception:
            pass

    async with AsyncSession(engine) as session:
        booking = await session.get(Booking, bid)
        if not booking:
            await callback.answer("Не найдено.")
            return
        booking.status = "cancelled"
        session.add(booking)
        await session.commit()

    await bot.send_message(chat_id, "Ваша запись отменена ❌")
    await callback.answer("Отменено")


@router.message(F.text == "◀️ Назад")
async def go_back(message: Message):
    await cmd_start(message)


# ===== FastAPI =====
app = FastAPI()
app.mount("/uploads", StaticFiles(directory=UPLOAD_PATH), name="uploads")


@app.on_event("startup")
async def startup_event():
    await init_db()
    asyncio.create_task(reminder_loop())


# ===== Reminders (24h + 2h) =====
async def reminder_loop():
    sent = set()  # (booking_id, hours)

    while True:
        try:
            now = datetime.utcnow()

            async with AsyncSession(engine) as session:
                result = await session.exec(
                    select(
                        Booking.id,
                        Booking.chat_id,
                        Booking.date,
                        Booking.time,
                    ).where(Booking.status == "confirmed")
                )
                rows = result.all()

            for booking_id, chat_id, date, time_ in rows:
                if not chat_id:
                    continue

                try:
                    visit_dt = datetime.strptime(
                        f"{date} {time_}", "%Y-%m-%d %H:%M"
                    )
                except Exception:
                    continue

                delta = visit_dt - now

                for hours in (24, 2):
                    key = (booking_id, hours)
                    if key in sent:
                        continue

                    if timedelta(hours=hours - 0.1) <= delta <= timedelta(hours=hours + 0.1):
                        try:
                            await bot.send_message(
                                chat_id,
                                (
                                    "⏰ Напоминание о записи\n\n"
                                    f"📅 {format_date_rus(date)}\n"
                                    f"⏰ {time_}\n\n"
                                    f"До визита {hours} ч."
                                )
                            )
                            sent.add(key)
                        except Exception:
                            pass

        except Exception as e:
            logger.exception("Reminder loop error: %s", e)

        await asyncio.sleep(600)  # каждые 10 минут


# ===== Run =====
async def main():
    await init_db()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())

