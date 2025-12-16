# ================= IMPORTS =================
import os
import shutil
import asyncio
import logging
import re
import locale
from zoneinfo import ZoneInfo
from datetime import datetime, timedelta, UTC
from typing import Optional, List

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, StateFilter
from aiogram.types import (
    Message, CallbackQuery,
    ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton
)
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State

from sqlmodel import Field, SQLModel, select
from sqlmodel.ext.asyncio.session import AsyncSession
from sqlalchemy.ext.asyncio import create_async_engine


# ================= TIMEZONE =================
IRKUTSK_TZ = ZoneInfo("Asia/Irkutsk")


# ================= LOCALE =================
try:
    locale.setlocale(locale.LC_TIME, "ru_RU.UTF-8")
except locale.Error:
    pass


# ================= CONFIG =================
PROJECT_FOLDER = "Manictest1"
DB_FILE = os.path.join(PROJECT_FOLDER, "Manictest1.db")

API_TOKEN = os.getenv("BOT_TOKEN")
if not API_TOKEN:
    raise RuntimeError("BOT_TOKEN not set")

ADMIN_IDS = [580493054]
WORKS_URL = "https://t.me/testworkmanic"

os.makedirs(PROJECT_FOLDER, exist_ok=True)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# ================= BOT =================
bot = Bot(API_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)


# ================= DB =================
engine = create_async_engine(
    f"sqlite+aiosqlite:///{DB_FILE}",
    echo=False
)


# ================= MODELS =================
class User(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    telegram_id: int
    name: Optional[str] = None
    phone: Optional[str] = None
    is_master: bool = False
    is_admin: bool = False


class MasterSchedule(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    master_id: int
    date: str
    time: str
    is_available: bool = True


class MasterWeekday(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    master_id: int
    weekday: int
    is_enabled: bool = True


class Booking(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    chat_id: int
    client_name: str
    phone: str
    date: str
    time: str
    master_id: int
    status: str = "pending"
    reminded_24h: bool = False
    reminded_2h: bool = False
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class Review(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int
    user_name: Optional[str]
    text: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class SalonInfo(SQLModel, table=True):
    id: Optional[int] = Field(default=1, primary_key=True)
    text: str = "💅 Наш салон маникюра\n\nЗаписывайтесь онлайн!"


# ================= HELPERS =================
async def get_current_master(session: AsyncSession, telegram_id: int) -> Optional[User]:
    res = await session.exec(
        select(User).where(
            User.telegram_id == telegram_id,
            User.is_master == True
        )
    )
    return res.first()


async def is_admin(uid: int) -> bool:
    if uid in ADMIN_IDS:
        return True
    async with AsyncSession(engine) as s:
        res = await s.exec(
            select(User).where(
                User.telegram_id == uid,
                User.is_admin == True
            )
        )
        return res.first() is not None


def reply_kb(rows: List[List[str]]):
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=t) for t in r] for r in rows],
        resize_keyboard=True
    )


def inline_kb(pairs):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t, callback_data=c)] for t, c in pairs
        ]
    )


def gen_dates(days=14):
    today = datetime.now(IRKUTSK_TZ).date()
    return [(today + timedelta(days=i)).isoformat() for i in range(days)]


def time_slots():
    return ["10:00", "11:00", "12:00", "13:00", "15:00", "16:00", "17:00"]


async def is_day_enabled(master_id: int, date_str: str) -> bool:
    weekday = datetime.strptime(date_str, "%Y-%m-%d").weekday()
    async with AsyncSession(engine) as s:
        res = await s.exec(
            select(MasterWeekday).where(
                MasterWeekday.master_id == master_id,
                MasterWeekday.weekday == weekday,
                MasterWeekday.is_enabled == True
            )
        )
        return res.first() is not None

# ================= FSM =================
class BookingFSM(StatesGroup):
    name = State()
    phone = State()
    master = State()
    date = State()
    time = State()


# ================= BOOKING =================
@router.message(F.text == "📅 Записаться")
async def booking_start(msg: Message, state: FSMContext):
    await msg.answer("Как вас зовут?")
    await state.set_state(BookingFSM.name)


@router.message(StateFilter(BookingFSM.name))
async def booking_name(msg: Message, state: FSMContext):
    await state.update_data(name=msg.text)
    await msg.answer("Введите телефон (+79999999999):")
    await state.set_state(BookingFSM.phone)


@router.message(StateFilter(BookingFSM.phone))
async def booking_phone(msg: Message, state: FSMContext):
    if not re.fullmatch(r"\+\d{10,15}", msg.text):
        await msg.answer("❌ Неверный формат телефона")
        return

    await state.update_data(phone=msg.text)

    async with AsyncSession(engine) as s:
        res = await s.exec(select(User).where(User.is_master == True))
        masters = res.all()

    await msg.answer(
        "Выберите мастера:",
        reply_markup=inline_kb([
            (m.name or f"Мастер {m.id}", f"bm:{m.id}")
            for m in masters
        ])
    )
    await state.set_state(BookingFSM.master)


@router.callback_query(F.data.startswith("bm:"))
async def booking_master(cb: CallbackQuery, state: FSMContext):
    master_id = int(cb.data.split(":")[1])
    await state.update_data(master=master_id)

    dates = [(d, f"bd:{d}") for d in gen_dates() if await is_day_enabled(master_id, d)]
    if not dates:
        await cb.answer("Нет рабочих дней", show_alert=True)
        return

    await cb.message.answer("Выберите дату:", reply_markup=inline_kb(dates))
    await state.set_state(BookingFSM.date)


@router.callback_query(F.data.startswith("bd:"))
async def booking_date(cb: CallbackQuery, state: FSMContext):
    date = cb.data.split(":")[1]
    data = await state.get_data()
    master_id = data["master"]

    async with AsyncSession(engine) as s:
        res = await s.exec(
            select(MasterSchedule).where(
                MasterSchedule.master_id == master_id,
                MasterSchedule.date == date,
                MasterSchedule.is_available == True
            )
        )
        slots = res.all()

    if not slots:
        await cb.answer("Нет времени", show_alert=True)
        return

    await state.update_data(date=date)
    await cb.message.answer(
        "Выберите время:",
        reply_markup=inline_kb([(s.time, f"bt:{s.time}") for s in slots])
    )
    await state.set_state(BookingFSM.time)


@router.callback_query(F.data.startswith("bt:"))
async def booking_time(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    time = cb.data.split(":")[1]

    async with AsyncSession(engine) as s:
        master = await s.get(User, data["master"])
        booking = Booking(
            chat_id=cb.from_user.id,
            client_name=data["name"],
            phone=data["phone"],
            date=data["date"],
            time=time,
            master_id=master.id
        )
        s.add(booking)

        res = await s.exec(
            select(MasterSchedule).where(
                MasterSchedule.master_id == master.id,
                MasterSchedule.date == data["date"],
                MasterSchedule.time == time
            )
        )
        slot = res.first()
        if slot:
            slot.is_available = False

        await s.commit()

    await cb.message.answer("✅ Запись создана", reply_markup=reply_kb([["⬅️ Назад"]]))
    await state.clear()


# ================= ADMIN =================
@router.message(Command("start"))
async def start(msg: Message):
    rows = [["📅 Записаться"], ["ℹ️ О салоне"], ["📸 Наши работы"]]
    if await is_admin(msg.from_user.id):
        rows.append(["🛠 Админ"])
    async with AsyncSession(engine) as s:
        if await get_current_master(s, msg.from_user.id):
            rows.append(["🧑‍🔧 Панель мастера"])
    await msg.answer("Добро пожаловать!", reply_markup=reply_kb(rows))


@router.message(F.text == "🧑‍🔧 Панель мастера")
async def master_panel(msg: Message):
    await msg.answer(
        "Панель мастера",
        reply_markup=reply_kb([
            ["📋 Мои записи"],
            ["🕒 Моё расписание"],
            ["📅 Дни работы"],
            ["⬅️ Назад"]
        ])
    )


@router.message(F.text == "📋 Мои записи")
async def master_bookings(msg: Message):
    async with AsyncSession(engine) as s:
        user = await get_current_master(s, msg.from_user.id)
        res = await s.exec(select(Booking).where(Booking.master_id == user.id))
        bookings = res.all()

    for b in bookings:
        await msg.answer(f"{b.date} {b.time} — {b.client_name}")

# ================= SERVICE =================
async def reminder_loop():
    while True:
        await asyncio.sleep(600)


async def backup_db():
    while True:
        try:
            shutil.copy(DB_FILE, f"{PROJECT_FOLDER}/backup.db")
        except Exception as e:
            logger.error(e)
        await asyncio.sleep(86400)


# ================= RUN =================
async def main():
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)

    asyncio.create_task(reminder_loop())
    asyncio.create_task(backup_db())

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
