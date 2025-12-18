import os
import shutil
import asyncio
import logging
import re
import locale
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
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

from sqlmodel import Field, SQLModel, select, delete
from sqlmodel.ext.asyncio.session import AsyncSession
from sqlalchemy.ext.asyncio import create_async_engine


# ================= LOCALE =================
try:
    locale.setlocale(locale.LC_TIME, "ru_RU.UTF-8")
except locale.Error:
    pass

LOCAL_TZ = ZoneInfo("Asia/Irkutsk")

# ================= CONFIG =================
PROJECT_FOLDER = "data"
DB_FILE = os.path.join(PROJECT_FOLDER, "bot.db")

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



class Review(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int
    user_name: Optional[str]
    text: str


class SalonInfo(SQLModel, table=True):
    id: Optional[int] = Field(default=1, primary_key=True)
    text: str = "💈 Добро пожаловать в салон!"


# ================= HELPERS =================
def reply_kb(rows):
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=t) for t in r] for r in rows],
        resize_keyboard=True
    )

MONTHS_RU = {
    1: "января", 2: "февраля", 3: "марта", 4: "апреля",
    5: "мая", 6: "июня", 7: "июля", 8: "августа",
    9: "сентября", 10: "октября", 11: "ноября", 12: "декабря"
}

WEEKDAYS_RU = {
    0: "Пн", 1: "Вт", 2: "Ср",
    3: "Чт", 4: "Пт", 5: "Сб", 6: "Вс"
}


def format_date_ru(date_str: str) -> str:
    d = datetime.fromisoformat(date_str)
    return f"{d.day} {MONTHS_RU[d.month]} ({WEEKDAYS_RU[d.weekday()]})"


def format_datetime_ru(date_str: str, time_str: str) -> str:
    d = datetime.fromisoformat(date_str)
    return f"{d.day} {MONTHS_RU[d.month]} {time_str}"



def inline_kb(pairs):
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=c)] for t, c in pairs]
    )


def gen_dates(days=14):
    today = datetime.now(LOCAL_TZ).date()
    return [(today + timedelta(days=i)).isoformat() for i in range(days)]


def time_slots():
    return ["10:00", "11:00", "12:00", "13:00","14:00", "15:00", "16:00", "17:00"]


async def is_day_enabled(master_id: int, date_str: str) -> bool:
    weekday = datetime.fromisoformat(date_str).weekday()
    async with AsyncSession(engine) as s:
        res = await s.exec(
            select(MasterWeekday).where(
                MasterWeekday.master_id == master_id,
                MasterWeekday.weekday == weekday,
                MasterWeekday.is_enabled == True
            )
        )
        return res.first() is not None


async def ensure_master_weekdays(master_id: int):
    async with AsyncSession(engine) as s:
        res = await s.exec(
            select(MasterWeekday).where(
                MasterWeekday.master_id == master_id
            )
        )
        existing = {d.weekday for d in res.all()}

        for i in range(7):
            if i not in existing:
                s.add(
                    MasterWeekday(
                        master_id=master_id,
                        weekday=i,
                        is_enabled=(i < 5)
                    )
                )
        await s.commit()

def booking_status_ru(status: str) -> str:
    return {
        "pending": "⏳ Ожидание ответа мастера",
        "confirmed": "✅ Подтверждена",
        "cancelled": "❌ Отменена"
    }.get(status, status)


def booking_card(text: str) -> str:
    return (
        "━━━━━━━━━━━━━━\n"
        f"{text}\n"
        "━━━━━━━━━━━━━━"
    )


@router.callback_query(F.data.startswith("bm:"))
async def booking_master(cb: CallbackQuery, state: FSMContext):
    master_id = int(cb.data.split(":")[1])
    await state.update_data(master=master_id)

    dates = []  # 🔥 ВОТ ЭТОГО НЕ ХВАТАЛО

    for d in gen_dates():
        if await is_day_enabled(master_id, d):
            dates.append((format_date_ru(d), f"bd:{d}"))

    if not dates:
        await cb.answer("У мастера нет рабочих дней", show_alert=True)
        return

    await cb.message.answer(
        "Выберите дату:",
        reply_markup=inline_kb(dates)
    )
    await state.set_state(BookingFSM.date)


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
    remove_master = State()


class SalonEditFSM(StatesGroup):
    text = State()


class MasterEditFSM(StatesGroup):
    name = State()
    phone = State()


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



# ================= START =================
@router.message(Command("start"))
async def start(msg: Message):
    rows = [
        ["📅 Записаться"],
        ["📋 Мои записи"],
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
        "💈 Маникюрный салон\n\n"
        "Онлайн-запись к мастерам:\n"
        "• выбор даты и времени\n"
        "• подтверждение записи\n"
        "• автоматические напоминания\n\n"
        "Выберите действие ниже 👇",
        reply_markup=reply_kb(rows)
    )


# ================= REVIEWS =================

@router.message(F.text == "⭐ Отзывы")
async def reviews_menu(msg: Message):
    await msg.answer(
        "⭐ Отзывы",
        reply_markup=reply_kb([
            ["✍️ Оставить отзыв"],
            ["📖 Посмотреть отзывы"],
            ["⬅️ Назад"]
        ])
    )


@router.message(F.text == "✍️ Оставить отзыв")
async def review_start(msg: Message, state: FSMContext):
    await msg.answer("✍️ Напишите ваш отзыв:")
    await state.set_state(ReviewFSM.text)


@router.message(StateFilter(ReviewFSM.text))
async def review_save(msg: Message, state: FSMContext):
    async with AsyncSession(engine) as s:
        s.add(
            Review(
                user_id=msg.from_user.id,
                user_name=msg.from_user.full_name,
                text=msg.text
            )
        )
        await s.commit()

    await msg.answer(
        "✅ Спасибо за отзыв!",
        reply_markup=reply_kb([["⬅️ Назад"]])
    )
    await state.clear()


@router.message(F.text == "📖 Посмотреть отзывы")
async def reviews_show(msg: Message):
    async with AsyncSession(engine) as s:
        res = await s.exec(
            select(Review).order_by(Review.id.desc()).limit(10)
        )
        reviews = res.all()

    if not reviews:
        await msg.answer("Пока отзывов нет 😔")
        return

    for r in reviews:
        await msg.answer(
            f"⭐ {r.user_name or 'Клиент'}:\n{r.text}"
        )


@router.message(F.text == "ℹ️ О салоне")
async def show_salon_info(msg: Message):
    async with AsyncSession(engine) as s:
        info = await s.get(SalonInfo, 1)

    text = info.text if info else "Информация о салоне пока не добавлена."

    await msg.answer(
        text,
        reply_markup=reply_kb([["⬅️ Назад"]])
    )

@router.message(F.text == "📸 Наши работы")
async def show_works(msg: Message):
    await msg.answer(
        "📸 Наши работы\n\n"
        "Смотрите примеры работ в нашем Telegram-канале 👇",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="🔗 Перейти в портфолио",
                        url=WORKS_URL
                    )
                ]
            ]
        )
    )


@router.message(F.text == "🛠 Админ")
async def admin_panel(msg: Message):
    if not await is_admin(msg.from_user.id):
        await msg.answer("⛔ Нет доступа")
        return

    await msg.answer(
        "🛠 Админ панель",
        reply_markup=reply_kb([
            ["➕ Добавить мастера"],
            ["➖ Удалить мастера"],
            ["✏️ О салоне"],
            ["⬅️ Назад"]
        ])
    )



# ================= EDIT SALON INFO =================
@router.message(F.text == "✏️ О салоне")
async def admin_edit_salon(msg: Message, state: FSMContext):
    if not await is_admin(msg.from_user.id):
        return

    async with AsyncSession(engine) as s:
        info = await s.get(SalonInfo, 1)
        text = info.text if info else "Информация не задана"

    await msg.answer(
        f"✏️ Текущий текст:\n\n{text}\n\nВведите новый:",
    )
    await state.set_state(SalonEditFSM.text)




@router.message(StateFilter(SalonEditFSM.text))
async def admin_save_salon(msg: Message, state: FSMContext):
    async with AsyncSession(engine) as s:
        info = await s.get(SalonInfo, 1)
        if info:
            info.text = msg.text
        else:
            s.add(SalonInfo(id=1, text=msg.text))
        await s.commit()

    await msg.answer("✅ Обновлено", reply_markup=reply_kb([["⬅️ Назад"]]))
    await state.clear()


# ================= ADD MASTER =================
@router.message(F.text == "➕ Добавить мастера")
async def admin_add_master(msg: Message, state: FSMContext):
    if not await is_admin(msg.from_user.id):
        return

    await msg.answer("Введите Telegram ID мастера:")
    await state.set_state(AdminFSM.add_master)


@router.message(StateFilter(AdminFSM.add_master))
async def admin_add_master_save(msg: Message, state: FSMContext):
    try:
        tg_id = int(msg.text)
    except ValueError:
        await msg.answer("❌ Нужно число")
        return

    async with AsyncSession(engine) as s:
        res = await s.exec(select(User).where(User.telegram_id == tg_id))
        user = res.first()

        if user:
            user.is_master = True
        else:
            s.add(User(telegram_id=tg_id, is_master=True))

        for wd in range(5):
            s.add(MasterWeekday(master_id=tg_id, weekday=wd, is_enabled=True))

        await s.commit()

    await msg.answer("✅ Мастер добавлен", reply_markup=reply_kb([["⬅️ Назад"]]))
    await state.clear()


@router.message(F.text == "➖ Удалить мастера")
async def admin_remove_master(msg: Message, state: FSMContext):
    if not await is_admin(msg.from_user.id):
        return

    await msg.answer("Введите Telegram ID мастера для удаления:")
    await state.set_state(AdminFSM.remove_master)

@router.message(StateFilter(AdminFSM.remove_master))
async def admin_remove_master_save(msg: Message, state: FSMContext):
    try:
        tg_id = int(msg.text)
    except ValueError:
        await msg.answer("❌ Нужно число")
        return

    async with AsyncSession(engine) as s:
        res = await s.exec(
            select(User).where(User.telegram_id == tg_id)
        )
        user = res.first()

        if not user or not user.is_master:
            await msg.answer("❌ Этот пользователь не является мастером")
            return

        # снимаем роль мастера
        user.is_master = False

        # удаляем расписание и дни
        await s.exec(
            delete(MasterSchedule).where(MasterSchedule.master_id == tg_id)
        )
        await s.exec(
            delete(MasterWeekday).where(MasterWeekday.master_id == tg_id)
        )

        await s.commit()

    await msg.answer(
        "✅ Мастер удалён",
        reply_markup=reply_kb([["⬅️ Назад"]])
    )
    await state.clear()




@router.message(F.text == "⬅️ Назад")
async def back(msg: Message, state: FSMContext):
    await state.clear()
    await start(msg)

# ================= TIMEZONE =================
from zoneinfo import ZoneInfo

IRKUTSK_TZ = ZoneInfo("Asia/Irkutsk")


def now_irkutsk() -> datetime:
    return datetime.now(IRKUTSK_TZ)


def is_time_future(date_str: str, time_str: str) -> bool:
    dt = datetime.strptime(
        f"{date_str} {time_str}", "%Y-%m-%d %H:%M"
    ).replace(tzinfo=IRKUTSK_TZ)
    return dt > now_irkutsk()


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
        await msg.answer("❌ Неверный формат")
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


# ================= SELECT DATE =================
@router.callback_query(F.data.startswith("bd:"))
async def booking_date(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()

    if "master" not in data:
        await cb.answer(
            "Сессия записи устарела. Начните запись заново.",
            show_alert=True
        )
        return

    date = cb.data.split(":")[1]

    async with AsyncSession(engine) as s:
        res = await s.exec(
            select(MasterSchedule).where(
                MasterSchedule.master_id == data["master"],
                MasterSchedule.date == date,
                MasterSchedule.is_available == True
            )
        )
        slots = res.all()  # ✅ ВОТ ЭТОГО НЕ ХВАТАЛО

    # ✅ сортировка по времени
    slots = sorted(slots, key=lambda s: s.time)

    valid_slots = [
        (s.time, f"bt:{s.time}")
        for s in slots
        if is_time_future(date, s.time)
    ]

    if not valid_slots:
        await cb.answer("Нет доступного времени", show_alert=True)
        return

    await state.update_data(date=date)

    await cb.message.answer(
        f"⏰ {format_date_ru(date)}",
        reply_markup=inline_kb(valid_slots)
    )
    await state.set_state(BookingFSM.time)


@router.callback_query(F.data.startswith("bt:"))
async def booking_time(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()

    if "master" not in data or "date" not in data:
        await cb.answer(
            "Сессия записи устарела. Начните запись заново.",
            show_alert=True
        )
        return

    time = cb.data.split(":", 1)[1]
    master_id = data["master"]
    date = data["date"]

    async with AsyncSession(engine) as s:
        res = await s.exec(
            select(MasterSchedule).where(
                MasterSchedule.master_id == master_id,
                MasterSchedule.date == date,
                MasterSchedule.time == time
            )
        )
        slot = res.first()

        if not slot:
            await cb.answer("⛔ Это время уже занято", show_alert=True)
            return

        await s.delete(slot)

        booking = Booking(
            chat_id=cb.from_user.id,
            client_name=data["name"],
            phone=data["phone"],
            date=date,
            time=time,
            master_id=master_id,
            status="pending"
        )
        s.add(booking)
        await s.commit()

    # 🔔 мастеру
    await bot.send_message(
        master_id,
        "📅 Новая запись\n\n"
        f"🗓 {format_datetime_ru(date, time)}\n"
        f"👤 {data['name']}\n"
        f"📞 {data['phone']}\n\n"
        "⏳ Ожидает подтверждения"
    )

    # ✅ клиенту
    await bot.send_message(
        cb.from_user.id,
        "⏳ Заявка отправлена мастеру\n\n"
        f"🗓 {format_datetime_ru(date, time)}\n"
        "Мастер подтвердит запись в ближайшее время."
    )

    await cb.answer()
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

@router.message(F.text == "📅 Дни работы")
async def master_weekdays(msg: Message):
    master_id = msg.from_user.id

    # 🔥 гарантируем, что дни есть в БД
    await ensure_master_weekdays(master_id)

    kb = await build_weekdays_keyboard(master_id)

    await msg.answer(
        "📅 Выберите рабочие дни:",
        reply_markup=kb
    )


WEEKDAYS = [
    "Понедельник",
    "Вторник",
    "Среда",
    "Четверг",
    "Пятница",
    "Суббота",
    "Воскресенье"
]

async def build_weekdays_keyboard(master_id: int) -> InlineKeyboardMarkup:
    async with AsyncSession(engine) as s:
        res = await s.exec(
            select(MasterWeekday).where(
                MasterWeekday.master_id == master_id
            )
        )
        days = {d.weekday: d.is_enabled for d in res.all()}

    buttons = []
    for i, name in enumerate(WEEKDAYS):
        enabled = days.get(i, False)
        mark = "✅" if enabled else "❌"
        buttons.append((f"{mark} {name}", f"wd:{i}"))

    return inline_kb(buttons)


@router.callback_query(F.data.startswith("wd:"))
async def toggle_weekday(cb: CallbackQuery):
    weekday = int(cb.data.split(":")[1])
    master_id = cb.from_user.id

    async with AsyncSession(engine) as s:
        res = await s.exec(
            select(MasterWeekday).where(
                MasterWeekday.master_id == master_id,
                MasterWeekday.weekday == weekday
            )
        )
        day = res.first()
        if day:
            day.is_enabled = not day.is_enabled
        await s.commit()

    # ✅ ШАГ 2 ВОТ ЗДЕСЬ
    kb = await build_weekdays_keyboard(master_id)
    await cb.message.edit_reply_markup(reply_markup=kb)
    await cb.answer("Обновлено")


from sqlalchemy import and_

@router.message(F.text == "📋 Мои записи")
async def my_bookings(msg: Message):
    user_id = msg.from_user.id

    async with AsyncSession(engine) as s:
        res = await s.exec(
            select(User).where(User.telegram_id == user_id)
        )
        user = res.first()

        is_master = bool(user and user.is_master)

        # ===== ШАПКА =====
        if is_master:
            await msg.answer(
                "🧑‍🔧 Записи клиентов\n"
                "──────────────"
            )
        else:
            await msg.answer(
                "📋 Мои записи\n"
                "──────────────"
            )

        # ===== МАСТЕР =====
        if is_master:
            res = await s.exec(
                select(Booking)
                .where(
                    and_(
                        Booking.master_id == user_id,
                        Booking.status.in_(["pending", "confirmed"])
                    )
                )
                .order_by(Booking.date, Booking.time)
            )
            bookings = res.all()

            if not bookings:
                await msg.answer(
                    "📭 У вас пока нет записей\n\n"
                    "Когда клиент запишется — запись появится здесь.",
                    reply_markup=reply_kb([["⬅️ Назад"]])
                )
                return

            for b in bookings:
                buttons = []

                if b.status == "pending":
                    buttons.append(("✅ Подтвердить", f"mc:{b.id}"))

                buttons.append(("❌ Отменить", f"mx:{b.id}"))

                await msg.answer(
                    booking_card(
                        f"📅 {format_datetime_ru(b.date, b.time)}\n"
                        f"👤 Клиент: {b.client_name}\n"
                        f"📞 {b.phone}\n"
                        f"📌 Статус: {booking_status_ru(b.status)}"
                    ),
                    reply_markup=inline_kb(buttons)
                )

        # ===== КЛИЕНТ =====
        else:
            res = await s.exec(
                select(Booking, User)
                .join(User, User.telegram_id == Booking.master_id)
                .where(
                    and_(
                        Booking.chat_id == user_id,
                        Booking.status.in_(["pending", "confirmed"])
                    )
                )
                .order_by(Booking.date, Booking.time)
            )
            rows = res.all()

            if not rows:
                await msg.answer(
                    "📭 У вас пока нет активных записей\n\n"
                    "Запишитесь к мастеру в любое удобное время 👇",
                    reply_markup=reply_kb([
                        ["📅 Записаться"],
                        ["⬅️ Назад"]
                    ])
                )
                return

            for b, master in rows:
                await msg.answer(
                    booking_card(
                        f"📅 {format_datetime_ru(b.date, b.time)}\n"
                        f"👨‍🔧 Мастер: {master.name or 'Без имени'}\n"
                        f"📌 Статус: {booking_status_ru(b.status)}"
                    ),
                    reply_markup=inline_kb([
                        ("❌ Отменить запись", f"cx:{b.id}")
                    ])
                )



# ================= MASTER PROFILE EDIT =================

@router.message(F.text == "✏️ Редактировать профиль")
async def master_edit_profile(msg: Message):
    async with AsyncSession(engine) as s:
        res = await s.exec(
            select(User).where(
                User.telegram_id == msg.from_user.id,
                User.is_master == True
            )
        )
        user = res.first()

    if not user:
        await msg.answer("⛔ Нет доступа")
        return

    await msg.answer(
        "✏️ Редактирование профиля",
        reply_markup=reply_kb([
            ["✏️ Изменить имя"],
            ["📞 Изменить телефон"],
            ["⬅️ Назад"]
        ])
    )


@router.message(F.text == "✏️ Изменить имя")
async def master_edit_name(msg: Message, state: FSMContext):
    await msg.answer("Введите новое имя:")
    await state.set_state(MasterEditFSM.name)


@router.message(StateFilter(MasterEditFSM.name))
async def master_save_name(msg: Message, state: FSMContext):
    async with AsyncSession(engine) as s:
        res = await s.exec(
            select(User).where(User.telegram_id == msg.from_user.id)
        )
        user = res.first()
        if user:
            user.name = msg.text
            await s.commit()

    await msg.answer("✅ Имя обновлено", reply_markup=reply_kb([["⬅️ Назад"]]))
    await state.clear()


@router.message(F.text == "📞 Изменить телефон")
async def master_edit_phone(msg: Message, state: FSMContext):
    await msg.answer("Введите телефон (+79999999999):")
    await state.set_state(MasterEditFSM.phone)


@router.message(StateFilter(MasterEditFSM.phone))
async def master_save_phone(msg: Message, state: FSMContext):
    if not re.fullmatch(r"\+\d{10,15}", msg.text):
        await msg.answer("❌ Неверный формат телефона")
        return

    async with AsyncSession(engine) as s:
        res = await s.exec(
            select(User).where(User.telegram_id == msg.from_user.id)
        )
        user = res.first()
        if user:
            user.phone = msg.text
            await s.commit()

    await msg.answer("✅ Телефон обновлён", reply_markup=reply_kb([["⬅️ Назад"]]))
    await state.clear()


@router.callback_query(F.data.startswith("mc:"))
async def master_confirm(cb: CallbackQuery):
    booking_id = int(cb.data.split(":")[1])

    async with AsyncSession(engine) as s:
        res = await s.exec(
            select(Booking).where(Booking.id == booking_id)
        )
        b = res.first()

        if not b:
            await cb.answer("Запись не найдена", show_alert=True)
            return

        if b.status == "confirmed":
            await cb.answer("Уже подтверждена")
            return

        # ✅ СОХРАНЯЕМ ДАННЫЕ ДО commit
        chat_id = b.chat_id
        date = b.date
        time = b.time

        b.status = "confirmed"
        await s.commit()

    # 🔔 уведомляем клиента (ВНЕ сессии, но с сохранёнными данными)
    await bot.send_message(
        chat_id,
        "✅ Ваша запись подтверждена!\n\n"
        f"🗓 {format_datetime_ru(date, time)}"
    )

    await cb.answer("Подтверждено")

@router.callback_query(F.data.startswith("mx:"))
async def master_cancel(cb: CallbackQuery):
    booking_id = int(cb.data.split(":")[1])

    async with AsyncSession(engine) as s:
        res = await s.exec(
            select(Booking).where(
                Booking.id == booking_id,
                Booking.status.in_(["pending", "confirmed"])
            )
        )
        b = res.first()

        if not b:
            await cb.answer("Запись не найдена", show_alert=True)
            return

        chat_id = b.chat_id
        date = b.date
        time = b.time
        master_id = b.master_id

        b.status = "cancelled"

        # возвращаем слот
        s.add(
            MasterSchedule(
                master_id=master_id,
                date=date,
                time=time,
                is_available=True
            )
        )

        await s.commit()

    await cb.message.delete()
    await cb.answer("Запись отменена")

    await bot.send_message(
        chat_id,
        f"❌ Ваша запись отменена мастером\n\n🗓 {format_datetime_ru(date, time)}"
    )


# ================= MASTER SCHEDULE FIX =================

@router.message(F.text == "🕒 Моё расписание")
async def master_schedule(msg: Message):
    await msg.answer(
        "📅 Выберите дату:",
        reply_markup=inline_kb([
            (format_date_ru(d), f"msd:{d}") for d in gen_dates()
        ])
    )


@router.callback_query(F.data.startswith("msd:"))
async def master_schedule_day(cb: CallbackQuery):
    date = cb.data.split(":")[1]

    async with AsyncSession(engine) as s:
        res = await s.exec(
            select(MasterSchedule).where(
                MasterSchedule.master_id == cb.from_user.id,
                MasterSchedule.date == date
            )
        )
        slots = {r.time: r.is_available for r in res.all()}

    buttons = []
    for t in time_slots():
        is_available = slots.get(t, False)
        mark = "✅" if is_available else "❌"
        buttons.append(
            (f"{mark} {t}", f"mst:{date}:{t}")
        )

    await cb.message.edit_text(
        f"🕒 {format_date_ru(date)}",
        reply_markup=inline_kb(buttons)
    )


@router.callback_query(F.data.startswith("mst:"))
async def master_toggle_slot(cb: CallbackQuery):
    _, date, time = cb.data.split(":", 2)

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
            if slot.is_available:
                await s.delete(slot)
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

    # обновляем кнопки
    async with AsyncSession(engine) as s:
        res = await s.exec(
            select(MasterSchedule).where(
                MasterSchedule.master_id == cb.from_user.id,
                MasterSchedule.date == date
            )
        )
        slots = {r.time: r.is_available for r in res.all()}

    buttons = []
    for t in time_slots():
        mark = "✅" if slots.get(t) else "❌"
        buttons.append((f"{mark} {t}", f"mst:{date}:{t}"))

    await cb.message.edit_reply_markup(
        reply_markup=inline_kb(buttons)
    )
    await cb.answer("Обновлено")


# ================= REMINDERS =================
async def reminder_loop():
    while True:
        now = now_irkutsk()

        async with AsyncSession(engine) as s:
            res = await s.exec(
                select(Booking).where(Booking.status == "pending")
            )
            for b in res.all():
                dt = datetime.strptime(
                    f"{b.date} {b.time}", "%Y-%m-%d %H:%M"
                ).replace(tzinfo=IRKUTSK_TZ)

                delta = dt - now

                if not b.reminded_24h and timedelta(hours=24) > delta > timedelta(hours=23, minutes=50):
                    await bot.send_message(b.chat_id, "⏰ Напоминание: визит через 24 часа")
                    b.reminded_24h = True

                if not b.reminded_2h and timedelta(hours=2) > delta > timedelta(hours=1, minutes=50):
                    await bot.send_message(b.chat_id, "⏰ Напоминание: визит через 2 часа")
                    b.reminded_2h = True

            await s.commit()

        await asyncio.sleep(600)


# ================= RUN =================
async def main():
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)

    asyncio.create_task(reminder_loop())

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
