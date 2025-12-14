import os
import asyncio
import logging
import sqlite3
import shutil
from datetime import datetime, timedelta
from typing import Optional, List, Tuple

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, StateFilter
from aiogram.types import (
    Message,
    CallbackQuery,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State

from sqlmodel import Field, SQLModel, select
from sqlmodel.ext.asyncio.session import AsyncSession
from sqlalchemy.ext.asyncio import create_async_engine

from fastapi import FastAPI

# ================= CONFIG =================
PROJECT_FOLDER = "Manictest1"
DB_FILE = os.path.join(PROJECT_FOLDER, "Manictest1.db")

# 🔐 TOKEN из Railway / окружения
API_TOKEN = os.getenv("BOT_TOKEN")
if not API_TOKEN:
    raise RuntimeError("BOT_TOKEN not set")

# 👑 Админы из Railway Variables
ADMIN_IDS = [
    int(x) for x in os.getenv("ADMIN_IDS", "580493054").split(",")
    if x.strip().isdigit()
]

# 👩‍🔧 Мастера (можно позже тоже вынести в env)
MASTER_IDS = [580493054]

TG_GROUP_URL = "https://t.me/testworkmanic"

# ================= BOT INIT =================
bot = Bot(token=API_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
router = Router()
dp.include_router(router)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


PAST_STATUS = "past"

os.makedirs(PROJECT_FOLDER, exist_ok=True)
# ==========================================

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ===== Aiogram init =====
bot = Bot(token=API_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
router = Router()
dp.include_router(router)

# ===== MODELS =====
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
    chat_id: Optional[int] = None
    client_name: str
    client_username: Optional[str] = None
    phone: str
    date: str
    time: str
    status: str = Field(default="pending")
    master_id: Optional[int] = None
    created_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())


class Review(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int
    user_name: Optional[str] = None
    text: str
    created_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())


# ✅ НОВОЕ: информация о салоне
class SalonInfo(SQLModel, table=True):
    id: Optional[int] = Field(default=1, primary_key=True)
    text: str


DATABASE_URL = f"sqlite+aiosqlite:///{DB_FILE}"
engine = create_async_engine(DATABASE_URL, echo=False, future=True)

# ===== HELPERS =====
def is_admin(tg_id: int) -> bool:
    return tg_id in ADMIN_IDS


def is_master_id(tg_id: int) -> bool:
    return tg_id in MASTER_IDS


def generate_dates(num_days=30):
    today = datetime.now().date()
    return [(today + timedelta(days=i)).isoformat() for i in range(num_days)]


def default_time_slots():
    return ["10:00", "11:00", "12:00", "13:00", "15:00", "16:00", "17:00"]


def build_inline_kb_from_pairs(
    pairs: List[Tuple[str, str]], row_width: int = 3
) -> InlineKeyboardMarkup:
    rows, row = [], []
    for i, (text, cb) in enumerate(pairs):
        row.append(InlineKeyboardButton(text=text, callback_data=cb))
        if (i + 1) % row_width == 0:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_reply_kb(rows: List[List[str]]) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=t) for t in r] for r in rows],
        resize_keyboard=True,
    )


def format_date_rus(iso_date: str) -> str:
    try:
        return datetime.fromisoformat(iso_date).strftime("%d.%m.%Y")
    except Exception:
        return iso_date


# ===== FSM =====
class BookingStates(StatesGroup):
    waiting_for_name = State()
    waiting_for_phone = State()
    waiting_for_master = State()
    waiting_for_date = State()
    waiting_for_time = State()


class ReviewStates(StatesGroup):
    waiting_for_text = State()


class MasterManageStates(StatesGroup):
    waiting_for_new_master_id = State()
    waiting_for_new_master_name = State()
    waiting_for_new_master_phone = State()


class SalonInfoStates(StatesGroup):
    editing_text = State()
# ===== START / MENU =====
WELCOME_TEXT = (
    "💅 Добро пожаловать в студию маникюра!\n\n"
    "Мы делаем маникюр, покрытие и дизайн — аккуратно и красиво.\n"
    "Нажмите «📅 Записаться», чтобы начать."
)


@router.message(Command(commands=["start", "help"]))
async def cmd_start(message: Message):
    rows = [
        ["📅 Записаться"],
        ["ℹ️ О салоне", "✍️ Оставить отзыв"],
        ["Отзывы", "👤 Мои записи"],
    ]

    is_m = message.from_user.id in MASTER_IDS
    is_a = message.from_user.id in ADMIN_IDS

    async with AsyncSession(engine) as session:
        result = await session.exec(
            select(User).where(User.telegram_id == message.from_user.id)
        )
        u = result.one_or_none()
        if u:
            is_m = is_m or u.is_master
            is_a = is_a or u.is_admin

    if is_m:
        rows.append(["🔧 Панель мастера"])
    if is_a:
        rows.append(["🛠️ Админ-панель"])

    await message.answer(WELCOME_TEXT, reply_markup=build_reply_kb(rows))

    await message.answer(
        "Наши работы и отзывы:",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="📸 Группа с работами", url=TG_GROUP_URL)]
            ]
        ),
    )


# ===== О САЛОНЕ =====
@router.message(F.text == "ℹ️ О салоне")
async def salon_info(message: Message):
    async with AsyncSession(engine) as session:
        result = await session.exec(select(SalonInfo).where(SalonInfo.id == 1))
        info = result.one_or_none()

    text = info.text if info else "Информация о салоне пока не добавлена."

    if is_admin(message.from_user.id):
        kb = build_reply_kb([["✏️ Редактировать"], ["◀️ Назад"]])
        await message.answer(text, reply_markup=kb)
    else:
        await message.answer(text)


@router.message(F.text == "✏️ Редактировать")
async def edit_salon_info_start(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await message.answer("Введите новый текст о салоне:")
    await state.set_state(SalonInfoStates.editing_text)


@router.message(StateFilter(SalonInfoStates.editing_text))
async def edit_salon_info_save(message: Message, state: FSMContext):
    async with AsyncSession(engine) as session:
        result = await session.exec(select(SalonInfo).where(SalonInfo.id == 1))
        info = result.one_or_none()
        if not info:
            info = SalonInfo(id=1, text=message.text.strip())
        else:
            info.text = message.text.strip()
        session.add(info)
        await session.commit()

    await message.answer("✅ Информация о салоне обновлена.")
    await state.clear()
    await cmd_start(message)


# ===== BOOKING START =====
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
    if not phone.startswith("+"):
        await message.answer("Укажите телефон в формате +79991234567")
        return

    await state.update_data(phone=phone)

    masters = await get_masters_list()
    pairs = [("К любому мастеру", "book_master:0")]
    for mid, name, phone_m in masters:
        label = f"{name}" + (f" ({phone_m})" if phone_m else "")
        pairs.append((label, f"book_master:{mid}"))

    await message.answer(
        "Выберите мастера:",
        reply_markup=build_inline_kb_from_pairs(pairs, row_width=1),
    )
    await state.set_state(BookingStates.waiting_for_master)


# ⬇️ дальше код booking_date / booking_time
# ⬇️ отзывы
# ⬇️ мастер-панель
# ⬇️ админ-панель
# ⬇️ подтверждение / отмена
# ⬇️ напоминания
# ⬇️ RUN
# ⚠️ ИДУТ БЕЗ ИЗМЕНЕНИЙ — как в твоём исходнике
# ================= ADMIN CONFIRM =================

@router.callback_query(lambda c: c.data and c.data.startswith("admin_confirm:"))
async def admin_confirm(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Доступ запрещён.")
        return

    bid = int(callback.data.split(":", 1)[1])

    async with AsyncSession(engine) as session:
        booking = await session.get(Booking, bid)
        if not booking:
            await callback.answer("Запись не найдена.")
            return

        chat_id = booking.chat_id
        master_id = booking.master_id
        date = booking.date
        time_ = booking.time

        booking.status = "confirmed"
        session.add(booking)
        await session.commit()

    if chat_id:
        try:
            await bot.send_message(
                chat_id,
                (
                    "✅ Ваша запись подтверждена!\n\n"
                    f"📅 {format_date_rus(date)}\n"
                    f"⏰ {time_}\n\n"
                    "Ждём вас 💅"
                ),
            )
        except Exception:
            pass

    if master_id:
        try:
            await bot.send_message(
                master_id,
                f"📌 Подтверждена запись #{bid}\n{format_date_rus(date)} {time_}",
            )
        except Exception:
            pass

    await callback.answer("Запись подтверждена ✅")

    try:
        await bot.edit_message_reply_markup(
            callback.from_user.id,
            callback.message.message_id,
            reply_markup=None,
        )
    except Exception:
        pass


# ================= ADMIN CANCEL =================

@router.callback_query(lambda c: c.data and c.data.startswith("admin_cancel:"))
async def admin_cancel(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Доступ запрещён.")
        return

    bid = int(callback.data.split(":", 1)[1])

    async with AsyncSession(engine) as session:
        booking = await session.get(Booking, bid)
        if not booking:
            await callback.answer("Запись не найдена.")
            return

        chat_id = booking.chat_id
        booking.status = "cancelled"
        session.add(booking)
        await session.commit()

    if chat_id:
        try:
            await bot.send_message(chat_id, "❌ Ваша запись отменена администратором.")
        except Exception:
            pass

    await callback.answer("Запись отменена ❌")

    try:
        await bot.edit_message_reply_markup(
            callback.from_user.id,
            callback.message.message_id,
            reply_markup=None,
        )
    except Exception:
        pass


# ================= MASTER CANCEL =================

@router.callback_query(lambda c: c.data and c.data.startswith("master_cancel:"))
async def master_cancel(callback: CallbackQuery):
    if not is_master_id(callback.from_user.id):
        await callback.answer("Доступ запрещён.")
        return

    bid = int(callback.data.split(":", 1)[1])

    async with AsyncSession(engine) as session:
        booking = await session.get(Booking, bid)
        if not booking:
            await callback.answer("Запись не найдена.")
            return

        booking.status = "cancelled"
        session.add(booking)
        await session.commit()

    try:
        await bot.send_message(
            booking.user_id,
            f"❌ Ваша запись #{bid} отменена мастером.",
        )
    except Exception:
        pass

    await callback.answer("Запись отменена")

    try:
        await bot.edit_message_reply_markup(
            callback.from_user.id,
            callback.message.message_id,
            reply_markup=None,
        )
    except Exception:
        pass


# ================= REMINDERS =================

async def reminder_loop():
    sent = set()

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
                                ),
                            )
                            sent.add(key)
                        except Exception:
                            pass

        except Exception as e:
            logger.exception("Reminder loop error: %s", e)

        await asyncio.sleep(600)

# ================= DB INIT =================

async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    logger.info("DB tables created (if not exists).")

PROJECT_FOLDER = "Manictest1"
DB_FILE = os.path.join(PROJECT_FOLDER, "Manictest1.db")
UPLOAD_PATH = os.path.join(PROJECT_FOLDER, "uploads")

os.makedirs(PROJECT_FOLDER, exist_ok=True)
os.makedirs(UPLOAD_PATH, exist_ok=True)
# ================= FASTAPI =================

app = FastAPI()


@app.on_event("startup")
async def startup_event():
    await init_db()
    asyncio.create_task(reminder_loop())


# ================= RUN =================
async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    logger.info("DB tables created (if not exists).")

async def main():
    await init_db()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
