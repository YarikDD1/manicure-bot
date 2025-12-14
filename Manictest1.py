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

API_TOKEN = os.getenv("BOT_TOKEN")
if not API_TOKEN:
    raise RuntimeError("BOT_TOKEN not set")

ADMIN_IDS = [
    int(x) for x in os.getenv("ADMIN_IDS", "580493054").split(",")
    if x.strip().isdigit()
]

MASTER_IDS = [580493054]
TG_GROUP_URL = "https://t.me/testworkmanic"

PAST_STATUS = "past"

os.makedirs(PROJECT_FOLDER, exist_ok=True)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ================= BOT INIT =================
bot = Bot(token=API_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
router = Router()
dp.include_router(router)

# ================= DATABASE =================
DATABASE_URL = f"sqlite+aiosqlite:///{DB_FILE}"
engine = create_async_engine(DATABASE_URL, echo=False, future=True)

# ================= MODELS =================
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


class SalonInfo(SQLModel, table=True):
    id: Optional[int] = Field(default=1, primary_key=True)
    text: str


# ================= HELPERS =================
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


# ================= FIX: MASTERS LIST =================
async def get_masters_list() -> List[Tuple[int, str, Optional[str]]]:
    async with AsyncSession(engine) as session:
        result = await session.exec(
            select(User).where(User.is_master == True)
        )
        users = result.all()

    masters = []
    for u in users:
        name = u.name or f"ID {u.telegram_id}"
        masters.append((u.telegram_id, name, u.phone))
    return masters


# ================= FSM =================
class BookingStates(StatesGroup):
    waiting_for_name = State()
    waiting_for_phone = State()
    waiting_for_master = State()
    waiting_for_date = State()
    waiting_for_time = State()


class ReviewStates(StatesGroup):
    waiting_for_text = State()


class SalonInfoStates(StatesGroup):
    editing_text = State()


# ================= START =================
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

    if is_admin(message.from_user.id):
        rows.append(["🛠️ Админ-панель"])

    await message.answer(WELCOME_TEXT, reply_markup=build_reply_kb(rows))


# ================= BOOKING =================
@router.message(F.text == "📅 Записаться")
async def start_booking(message: Message, state: FSMContext):
    await message.answer("Как вас зовут?")
    await state.set_state(BookingStates.waiting_for_name)


@router.message(StateFilter(BookingStates.waiting_for_name))
async def booking_name(message: Message, state: FSMContext):
    await state.update_data(client_name=message.text.strip())
    await message.answer("Телефон (пример: +79991234567):")
    await state.set_state(BookingStates.waiting_for_phone)


@router.message(StateFilter(BookingStates.waiting_for_phone))
async def booking_phone(message: Message, state: FSMContext):
    phone = message.text.strip()
    if not phone.startswith("+"):
        await message.answer("Телефон должен быть в формате +79991234567")
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
                                f"⏰ Напоминание: визит через {hours} ч.",
                            )
                            sent.add(key)
                        except Exception:
                            pass

        except Exception as e:
            logger.exception("Reminder error: %s", e)

        await asyncio.sleep(600)


# ================= DB INIT =================
async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    logger.info("DB tables created (if not exists).")


# ================= FASTAPI =================
app = FastAPI()


# ================= RUN =================
async def main():
    await init_db()
    asyncio.create_task(reminder_loop())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
