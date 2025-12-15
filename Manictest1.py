import os
import shutil
import asyncio
import logging
import re
import locale
from zoneinfo import ZoneInfo
IRKUTSK_TZ = ZoneInfo("Asia/Irkutsk")
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
    date: str              # YYYY-MM-DD
    time: str              # HH:MM
    is_available: bool = True


class MasterWeekday(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    master_id: int
    weekday: int           # 0=Пн ... 6=Вс
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
    user_name: Optional[str] = None
    text: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class SalonInfo(SQLModel, table=True):
    id: Optional[int] = Field(default=1, primary_key=True)
    text: str = "💅 Наш салон маникюра\n\nЗаписывайтесь онлайн!"


# ================= RU HELPERS =================
RU_WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
RU_MONTHS = [
    "января", "февраля", "марта", "апреля",
    "мая", "июня", "июля", "августа",
    "сентября", "октября", "ноября", "декабря"
]


def format_date_ru(date_str: str) -> str:
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    return f"{RU_WEEKDAYS[dt.weekday()]}, {dt.day} {RU_MONTHS[dt.month - 1]}"


def format_datetime_ru(date_str: str, time_str: str) -> str:
    if ":" not in time_str:
        time_str = f"{time_str}:00"
    dt = datetime.strptime(
        f"{date_str} {time_str}", "%Y-%m-%d %H:%M"
    )
    return f"{RU_WEEKDAYS[dt.weekday()]}, {dt.day} {RU_MONTHS[dt.month - 1]} {dt.strftime('%H:%M')}"


# ================= HELPERS =================
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
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    weekday = dt.weekday()
    async with AsyncSession(engine) as s:
        res = await s.exec(
            select(MasterWeekday).where(
                MasterWeekday.master_id == master_id,
                MasterWeekday.weekday == weekday,
                MasterWeekday.is_enabled == True
            )
        )
        return res.first() is not None


# ================= START =================
@router.message(Command("start"))
async def start(msg: Message):
    rows = [
        ["📅 Записаться"],
        ["ℹ️ О салоне"],
        ["⭐ Отзывы"],
        ["📸 Наши работы"],
    ]

    if await is_admin(msg.from_user.id):
        rows.append(["🛠 Админ"])

    async with AsyncSession(engine) as s:
        res = await s.exec(
            select(User).where(
                User.telegram_id == msg.from_user.id,
                User.is_master == True
            )
        )
        if res.first():
            rows.append(["🧑‍🔧 Панель мастера"])

    await msg.answer(
        "💅 Добро пожаловать в салон маникюра!",
        reply_markup=reply_kb(rows)
    )

@router.message(F.text == "🛠 Админ")
async def admin_panel(msg: Message):
    if not await is_admin(msg.from_user.id):
        await msg.answer("⛔ У вас нет доступа")
        return

    await msg.answer(
        "🛠 Админ панель",
        reply_markup=reply_kb([
            ["➕ Добавить мастера"],
            ["➖ Удалить мастера"],
            ["✏️ О салоне"],
            ["📊 Статистика"],
            ["⬅️ Назад"]
        ])
    )


# ================= FSM =================
class BookingFSM(StatesGroup):
    name = State()
    phone = State()
    master = State()
    date = State()
    time = State()


class ReviewFSM(StatesGroup):
    text = State()


class AdminFSM(StatesGroup):
    add_master = State()


class SalonEditFSM(StatesGroup):
    text = State()


class MasterEditFSM(StatesGroup):
    name = State()
    phone = State()


# ================= EDIT SALON INFO =================
@router.message(F.text == "✏️ О салоне")
async def admin_edit_salon(msg: Message, state: FSMContext):
    if not await is_admin(msg.from_user.id):
        await msg.answer("⛔ У вас нет доступа")
        return

    async with AsyncSession(engine) as s:
        res = await s.exec(select(SalonInfo).where(SalonInfo.id == 1))
        info = res.first()
        salon_text = info.text if info else "Информация ещё не задана"

    await msg.answer(
        "✏️ *Текущая информация о салоне:*\n\n"
        f"{salon_text}\n\n"
        "📝 Введите новый текст:",
        parse_mode="Markdown"
    )

    await state.set_state(SalonEditFSM.text)


@router.message(StateFilter(SalonEditFSM.text))
async def admin_save_salon(msg: Message, state: FSMContext):
    async with AsyncSession(engine) as s:
        res = await s.exec(select(SalonInfo).where(SalonInfo.id == 1))
        info = res.first()

        if info:
            info.text = msg.text
        else:
            s.add(SalonInfo(id=1, text=msg.text))

        await s.commit()

    await msg.answer(
        "✅ Информация о салоне обновлена",
        reply_markup=reply_kb([["⬅️ Назад"]])
    )
    await state.clear()


# === кнопка "Добавить мастера" ===
@router.message(F.text == "➕ Добавить мастера")
async def admin_add_master(msg: Message, state: FSMContext):
    if not await is_admin(msg.from_user.id):
        await msg.answer("⛔ У вас нет доступа")
        return

    await msg.answer("Введите Telegram ID мастера:")
    await state.set_state(AdminFSM.add_master)


# === ввод Telegram ID ===
@router.message(StateFilter(AdminFSM.add_master))
async def admin_add_master_save(msg: Message, state: FSMContext):
    try:
        tg_id = int(msg.text)
    except ValueError:
        await msg.answer("❌ Telegram ID должен быть числом")
        return

    async with AsyncSession(engine) as s:
        res = await s.exec(
            select(User).where(User.telegram_id == tg_id)
        )
        user = res.first()

        if user:
            user.is_master = True
        else:
            user = User(
                telegram_id=tg_id,
                is_master=True
            )
            s.add(user)

        await s.commit()

        # создаём рабочие дни Пн–Пт
        for wd in range(5):
            s.add(
                MasterWeekday(
                    master_id=tg_id,
                    weekday=wd,
                    is_enabled=True
                )
            )

        await s.commit()

    await msg.answer("✅ Мастер добавлен", reply_markup=reply_kb([["⬅️ Назад"]]))
    await state.clear()


@router.message(F.text == "⬅️ Назад")
async def back(msg: Message, state: FSMContext):
    await state.clear()
    await start(msg)

# ================= ABOUT SALON =================
@router.message(F.text == "ℹ️ О салоне")
async def about_salon(msg: Message):
    async with AsyncSession(engine) as s:
        info = await s.get(SalonInfo, 1)

        if not info:
            info = SalonInfo(id=1)
            s.add(info)
            await s.commit()
            salon_text = info.text
        else:
            salon_text = info.text  # ✅ читаем ВНУТРИ сессии

    await msg.answer(
        salon_text,
        reply_markup=reply_kb([["⬅️ Назад"]])
    )






# ================= WORKS =================
@router.message(F.text == "📸 Наши работы")
async def works(msg: Message):
    await msg.answer(
        "📸 Примеры наших работ:",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="👀 Смотреть", url=WORKS_URL)]
            ]
        )
    )


# ================= REVIEWS =================
@router.message(F.text == "⭐ Отзывы")
async def reviews(msg: Message):
    async with AsyncSession(engine) as s:
        res = await s.exec(
            select(Review).order_by(Review.created_at.desc()).limit(5)
        )
        reviews = res.all()

    if not reviews:
        await msg.answer(
            "Пока нет отзывов.",
            reply_markup=reply_kb([["✍️ Оставить отзыв"], ["⬅️ Назад"]])
        )
        return

    text = "⭐ *Отзывы клиентов:*\n\n"
    for r in reviews:
        text += f"🗣 {r.user_name or 'Аноним'}:\n{r.text}\n\n"

    await msg.answer(
        text,
        parse_mode="Markdown",
        reply_markup=reply_kb([["✍️ Оставить отзыв"], ["⬅️ Назад"]])
    )


@router.message(F.text == "✍️ Оставить отзыв")
async def review_start(msg: Message, state: FSMContext):
    await msg.answer("Напишите ваш отзыв:")
    await state.set_state(ReviewFSM.text)


@router.message(StateFilter(ReviewFSM.text))
async def review_save(msg: Message, state: FSMContext):
    async with AsyncSession(engine) as s:
        s.add(
            Review(
                user_id=msg.from_user.id,
                user_name=msg.from_user.first_name,
                text=msg.text
            )
        )
        await s.commit()

    await msg.answer("⭐ Спасибо за отзыв!", reply_markup=reply_kb([["⬅️ Назад"]]))
    await state.clear()


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
            (m.name or f"ID {m.telegram_id}", f"bm:{m.telegram_id}")
            for m in masters
        ])
    )
    await state.set_state(BookingFSM.master)


# ================= SELECT MASTER =================
@router.callback_query(F.data.startswith("bm:"))
async def booking_master(cb: CallbackQuery, state: FSMContext):
    master_id = int(cb.data.split(":")[1])
    await state.update_data(master=master_id)

    dates = []
    for d in gen_dates():
        if await is_day_enabled(master_id, d):
            dates.append((format_date_ru(d), f"bd:{d}"))

    if not dates:
        await cb.message.answer("❌ У мастера нет рабочих дней")
        return

    await cb.message.answer(
        "Выберите дату:",
        reply_markup=inline_kb(dates)
    )
    await state.set_state(BookingFSM.date)




@router.callback_query(F.data.startswith("bd:"))
async def booking_date(cb: CallbackQuery, state: FSMContext):
    date = cb.data.split(":")[1]
    data = await state.get_data()

    now = datetime.now(IRKUTSK_TZ)

    async with AsyncSession(engine) as s:
        res = await s.exec(
            select(MasterSchedule).where(
                MasterSchedule.master_id == data["master"],
                MasterSchedule.date == date,
                MasterSchedule.is_available == True
            )
        )
        slots = res.all()

    valid_slots = []

    for slot in slots:
        dt = datetime.strptime(
            f"{date} {slot.time}", "%Y-%m-%d %H:%M"
        ).replace(tzinfo=IRKUTSK_TZ)

        # ❌ запрещаем прошлое и текущее время
        if dt > now:
            valid_slots.append(slot)

    if not valid_slots:
        await cb.answer("Нет доступного времени", show_alert=True)
        return

    await state.update_data(date=date)

    await cb.message.answer(
        f"⏰ {format_date_ru(date)}\nВыберите время:",
        reply_markup=inline_kb([
            (s.time, f"bt:{s.time}") for s in valid_slots
        ])
    )

    await state.set_state(BookingFSM.time)



# ================= FINISH BOOKING =================
@router.callback_query(F.data.startswith("bt:"))
async def booking_time(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    time = cb.data.split(":")[1]

    async with AsyncSession(engine) as s:
        res = await s.exec(
            select(User).where(User.telegram_id == data["master"])
        )
        master = res.first()
        if not master:
            await cb.message.answer("❌ Мастер не найден")
            return

        # ✅ сохраняем ПРИМИТИВЫ
        master_name = master.name or "Мастер"
        master_phone = master.phone or "не указан"
        master_tg = master.telegram_id

        booking = Booking(
            chat_id=cb.from_user.id,
            client_name=data["name"],
            phone=data["phone"],
            date=data["date"],
            time=time,
            master_id=master_tg,
            status="pending"
        )
        s.add(booking)

        q = await s.exec(
            select(MasterSchedule).where(
                MasterSchedule.master_id == master_tg,
                MasterSchedule.date == data["date"],
                MasterSchedule.time == time
            )
        )
        slot = q.first()
        if slot:
            slot.is_available = False

        await s.commit()

    # ⬇️ ВНЕ СЕССИИ — ТОЛЬКО ПРИМИТИВЫ
    formatted_dt = format_datetime_ru(data["date"], time)

    await cb.message.answer(
        "✅ **Запись создана!**\n\n"
        f"💅 Мастер: {master_name}\n"
        f"📞 Телефон: {master_phone}\n"
        f"📅 {formatted_dt}",
        parse_mode="Markdown",
        reply_markup=reply_kb([["⬅️ Назад"]])
    )

    await bot.send_message(
        master_tg,
        "🔔 **Новая запись!**\n\n"
        f"📅 {formatted_dt}\n"
        f"👤 Клиент: {data['name']}\n"
        f"📞 Телефон: {data['phone']}",
        parse_mode="Markdown"
    )

    await state.clear()


# ================= MASTER PANEL =================
@router.message(F.text == "🧑‍🔧 Панель мастера")
async def master_panel(msg: Message):
    await msg.answer(
        "🧑‍🔧 Панель мастера",
        reply_markup=reply_kb([
            ["📋 Мои записи"],
            ["🕒 Моё расписание"],
            ["📅 Дни работы"],
            ["✏️ Редактировать профиль"],
            ["⬅️ Назад"]
        ])
    )


class MasterEditFSM(StatesGroup):
    name = State()
    phone = State()

@router.message(F.text == "✏️ Редактировать профиль")
async def edit_profile(msg: Message):
    async with AsyncSession(engine) as s:
        res = await s.exec(
            select(User).where(
                User.telegram_id == msg.from_user.id,
                User.is_master == True
            )
        )
        if not res.first():
            await msg.answer("⛔ Только для мастеров")
            return

    await msg.answer(
        "Что хотите изменить?",
        reply_markup=reply_kb([
            ["✏️ Имя"],
            ["📞 Телефон"],
            ["⬅️ Назад"]
        ])
    )

@router.message(F.text == "✏️ Имя")
async def edit_name(msg: Message, state: FSMContext):
    await msg.answer("Введите новое имя:")
    await state.set_state(MasterEditFSM.name)


@router.message(StateFilter(MasterEditFSM.name))
async def save_name(msg: Message, state: FSMContext):
    async with AsyncSession(engine) as s:
        res = await s.exec(
            select(User).where(User.telegram_id == msg.from_user.id)
        )
        user = res.first()
        user.name = msg.text
        await s.commit()

    await msg.answer("✅ Имя обновлено", reply_markup=reply_kb([["⬅️ Назад"]]))
    await state.clear()

@router.message(F.text == "📞 Телефон")
async def edit_phone(msg: Message, state: FSMContext):
    await msg.answer("Введите новый телефон (+7999...):")
    await state.set_state(MasterEditFSM.phone)


@router.message(StateFilter(MasterEditFSM.phone))
async def save_phone(msg: Message, state: FSMContext):
    if not re.fullmatch(r"\+\d{10,15}", msg.text):
        await msg.answer("❌ Неверный формат")
        return

    async with AsyncSession(engine) as s:
        res = await s.exec(
            select(User).where(User.telegram_id == msg.from_user.id)
        )
        user = res.first()
        user.phone = msg.text
        await s.commit()

    await msg.answer("✅ Телефон обновлён", reply_markup=reply_kb([["⬅️ Назад"]]))
    await state.clear()

# ================= MASTER BOOKINGS =================
@router.message(F.text == "📋 Мои записи")
async def master_bookings(msg: Message):
    async with AsyncSession(engine) as s:
        res = await s.exec(
            select(Booking).where(
                Booking.master_id == msg.from_user.id,
                Booking.status.in_(["pending", "confirmed"])
            ).order_by(Booking.date, Booking.time)
        )
        bookings = res.all()

    if not bookings:
        await msg.answer(
            "📭 У вас пока нет записей",
            reply_markup=reply_kb([["⬅️ Назад"]])
        )
        return

    for b in bookings:
        status_icon = "🕓" if b.status == "pending" else "✅"

        await msg.answer(
            f"{status_icon} {format_datetime_ru(b.date, b.time)}\n"
            f"👤 Клиент: {b.client_name}\n"
            f"📞 Телефон: {b.phone}",
            reply_markup=inline_kb([
                ("✅ Подтвердить", f"mc:{b.id}"),
                ("❌ Отменить", f"mx:{b.id}")
            ]) if b.status == "pending" else None
        )


@router.callback_query(F.data.startswith("mc:"))
async def master_confirm(cb: CallbackQuery):
    booking_id = int(cb.data.split(":")[1])

    async with AsyncSession(engine) as s:
        res = await s.exec(select(Booking).where(Booking.id == booking_id))
        b = res.first()
        if not b:
            await cb.answer("Запись не найдена")
            return

        b.status = "confirmed"
        chat_id = b.chat_id
        await s.commit()

    await bot.send_message(chat_id, "✅ Ваша запись подтверждена")
    await cb.answer("Подтверждено")


@router.callback_query(F.data.startswith("mx:"))
async def master_cancel(cb: CallbackQuery):
    booking_id = int(cb.data.split(":")[1])

    async with AsyncSession(engine) as s:
        res = await s.exec(select(Booking).where(Booking.id == booking_id))
        b = res.first()
        if not b:
            await cb.answer("Запись не найдена")
            return

        b.status = "cancelled"
        chat_id = b.chat_id
        await s.commit()

    await bot.send_message(chat_id, "❌ Запись отменена")
    await cb.answer("Отменено")


# ================= MASTER SCHEDULE =================
@router.message(F.text == "🕒 Моё расписание")
async def master_schedule(msg: Message):
    await msg.answer(
        "Выберите дату:",
        reply_markup=inline_kb([
            (format_date_ru(d), f"sd:{d}") for d in gen_dates()
        ])
    )


@router.callback_query(F.data.startswith("sd:"))
async def schedule_day(cb: CallbackQuery):
    date = cb.data.split(":")[1]
    await cb.message.answer(
        f"{format_date_ru(date)}\nВыберите время:",
        reply_markup=inline_kb([
            (t, f"st:{date}:{t}") for t in time_slots()
        ])
    )


@router.callback_query(F.data.startswith("st:"))
async def toggle_slot(cb: CallbackQuery):
    parts = cb.data.split(":")
    date = parts[1]
    time = f"{parts[2]}:{parts[3]}"

    async with AsyncSession(engine) as s:
        res = await s.exec(
            select(MasterSchedule).where(
                MasterSchedule.master_id == cb.from_user.id,
                MasterSchedule.date == date,
                MasterSchedule.time == time
            )
        )
        slot = res.first()

        if slot:
            slot.is_available = not slot.is_available
        else:
            s.add(
                MasterSchedule(
                    master_id=cb.from_user.id,
                    date=date,
                    time=time,
                    is_available=True
                )
            )

        await s.commit()

        # 🔁 получаем обновлённые слоты
        res = await s.exec(
            select(MasterSchedule).where(
                MasterSchedule.master_id == cb.from_user.id,
                MasterSchedule.date == date
            )
        )
        slots = {s.time: s.is_available for s in res.all()}

    # 🔁 перерисовываем клавиатуру
    buttons = []
    for t in time_slots():
        mark = "✅" if slots.get(t, False) else "❌"
        buttons.append((f"{mark} {t}", f"st:{date}:{t}"))

    await cb.message.edit_reply_markup(
        reply_markup=inline_kb(buttons)
    )



# ================= MASTER WEEKDAYS =================
@router.message(F.text == "📅 Дни работы")
async def master_weekdays(msg: Message):
    async with AsyncSession(engine) as s:
        res = await s.exec(
            select(MasterWeekday).where(
                MasterWeekday.master_id == msg.from_user.id
            )
        )
        rows = res.all()
        enabled = {r.weekday for r in rows if r.is_enabled}

    buttons = []
    for i, name in enumerate(RU_WEEKDAYS):
        mark = "✅" if i in enabled else "❌"
        buttons.append((f"{mark} {name}", f"wd:{i}"))

    await msg.answer(
        "Выберите рабочие дни:",
        reply_markup=inline_kb(buttons)
    )


@router.callback_query(F.data.startswith("wd:"))
async def toggle_weekday(cb: CallbackQuery):
    wd = int(cb.data.split(":")[1])

    async with AsyncSession(engine) as s:
        res = await s.exec(
            select(MasterWeekday).where(
                MasterWeekday.master_id == cb.from_user.id,
                MasterWeekday.weekday == wd
            )
        )
        row = res.first()
        if row:
            row.is_enabled = not row.is_enabled
        else:
            s.add(
                MasterWeekday(
                    master_id=cb.from_user.id,
                    weekday=wd,
                    is_enabled=True
                )
            )
        await s.commit()

    await cb.answer("Обновлено")


# ================= REMINDERS =================
async def reminder_loop():
    while True:
        now = datetime.now(IRKUTSK_TZ)

        async with AsyncSession(engine) as s:
            res = await s.exec(
                select(Booking).where(Booking.status == "confirmed")
            )
            for b in res.all():
                time_str = b.time
                if ":" not in time_str:
                    time_str = f"{time_str}:00"

                dt = datetime.strptime(
                    f"{b.date} {time_str}", "%Y-%m-%d %H:%M"
                ).replace(tzinfo=UTC)

                delta = dt - now

                if not b.reminded_24h and timedelta(hours=24) > delta > timedelta(hours=23, minutes=50):
                    await bot.send_message(b.chat_id, "⏰ Напоминание: визит через 24 часа")
                    b.reminded_24h = True

                if not b.reminded_2h and timedelta(hours=2) > delta > timedelta(hours=1, minutes=50):
                    await bot.send_message(b.chat_id, "⏰ Напоминание: визит через 2 часа")
                    b.reminded_2h = True

            await s.commit()

        await asyncio.sleep(600)



# ================= BACKUP =================
async def backup_db():
    while True:
        try:
            ts = datetime.now().strftime("%Y-%m-%d_%H-%M")
            backup_path = f"{PROJECT_FOLDER}/backup_{ts}.db"
            shutil.copy(DB_FILE, backup_path)
            logger.info("DB backup created: %s", backup_path)
        except Exception as e:
            logger.error("Backup error: %s", e)

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
