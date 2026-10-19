from __future__ import annotations

import asyncio
import html
import logging
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command, CommandObject, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    ChatMemberUpdated,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    Update,
)
from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

TOKEN = os.getenv("BOT_TOKEN", "8643378046:AAHQEnnuWdQ7qIxj7keXSGOxVAQNFEVdIPo").strip()
DB_PATH = Path(os.getenv("DB_PATH", str(BASE_DIR / "tamosho.sqlite3")))
ADMIN_IDS = {
    int(part.strip())
    for part in os.getenv("ADMIN_IDS", "8837120659").split(",")
    if part.strip().isdigit()
}

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("tamosho")
membership_api_semaphore = asyncio.Semaphore(10)

router = Router()
dp = Dispatcher(storage=MemoryStorage())
dp.include_router(router)
bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML)) if TOKEN else None
bot_username = ""


class AddMovie(StatesGroup):
    waiting_for_video = State()
    waiting_for_title = State()
    waiting_for_episodes = State()
    waiting_for_country = State()
    waiting_for_language = State()
    waiting_for_year = State()
    waiting_for_genre = State()


class EditMovie(StatesGroup):
    waiting_for_value = State()


class AddChannel(StatesGroup):
    waiting_for_channel = State()


class AddAdmin(StatesGroup):
    waiting_for_user_id = State()


class RemoveAdmin(StatesGroup):
    waiting_for_user_id = State()


class PremiumSettings(StatesGroup):
    waiting_for_price = State()
    waiting_for_instructions = State()
    waiting_for_days = State()


class StartSettings(StatesGroup):
    waiting_for_text = State()


class Broadcast(StatesGroup):
    waiting_for_message = State()
    waiting_for_confirmation = State()


class PremiumReceipt(StatesGroup):
    waiting_for_receipt = State()


MOVIE_FIELDS = {
    "code": ("🔢 Kino kodi", "waiting_for_code"),
    "title": ("🎬 Kino nomi", "waiting_for_title"),
    "episodes": ("🎥 Qismlar soni", "waiting_for_episodes"),
    "country": ("🌍 Davlati", "waiting_for_country"),
    "language": ("🗣 Tili", "waiting_for_language"),
    "year": ("📅 Yili", "waiting_for_year"),
    "genre": ("🎭 Janri", "waiting_for_genre"),
}

SKIP_TEXT = "⏭ O‘tkazib yuborish"
CANCEL_TEXT = "❌ Bekor qilish"


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def from_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        result = datetime.fromisoformat(value)
        return result if result.tzinfo else result.replace(tzinfo=UTC)
    except ValueError:
        return None


def plain(value: Any) -> str:
    return html.escape(str(value or ""), quote=False)


def user_keyboard(is_admin: bool = False) -> Any:
    from aiogram.types import KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove

    if not is_admin:
        return ReplyKeyboardRemove()
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="🛠 Admin panel")]],
        resize_keyboard=True,
    )


def cancel_keyboard() -> Any:
    from aiogram.types import KeyboardButton, ReplyKeyboardMarkup

    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=CANCEL_TEXT)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def skip_keyboard() -> Any:
    from aiogram.types import KeyboardButton, ReplyKeyboardMarkup

    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=SKIP_TEXT)],
            [KeyboardButton(text=CANCEL_TEXT)],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def admin_keyboard() -> Any:
    from aiogram.types import KeyboardButton, ReplyKeyboardMarkup

    labels = [
        ["📊 Statistika", "📢 Majburiy obuna"],
        ["📈 Kanal statistikasi", "🎁 Referral statistikasi"],
        ["💎 Premium boshqaruvi", "🎬 Kino qo‘shish"],
        ["🗑 Kino o‘chirish", "✏️ Kino tahrirlash"],
        ["👮 Adminlar", "📝 Start sozlamalari"],
        ["📣 Reklama", "⬅️ Asosiy menyu"],
    ]
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=label) for label in row] for row in labels],
        resize_keyboard=True,
    )


class DatabaseContext:
    def __init__(self) -> None:
        self.connection: aiosqlite.Connection | None = None

    async def __aenter__(self) -> aiosqlite.Connection:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        self.connection = await aiosqlite.connect(DB_PATH)
        self.connection.row_factory = aiosqlite.Row
        await self.connection.execute("PRAGMA foreign_keys = ON")
        return self.connection

    async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if self.connection is not None:
            if exc_type is not None:
                await self.connection.rollback()
            await self.connection.close()


async def connect_db() -> DatabaseContext:
    return DatabaseContext()


async def init_db() -> None:
    async with await connect_db() as db:
        await db.execute("PRAGMA journal_mode = WAL")
        await db.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT NOT NULL DEFAULT '',
                registered_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                referred_by INTEGER,
                premium_until TEXT,
                FOREIGN KEY (referred_by) REFERENCES users(user_id)
            );
            CREATE INDEX IF NOT EXISTS idx_users_referrer ON users(referred_by);

            CREATE TABLE IF NOT EXISTS admins (
                user_id INTEGER PRIMARY KEY,
                added_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS channels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT NOT NULL UNIQUE,
                username TEXT,
                invite_link TEXT,
                title TEXT NOT NULL,
                tracking_started_at TEXT NOT NULL,
                last_update_at TEXT
            );

            CREATE TABLE IF NOT EXISTS member_current (
                channel_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                is_member INTEGER NOT NULL,
                status TEXT NOT NULL,
                last_changed_at TEXT NOT NULL,
                PRIMARY KEY (channel_id, user_id),
                FOREIGN KEY (channel_id) REFERENCES channels(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS channel_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                update_id INTEGER NOT NULL UNIQUE,
                channel_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                event_type TEXT NOT NULL CHECK(event_type IN ('joined', 'left')),
                old_status TEXT NOT NULL,
                new_status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (channel_id) REFERENCES channels(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_channel_events_channel_type
                ON channel_events(channel_id, event_type);

            CREATE TABLE IF NOT EXISTS movies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT,
                title TEXT NOT NULL,
                video_file_id TEXT NOT NULL,
                episodes TEXT,
                country TEXT,
                language TEXT,
                year TEXT,
                genre TEXT,
                added_by INTEGER NOT NULL,
                added_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_movies_title ON movies(title COLLATE NOCASE);

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS premium_payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                receipt_file_id TEXT NOT NULL,
                receipt_type TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK(status IN ('pending', 'approved', 'rejected')),
                created_at TEXT NOT NULL,
                reviewed_at TEXT,
                reviewed_by INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_premium_payments_status
                ON premium_payments(status, created_at);

            CREATE TABLE IF NOT EXISTS premium_subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                approved_by INTEGER NOT NULL,
                approved_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1
            );
            CREATE INDEX IF NOT EXISTS idx_premium_subscriptions_active
                ON premium_subscriptions(user_id, active, expires_at);

            CREATE TABLE IF NOT EXISTS processed_updates (
                update_id INTEGER PRIMARY KEY,
                processed_at TEXT NOT NULL
            );
            """
        )
        async with db.execute("PRAGMA table_info(movies)") as cursor:
            movie_columns = {row["name"] for row in await cursor.fetchall()}
        if "code" not in movie_columns:
            await db.execute("ALTER TABLE movies ADD COLUMN code TEXT")
        async with db.execute(
            "SELECT id FROM movies WHERE code IS NULL OR code = '' ORDER BY id"
        ) as cursor:
            movies_without_codes = await cursor.fetchall()
        for movie in movies_without_codes:
            await db.execute(
                "UPDATE movies SET code = ? WHERE id = ?",
                (str(movie["id"]), movie["id"]),
            )
        await db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_movies_code ON movies(code)"
        )
        defaults = {
            "premium_price": "So‘mda narx admin tomonidan belgilanadi",
            "premium_instructions": "To‘lov yo‘riqnomasi admin tomonidan belgilanadi.",
            "premium_days": "30",
            "start_text": "Kino kodini yozing.",
        }
        for key, value in defaults.items():
            await db.execute(
                "INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)",
                (key, value),
            )
        await db.execute(
            """
            UPDATE settings
            SET value = 'Kino kodini yozing.'
            WHERE key = 'start_text'
              AND value = 'Assalomu alaykum! Tamosho Kino Bot’ga xush kelibsiz.'
            """
        )
        async with db.execute("SELECT COUNT(*) AS n FROM admins") as cursor:
            has_existing_admins = int((await cursor.fetchone())["n"]) > 0
        if not has_existing_admins:
            for admin_id in ADMIN_IDS:
                await db.execute(
                    "INSERT OR IGNORE INTO admins(user_id, added_at) VALUES (?, ?)",
                    (admin_id, now_iso()),
                )
        await db.commit()


async def setting(key: str, fallback: str = "") -> str:
    async with await connect_db() as db:
        async with db.execute("SELECT value FROM settings WHERE key = ?", (key,)) as cursor:
            row = await cursor.fetchone()
    return str(row["value"]) if row else fallback


async def set_setting(key: str, value: str) -> None:
    async with await connect_db() as db:
        await db.execute(
            """
            INSERT INTO settings(key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )
        await db.commit()


async def next_movie_code(db: aiosqlite.Connection) -> str:
    async with db.execute("SELECT code FROM movies") as cursor:
        rows = await cursor.fetchall()
    numeric_codes = [int(row["code"]) for row in rows if row["code"] and str(row["code"]).isdigit()]
    return str(max([9999, *numeric_codes]) + 1)


async def is_admin(user_id: int) -> bool:
    async with await connect_db() as db:
        async with db.execute("SELECT 1 FROM admins WHERE user_id = ?", (user_id,)) as cursor:
            return await cursor.fetchone() is not None


async def is_premium(user_id: int) -> bool:
    current = now_iso()
    async with await connect_db() as db:
        await db.execute(
            "UPDATE premium_subscriptions SET active = 0 WHERE active = 1 AND expires_at <= ?",
            (current,),
        )
        async with db.execute(
            """
            SELECT 1 FROM premium_subscriptions
            WHERE user_id = ? AND active = 1 AND expires_at > ?
            ORDER BY expires_at DESC LIMIT 1
            """,
            (user_id, current),
        ) as cursor:
            result = await cursor.fetchone()
        await db.execute(
            """
            UPDATE users SET premium_until = (
                SELECT MAX(expires_at) FROM premium_subscriptions
                WHERE user_id = ? AND active = 1 AND expires_at > ?
            ) WHERE user_id = ?
            """,
            (user_id, current, user_id),
        )
        await db.commit()
    return result is not None


async def record_user(message: Message, ref_arg: str | None = None) -> bool:
    user = message.from_user
    if user is None:
        return False
    async with await connect_db() as db:
        referrer_id: int | None = None
        if ref_arg and ref_arg.startswith("ref_"):
            raw_id = ref_arg[4:]
            if raw_id.isdigit():
                candidate = int(raw_id)
                if candidate != user.id:
                    async with db.execute(
                        "SELECT user_id FROM users WHERE user_id = ?", (candidate,)
                    ) as cursor:
                        if await cursor.fetchone():
                            referrer_id = candidate
        stamp = now_iso()
        insert_cursor = await db.execute(
            """
            INSERT OR IGNORE INTO users(
                user_id, username, first_name, registered_at, last_seen_at, referred_by
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (user.id, user.username, user.first_name or "", stamp, stamp, referrer_id),
        )
        is_new = insert_cursor.rowcount == 1
        if not is_new:
            await db.execute(
                "UPDATE users SET username = ?, first_name = ?, last_seen_at = ? WHERE user_id = ?",
                (user.username, user.first_name or "", stamp, user.id),
            )
        await db.commit()
    return is_new


async def fetch_channels() -> list[aiosqlite.Row]:
    async with await connect_db() as db:
        async with db.execute("SELECT * FROM channels ORDER BY title COLLATE NOCASE") as cursor:
            return await cursor.fetchall()


def member_status_is_active(member: Any) -> bool:
    status = getattr(member, "status", "")
    if status in {"member", "administrator", "creator"}:
        return True
    return status == "restricted" and bool(getattr(member, "is_member", False))


async def user_has_required_subscriptions(
    user_id: int,
    channels: list[aiosqlite.Row] | None = None,
) -> bool:
    if channels is None:
        channels = await fetch_channels()
    if not channels:
        return True

    async def check_channel(channel: aiosqlite.Row) -> bool:
        async with membership_api_semaphore:
            try:
                member = await bot.get_chat_member(  # type: ignore[union-attr]
                    int(channel["chat_id"]),
                    user_id,
                )
            except (TelegramAPIError, ValueError):
                return False
            return member_status_is_active(member)

    results = await asyncio.gather(*(check_channel(channel) for channel in channels))
    return all(results)


async def subscription_keyboard(
    channels: list[aiosqlite.Row] | None = None,
) -> InlineKeyboardMarkup:
    if channels is None:
        channels = await fetch_channels()
    rows: list[list[InlineKeyboardButton]] = []
    for channel in channels:
        link = channel["invite_link"]
        username = channel["username"]
        if not link and username:
            link = f"https://t.me/{str(username).lstrip('@')}"
        if link:
            rows.append(
                [
                    InlineKeyboardButton(
                        text=f"📢 {str(channel['title'])[:55]}",
                        url=str(link),
                    )
                ]
            )
    rows.append([InlineKeyboardButton(text="✅ Obunani tekshirish", callback_data="membership:check")])
    rows.append([InlineKeyboardButton(text="💎 PREMIUM", callback_data="premium:info")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def show_subscription_prompt(
    message: Message,
    reset_reply_keyboard: bool = True,
    channels: list[aiosqlite.Row] | None = None,
) -> None:
    if reset_reply_keyboard:
        await message.answer(
            "Kino kodini yozing.",
            reply_markup=user_keyboard(await is_admin_message(message)),
        )
    if channels is None:
        channels = await fetch_channels()
    names = "\n".join(f"• {plain(channel['title'])}" for channel in channels)
    text = "Kino kodini olish uchun quyidagi majburiy kanallarga obuna bo‘ling:"
    if names:
        text += f"\n\n{names}"
    await message.answer(text, reply_markup=await subscription_keyboard(channels))


async def membership_message(callback: CallbackQuery) -> None:
    if callback.from_user and await is_premium(callback.from_user.id):
        await callback.answer("Sizda Premium faol.", show_alert=True)
        if callback.message:
            await callback.message.answer(
                "Premium faol. Kino kodini yozing.",
                reply_markup=user_keyboard(await is_admin(callback.from_user.id)),
            )
        return
    if callback.from_user and await user_has_required_subscriptions(callback.from_user.id):
        await callback.answer("Obuna tasdiqlandi.")
        if callback.message:
            await callback.message.answer(
                "Obuna tasdiqlandi. Kino kodini yozing.",
                reply_markup=user_keyboard(await is_admin(callback.from_user.id)),
            )
        return
    await callback.answer("Majburiy kanallarga obuna bo‘ling.", show_alert=True)


@router.callback_query(F.data == "membership:check")
async def check_membership(callback: CallbackQuery) -> None:
    await membership_message(callback)


def premium_info_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🧾 Chek yuborish", callback_data="premium:receipt")],
            [InlineKeyboardButton(text="⬅️ Orqaga", callback_data="premium:back")],
        ]
    )


@router.callback_query(F.data == "premium:info")
async def premium_info_callback(callback: CallbackQuery) -> None:
    await callback.answer()
    await send_premium_info(callback)


@router.callback_query(F.data == "premium:back")
async def premium_back_callback(callback: CallbackQuery) -> None:
    await callback.answer()
    if callback.message:
        await show_subscription_prompt(callback.message)


async def send_premium_info(callback_or_message: CallbackQuery | Message) -> None:
    price = await setting("premium_price")
    instructions = await setting("premium_instructions")
    days = await setting("premium_days", "30")
    text = (
        f"💎 <b>PREMIUM OBUNA</b>\n\n"
        f"Narxi: {plain(price)}\n"
        f"Muddati: {plain(days)} kun\n\n"
        f"To‘lov yo‘riqnomasi:\n{plain(instructions)}\n\n"
        "To‘lovni admin tasdiqlagandan keyingina Premium yoqiladi. "
        "To‘lov chekining rasmini yoki faylini yuboring."
    )
    if isinstance(callback_or_message, CallbackQuery):
        if callback_or_message.message:
            await callback_or_message.message.answer(text, reply_markup=premium_info_keyboard())
    else:
        await callback_or_message.answer(text, reply_markup=premium_info_keyboard())


@router.message(Command("premium"))
async def premium_command(message: Message) -> None:
    await send_premium_info(message)


@router.callback_query(F.data == "premium:receipt")
async def start_receipt(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    if await is_premium(callback.from_user.id):
        await callback.message.answer("Sizda Premium obuna allaqachon faol.")  # type: ignore[union-attr]
        return
    await state.set_state(PremiumReceipt.waiting_for_receipt)
    await callback.message.answer(  # type: ignore[union-attr]
        "To‘lov chekini rasm yoki hujjat ko‘rinishida yuboring.",
        reply_markup=cancel_keyboard(),
    )


async def is_admin_message(message: Message) -> bool:
    return bool(message.from_user and await is_admin(message.from_user.id))


async def is_admin_callback(callback: CallbackQuery) -> bool:
    return await is_admin(callback.from_user.id)


async def admin_only(callback: CallbackQuery) -> bool:
    if await is_admin_callback(callback):
        return True
    await callback.answer("Bu bo‘lim faqat adminlar uchun.", show_alert=True)
    return False


@router.message(Command("cancel"))
@router.message(F.text == CANCEL_TEXT)
async def cancel_action(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Amal bekor qilindi.", reply_markup=user_keyboard(await is_admin_message(message)))


@router.message(Command("start"))
async def start_handler(message: Message, command: CommandObject, state: FSMContext) -> None:
    await state.clear()
    is_new = await record_user(message, command.args)
    if is_new and command.args and command.args.startswith("ref_"):
        logger.info("A new user registered via a referral.")
    is_admin_user = await is_admin_message(message)
    greeting = await setting(
        "start_text",
        "Kino kodini yozing.",
    )
    await message.answer(plain(greeting), reply_markup=user_keyboard(is_admin_user))
    if message.from_user and not await is_premium(message.from_user.id):
        channels = await fetch_channels()
        if not await user_has_required_subscriptions(message.from_user.id, channels):
            await show_subscription_prompt(
                message,
                reset_reply_keyboard=False,
                channels=channels,
            )


@router.message(Command("admin"))
async def admin_command(message: Message) -> None:
    if not await is_admin_message(message):
        await message.answer("Admin bo‘limidan foydalanish huquqi yo‘q.")
        return
    await message.answer("🛠 Admin panel", reply_markup=admin_keyboard())


@router.message(F.text == "⬅️ Asosiy menyu")
async def back_to_user_menu(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "Asosiy menyu.",
        reply_markup=user_keyboard(await is_admin_message(message)),
    )


@router.message(F.text == "🎁 Do‘stlarni taklif qilish")
@router.message(Command("referral"))
async def referral_screen(message: Message) -> None:
    user = message.from_user
    if user is None:
        return
    if not await is_admin(user.id):
        channels = await fetch_channels()
        if not await user_has_required_subscriptions(user.id, channels):
            await show_subscription_prompt(message, channels=channels)
            return
    if not bot_username:
        await message.answer("Referral havolasi vaqtincha mavjud emas.")
        return
    link = f"https://t.me/{bot_username}?start=ref_{user.id}"
    async with await connect_db() as db:
        async with db.execute(
            "SELECT COUNT(*) AS total FROM users WHERE referred_by = ?", (user.id,)
        ) as cursor:
            row = await cursor.fetchone()
        async with db.execute(
            """
            SELECT user_id, username, first_name, registered_at
            FROM users WHERE referred_by = ?
            ORDER BY registered_at DESC LIMIT 10
            """,
            (user.id,),
        ) as cursor:
            invited_users = await cursor.fetchall()
    total = int(row["total"]) if row else 0
    invited_lines = ["\n👥 <b>Taklif qilgan foydalanuvchilar</b>"]
    for invited in invited_users:
        name = invited["first_name"] or invited["username"] or "Noma’lum"
        invited_lines.append(
            f"• <code>{invited['user_id']}</code> · {plain(name)} — "
            "botga kirgan; blok holatini aniqlab bo‘lmaydi"
        )
    if not invited_users:
        invited_lines.append("Hali taklif orqali kirgan foydalanuvchi yo‘q.")
    rows: list[list[InlineKeyboardButton]] = []
    if total > 10:
        rows.append(
            [
                InlineKeyboardButton(
                    text="Taklif qilinganlarni ko‘rish ➡️",
                    callback_data=f"myref:{user.id}:1",
                )
            ]
        )
    await message.answer(
        "🎁 <b>REFERRAL</b>\n\n"
        f"🔗 Sizning havolangiz:\n<code>{plain(link)}</code>\n\n"
        f"👥 Taklif qilganlaringiz: {total}\n"
        f"✅ Botga muvaffaqiyatli kirganlar: {total}\n\n"
        "Telegram foydalanuvchi botni bloklaganini yoki foydalanishni to‘xtatganini "
        "ishonchli ko‘rsatmaydi; bu yerda chiqib ketish statistikasi yuritilmaydi.\n\n"
        f"Do‘stlaringizga yuborish uchun:\n"
        f"<a href=\"{plain(link)}\">Tamosho Kino Bot’ga qo‘shiling</a>\n"
        + "\n".join(invited_lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows) if rows else None,
    )


@router.callback_query(F.data.startswith("myref:"))
async def my_referrals_page(callback: CallbackQuery) -> None:
    try:
        _, raw_owner, raw_page = (callback.data or "").split(":", 2)
        owner_id, page = int(raw_owner), max(0, int(raw_page))
    except (ValueError, TypeError):
        await callback.answer("Ro‘yxatni ochib bo‘lmadi.", show_alert=True)
        return
    if owner_id != callback.from_user.id:
        await callback.answer("Bu ro‘yxat sizga tegishli emas.", show_alert=True)
        return
    page_size = 10
    async with await connect_db() as db:
        async with db.execute(
            "SELECT COUNT(*) AS n FROM users WHERE referred_by = ?", (owner_id,)
        ) as cursor:
            total = int((await cursor.fetchone())["n"])
        async with db.execute(
            """
            SELECT user_id, username, first_name, registered_at
            FROM users WHERE referred_by = ?
            ORDER BY registered_at DESC LIMIT ? OFFSET ?
            """,
            (owner_id, page_size, page * page_size),
        ) as cursor:
            invited_users = await cursor.fetchall()
    lines = [f"👥 <b>Taklif qilinganlar</b> · {page + 1}-sahifa"]
    for invited in invited_users:
        name = invited["first_name"] or invited["username"] or "Noma’lum"
        lines.append(
            f"• <code>{invited['user_id']}</code> · {plain(name)} — "
            "botga kirgan; blok holatini aniqlab bo‘lmaydi"
        )
    buttons: list[InlineKeyboardButton] = []
    if page > 0:
        buttons.append(
            InlineKeyboardButton(
                text="⬅️ Oldingi",
                callback_data=f"myref:{owner_id}:{page - 1}",
            )
        )
    if (page + 1) * page_size < total:
        buttons.append(
            InlineKeyboardButton(
                text="Keyingi ➡️",
                callback_data=f"myref:{owner_id}:{page + 1}",
            )
        )
    await callback.answer()
    if callback.message:
        await callback.message.answer(
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[buttons]) if buttons else None,
        )


@router.message(F.text == "🎬 Kino kodi")
async def prompt_movie_code(message: Message) -> None:
    if message.from_user is None:
        return
    if not await is_premium(message.from_user.id):
        channels = await fetch_channels()
        if not await user_has_required_subscriptions(message.from_user.id, channels):
            await show_subscription_prompt(message, channels=channels)
            return
    await message.answer("Kino kodini yozing.")


@router.message(PremiumReceipt.waiting_for_receipt, F.photo)
async def receive_receipt_photo(message: Message, state: FSMContext) -> None:
    await save_receipt(message, state, message.photo[-1].file_id, "photo")


@router.message(PremiumReceipt.waiting_for_receipt, F.document)
async def receive_receipt_document(message: Message, state: FSMContext) -> None:
    await save_receipt(message, state, message.document.file_id, "document")  # type: ignore[union-attr]


async def save_receipt(
    message: Message,
    state: FSMContext,
    file_id: str,
    receipt_type: str,
) -> None:
    user = message.from_user
    if user is None:
        return
    async with await connect_db() as db:
        async with db.execute(
            "SELECT id FROM premium_payments WHERE user_id = ? AND status = 'pending' LIMIT 1",
            (user.id,),
        ) as cursor:
            pending = await cursor.fetchone()
        if pending:
            await state.clear()
            await message.answer("Chekingiz admin ko‘rib chiqishi uchun navbatda.")
            return
        cursor = await db.execute(
            """
            INSERT INTO premium_payments(user_id, receipt_file_id, receipt_type, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (user.id, file_id, receipt_type, now_iso()),
        )
        payment_id = cursor.lastrowid
        await db.commit()
    await state.clear()
    await message.answer("Chek yuborildi. Admin tekshirganidan so‘ng sizga xabar beramiz.")
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Tasdiqlash",
                    callback_data=f"payment:approve:{payment_id}",
                ),
                InlineKeyboardButton(
                    text="❌ Rad etish",
                    callback_data=f"payment:reject:{payment_id}",
                ),
            ]
        ]
    )
    admin_text = (
        f"💎 Premium to‘lovi #{payment_id}\n"
        f"Foydalanuvchi ID: <code>{user.id}</code>\n"
        f"Username: @{plain(user.username) if user.username else 'yo‘q'}"
    )
    delivered = 0
    async with await connect_db() as db:
        async with db.execute("SELECT user_id FROM admins") as cursor:
            admins = await cursor.fetchall()
    for admin in admins:
        try:
            if receipt_type == "photo":
                await bot.send_photo(  # type: ignore[union-attr]
                    int(admin["user_id"]),
                    file_id,
                    caption=admin_text,
                    reply_markup=keyboard,
                )
            else:
                await bot.send_document(  # type: ignore[union-attr]
                    int(admin["user_id"]),
                    file_id,
                    caption=admin_text,
                    reply_markup=keyboard,
                )
            delivered += 1
        except TelegramAPIError:
            logger.warning("Could not deliver a premium receipt to an admin.")
    if delivered == 0:
        logger.error("Pending payment receipt %s could not be delivered to any admin.", payment_id)


@router.callback_query(F.data.startswith("payment:"))
async def review_payment(callback: CallbackQuery) -> None:
    if not await admin_only(callback):
        return
    try:
        _, decision, raw_id = (callback.data or "").split(":", 2)
        payment_id = int(raw_id)
    except (ValueError, TypeError):
        await callback.answer("So‘rov noto‘g‘ri.", show_alert=True)
        return
    if decision not in {"approve", "reject"}:
        await callback.answer("Amal topilmadi.", show_alert=True)
        return
    async with await connect_db() as db:
        await db.execute("BEGIN IMMEDIATE")
        async with db.execute(
            "SELECT * FROM premium_payments WHERE id = ?", (payment_id,)
        ) as cursor:
            payment = await cursor.fetchone()
        if not payment or payment["status"] != "pending":
            await db.rollback()
            await callback.answer("Bu to‘lov allaqachon ko‘rib chiqilgan.", show_alert=True)
            return
        new_status = "approved" if decision == "approve" else "rejected"
        update_cursor = await db.execute(
            """
            UPDATE premium_payments
            SET status = ?, reviewed_at = ?, reviewed_by = ?
            WHERE id = ? AND status = 'pending'
            """,
            (new_status, now_iso(), callback.from_user.id, payment_id),
        )
        if update_cursor.rowcount != 1:
            await db.rollback()
            await callback.answer("Bu to‘lov allaqachon ko‘rib chiqilgan.", show_alert=True)
            return
        expires_at: str | None = None
        if decision == "approve":
            try:
                days = max(1, int(await setting("premium_days", "30")))
            except ValueError:
                days = 30
            expires_at = (datetime.now(UTC) + timedelta(days=days)).isoformat(timespec="seconds")
            await db.execute(
                """
                INSERT INTO premium_subscriptions(user_id, approved_by, approved_at, expires_at, active)
                VALUES (?, ?, ?, ?, 1)
                """,
                (payment["user_id"], callback.from_user.id, now_iso(), expires_at),
            )
            await db.execute(
                "UPDATE users SET premium_until = ? WHERE user_id = ?",
                (expires_at, payment["user_id"]),
            )
        await db.commit()
    await callback.answer("To‘lov tasdiqlandi." if decision == "approve" else "To‘lov rad etildi.")
    if callback.message:
        await callback.message.edit_reply_markup(reply_markup=None)
    if decision == "approve":
        await bot.send_message(  # type: ignore[union-attr]
            payment["user_id"],
            f"✅ To‘lov tasdiqlandi. Premium {plain(expires_at)} gacha faol.",
        )
    else:
        await bot.send_message(  # type: ignore[union-attr]
            payment["user_id"],
            "To‘lov cheki rad etildi. Yordam uchun admin bilan bog‘laning.",
        )


@router.message(F.text == "🛠 Admin panel")
async def show_admin_panel(message: Message) -> None:
    if not await is_admin_message(message):
        await message.answer("Bu bo‘lim faqat adminlar uchun.")
        return
    await message.answer("🛠 Admin panel", reply_markup=admin_keyboard())


@router.message(F.text == "📊 Statistika")
async def admin_stats(message: Message) -> None:
    if not await is_admin_message(message):
        return
    async with await connect_db() as db:
        async with db.execute("SELECT COUNT(*) AS n FROM users") as cursor:
            users = int((await cursor.fetchone())["n"])
        async with db.execute("SELECT COUNT(*) AS n FROM movies") as cursor:
            movies = int((await cursor.fetchone())["n"])
        async with db.execute(
            "SELECT COUNT(*) AS n FROM premium_subscriptions WHERE active = 1 AND expires_at > ?",
            (now_iso(),),
        ) as cursor:
            premiums = int((await cursor.fetchone())["n"])
        async with db.execute(
            "SELECT COUNT(*) AS n FROM premium_payments WHERE status = 'pending'"
        ) as cursor:
            pending = int((await cursor.fetchone())["n"])
        async with db.execute("SELECT COUNT(*) AS n FROM channels") as cursor:
            channels = int((await cursor.fetchone())["n"])
    await message.answer(
        "📊 <b>BOT STATISTIKASI</b>\n\n"
        f"👥 Foydalanuvchilar: {users}\n"
        f"🎬 Kinolar: {movies}\n"
        f"💎 Faol Premium: {premiums}\n"
        f"🧾 Tekshirilmagan cheklar: {pending}\n"
        f"📢 Majburiy kanallar: {channels}"
    )


def channels_keyboard(channels: list[aiosqlite.Row]) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=f"🗑 {str(channel['title'])[:42]}",
                callback_data=f"channel:remove:{channel['id']}",
            )
        ]
        for channel in channels
    ]
    rows.extend(
        [
            [InlineKeyboardButton(text="➕ Kanal qo‘shish", callback_data="channel:add")],
            [InlineKeyboardButton(text="🔄 Yangilash", callback_data="channel:list")],
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(F.text == "📢 Majburiy obuna")
async def required_channels_admin(message: Message) -> None:
    if not await is_admin_message(message):
        return
    channels = await fetch_channels()
    if not channels:
        text = "Majburiy obuna kanallari hali qo‘shilmagan."
    else:
        lines = ["📢 <b>MAJBURIY OBUNA KANALLARI</b>"]
        for channel in channels:
            status = await channel_admin_status(channel["chat_id"])
            link = channel["invite_link"] or (
                f"https://t.me/{str(channel['username']).lstrip('@')}"
                if channel["username"]
                else "Taklif havolasi kiritilmagan"
            )
            lines.append(
                f"\n• {plain(channel['title'])}\n"
                f"ID/username: <code>{plain(channel['username'] or channel['chat_id'])}</code>\n"
                f"Bot holati: {plain(status)}\n"
                f"Obuna havolasi: {plain(link)}"
            )
        text = "\n".join(lines)
    await message.answer(text, reply_markup=channels_keyboard(channels))


async def channel_admin_status(chat_id: str) -> str:
    try:
        member = await bot.get_chat_member(int(chat_id), (await bot.get_me()).id)  # type: ignore[union-attr]
        if member.status in {"administrator", "creator"}:
            return "Bot admin; a’zolik update’larini olish mumkin"
        return f"Bot admin emas ({member.status})"
    except (TelegramAPIError, ValueError) as exc:
        logger.info("Could not verify bot role for a required channel: %s", type(exc).__name__)
        return "Tekshirib bo‘lmadi — kanal ID-si, bot huquqlari yoki kanal mavjudligini tekshiring"


@router.callback_query(F.data == "channel:list")
async def refresh_channels_callback(callback: CallbackQuery) -> None:
    if not await admin_only(callback):
        return
    channels = await fetch_channels()
    await callback.answer()
    if callback.message:
        await callback.message.answer(
            "Majburiy obuna kanallari:",
            reply_markup=channels_keyboard(channels),
        )


@router.callback_query(F.data == "channel:add")
async def add_channel_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not await admin_only(callback):
        return
    await state.set_state(AddChannel.waiting_for_channel)
    await callback.answer()
    await callback.message.answer(  # type: ignore[union-attr]
        "Kanal username’i yoki ID’sini yuboring.\n"
        "Yopiq kanal uchun: <code>-1001234567890 | https://t.me/+taklif</code>\n"
        "Bot kanal administratori bo‘lishi kerak.",
        reply_markup=cancel_keyboard(),
    )


@router.message(AddChannel.waiting_for_channel, F.text)
async def add_channel_receive(message: Message, state: FSMContext) -> None:
    if not await is_admin_message(message):
        return
    raw = (message.text or "").strip()
    target, _, invite = raw.partition("|")
    target = target.strip()
    invite = invite.strip()
    if invite and not invite.startswith(("https://t.me/", "http://t.me/")):
        await message.answer("Taklif havolasi https://t.me/ bilan boshlanishi kerak.")
        return
    try:
        chat = await bot.get_chat(target)  # type: ignore[union-attr]
        me = await bot.get_me()  # type: ignore[union-attr]
        try:
            bot_member = await bot.get_chat_member(chat.id, me.id)  # type: ignore[union-attr]
            bot_is_admin = bot_member.status in {"administrator", "creator"}
        except TelegramAPIError:
            bot_is_admin = False
    except (TelegramAPIError, ValueError) as exc:
        logger.info("Channel could not be added: %s", type(exc).__name__)
        await message.answer(
            "Kanalni topib bo‘lmadi. Username/ID’ni tekshiring va botni kanalga "
            "qo‘shib, administrator qiling; keyin qayta urinib ko‘ring."
        )
        return
    username = getattr(chat, "username", None)
    if not invite and username:
        invite = f"https://t.me/{username}"
    try:
        async with await connect_db() as db:
            await db.execute(
                """
                INSERT INTO channels(chat_id, username, invite_link, title, tracking_started_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (str(chat.id), username, invite or None, chat.title or str(chat.id), now_iso()),
            )
            await db.commit()
    except aiosqlite.IntegrityError:
        await message.answer("Bu kanal allaqachon qo‘shilgan.")
        return
    await state.clear()
    status_note = (
        "Bot kanal administratori ekanligi tasdiqlandi."
        if bot_is_admin
        else "⚠️ Bot kanal administratori emas yoki a’zolik ma’lumotini tekshira olmadi. "
        "Majburiy obuna tekshiruvi va chat_member kuzatuvi ishlashi uchun botni admin qiling."
    )
    await message.answer(
        f"✅ {plain(chat.title or chat.id)} qo‘shildi.\n"
        f"{status_note}\n"
        "Kuzatuv hozirdan boshlanadi; undan oldingi obuna tarixi mavjud emas.",
        reply_markup=admin_keyboard(),
    )


@router.callback_query(F.data.startswith("channel:remove:"))
async def remove_channel_callback(callback: CallbackQuery) -> None:
    if not await admin_only(callback):
        return
    try:
        channel_id = int((callback.data or "").rsplit(":", 1)[1])
    except (ValueError, IndexError):
        await callback.answer("Kanal topilmadi.", show_alert=True)
        return
    async with await connect_db() as db:
        await db.execute("DELETE FROM channels WHERE id = ?", (channel_id,))
        await db.commit()
    await callback.answer("Kanal o‘chirildi.")
    if callback.message:
        await callback.message.answer("Kanal majburiy obunalar ro‘yxatidan o‘chirildi.")


async def channel_statistics_text(channel: aiosqlite.Row) -> str:
    async with await connect_db() as db:
        async with db.execute(
            "SELECT COUNT(*) AS n FROM member_current WHERE channel_id = ? AND is_member = 1",
            (channel["id"],),
        ) as cursor:
            observed_current = int((await cursor.fetchone())["n"])
        async with db.execute(
            "SELECT event_type, COUNT(*) AS n FROM channel_events WHERE channel_id = ? GROUP BY event_type",
            (channel["id"],),
        ) as cursor:
            events = {row["event_type"]: int(row["n"]) for row in await cursor.fetchall()}
    try:
        current = str(await bot.get_chat_member_count(int(channel["chat_id"])))  # type: ignore[union-attr]
    except (TelegramAPIError, ValueError):
        current = "Ma’lumot mavjud emas"
    return (
        f"📢 <b>Kanal:</b> {plain(channel['title'])}\n"
        f"🔗 Username/ID: <code>{plain(channel['username'] or channel['chat_id'])}</code>\n"
        f"👥 Hozirgi obunachilar: {plain(current)}\n"
        f"🧮 Kuzatilgan hozirgi obunachilar: {observed_current}\n"
        f"➕ Kuzatilgan yangi obunalar: {events.get('joined', 0)}\n"
        f"➖ Kuzatilgan chiqib ketishlar: {events.get('left', 0)}\n"
        f"🛡 Bot holati: {plain(await channel_admin_status(channel['chat_id']))}\n"
        f"🕒 Kuzatish boshlandi: {plain(channel['tracking_started_at'])}\n"
        f"🔄 Oxirgi update: {plain(channel['last_update_at'] or 'Hali kuzatilmagan')}\n"
        "Eslatma: Telegram kuzatuv boshlanishidan oldingi a’zolik tarixini bermaydi."
    )


@router.message(F.text == "📈 Kanal statistikasi")
async def channel_stats(message: Message) -> None:
    if not await is_admin_message(message):
        return
    channels = await fetch_channels()
    if not channels:
        await message.answer("Majburiy obuna kanallari qo‘shilmagan.")
        return
    for channel in channels:
        await message.answer(await channel_statistics_text(channel))


@router.message(F.text == "🎁 Referral statistikasi")
async def referral_stats_start(message: Message) -> None:
    if not await is_admin_message(message):
        return
    await send_referral_stats_page(message, 0)


async def send_referral_stats_page(
    target: Message | CallbackQuery,
    page: int,
) -> None:
    page_size = 15
    async with await connect_db() as db:
        async with db.execute("SELECT COUNT(*) AS n FROM users") as cursor:
            total = int((await cursor.fetchone())["n"])
        async with db.execute(
            """
            SELECT u.user_id, u.username, u.first_name, COUNT(r.user_id) AS invited
            FROM users u
            LEFT JOIN users r ON r.referred_by = u.user_id
            GROUP BY u.user_id
            ORDER BY invited DESC, u.user_id ASC
            LIMIT ? OFFSET ?
            """,
            (page_size, page * page_size),
        ) as cursor:
            rows = await cursor.fetchall()
    lines = [
        f"🎁 <b>REFERRAL STATISTIKASI</b> — {page + 1}-sahifa",
        "Bot bloklangani yoki undan foydalanish to‘xtatilgani aniqlanmaydi.",
        "",
    ]
    for row in rows:
        lines.append(
            f"ID <code>{row['user_id']}</code> · "
            f"{plain(row['first_name'] or row['username'] or 'Noma’lum')} · "
            f"taklif qilganlar: <b>{row['invited']}</b> · "
            f"jami referral: <b>{row['invited']}</b>"
        )
    page_count = max(1, (total + page_size - 1) // page_size)
    controls: list[InlineKeyboardButton] = []
    if page > 0:
        controls.append(
            InlineKeyboardButton(text="⬅️ Oldingi", callback_data=f"referrals:{page - 1}")
        )
    if page + 1 < page_count:
        controls.append(
            InlineKeyboardButton(text="Keyingi ➡️", callback_data=f"referrals:{page + 1}")
        )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[controls]) if controls else None
    text = "\n".join(lines)
    if isinstance(target, CallbackQuery):
        if target.message:
            await target.message.edit_text(text, reply_markup=keyboard)
    else:
        await target.answer(text, reply_markup=keyboard)


@router.callback_query(F.data.startswith("referrals:"))
async def referral_stats_page_callback(callback: CallbackQuery) -> None:
    if not await admin_only(callback):
        return
    try:
        page = max(0, int((callback.data or "").split(":", 1)[1]))
    except (ValueError, IndexError):
        page = 0
    await callback.answer()
    await send_referral_stats_page(callback, page)


@router.message(F.text == "💎 Premium boshqaruvi")
async def premium_admin(message: Message) -> None:
    if not await is_admin_message(message):
        return
    async with await connect_db() as db:
        async with db.execute(
            "SELECT COUNT(*) AS n FROM premium_payments WHERE status = 'pending'"
        ) as cursor:
            pending = int((await cursor.fetchone())["n"])
        async with db.execute(
            "SELECT COUNT(*) AS n FROM premium_subscriptions WHERE active = 1 AND expires_at > ?",
            (now_iso(),),
        ) as cursor:
            active = int((await cursor.fetchone())["n"])
    price = await setting("premium_price")
    days = await setting("premium_days", "30")
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🧾 Kutayotgan cheklar", callback_data="payments:list:0")],
            [InlineKeyboardButton(text="💵 Narxni sozlash", callback_data="premium:set:price")],
            [InlineKeyboardButton(text="📜 Yo‘riqnomani sozlash", callback_data="premium:set:instructions")],
            [InlineKeyboardButton(text="⏳ Muddatni sozlash", callback_data="premium:set:days")],
        ]
    )
    await message.answer(
        "💎 <b>PREMIUM BOSHQARUVI</b>\n\n"
        f"Narx: {plain(price)}\n"
        f"Muddati: {plain(days)} kun\n"
        f"Faol obunalar: {active}\n"
        f"Tekshirilmagan cheklar: {pending}\n\n"
        "Premium faqat admin chekni tasdiqlagandan keyin beriladi.",
        reply_markup=keyboard,
    )


@router.callback_query(F.data.startswith("premium:set:"))
async def premium_setting_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not await admin_only(callback):
        return
    field = (callback.data or "").split(":")[-1]
    prompts = {
        "price": ("Narxni yuboring (masalan: 25 000 so‘m).", PremiumSettings.waiting_for_price),
        "instructions": ("To‘lov yo‘riqnomasini yuboring.", PremiumSettings.waiting_for_instructions),
        "days": ("Premium necha kun faol bo‘lishini yuboring (1–3650).", PremiumSettings.waiting_for_days),
    }
    if field not in prompts:
        await callback.answer("Sozlama topilmadi.", show_alert=True)
        return
    prompt, next_state = prompts[field]
    await state.set_state(next_state)
    await callback.answer()
    await callback.message.answer(prompt, reply_markup=cancel_keyboard())  # type: ignore[union-attr]


@router.message(PremiumSettings.waiting_for_price, F.text)
async def set_premium_price(message: Message, state: FSMContext) -> None:
    if not await is_admin_message(message):
        return
    value = (message.text or "").strip()
    if len(value) > 100:
        await message.answer("Narx 100 belgidan oshmasin.")
        return
    await set_setting("premium_price", value)
    await state.clear()
    await message.answer("Premium narxi saqlandi.", reply_markup=admin_keyboard())


@router.message(PremiumSettings.waiting_for_instructions, F.text)
async def set_premium_instructions(message: Message, state: FSMContext) -> None:
    if not await is_admin_message(message):
        return
    value = (message.text or "").strip()
    if not value or len(value) > 2500:
        await message.answer("Yo‘riqnoma 1–2500 belgi bo‘lishi kerak.")
        return
    await set_setting("premium_instructions", value)
    await state.clear()
    await message.answer("To‘lov yo‘riqnomasi saqlandi.", reply_markup=admin_keyboard())


@router.message(PremiumSettings.waiting_for_days, F.text)
async def set_premium_days(message: Message, state: FSMContext) -> None:
    if not await is_admin_message(message):
        return
    raw = (message.text or "").strip()
    if not raw.isdigit() or not 1 <= int(raw) <= 3650:
        await message.answer("1 dan 3650 gacha bo‘lgan butun son kiriting.")
        return
    await set_setting("premium_days", raw)
    await state.clear()
    await message.answer("Premium muddati saqlandi.", reply_markup=admin_keyboard())


@router.callback_query(F.data.startswith("payments:list:"))
async def pending_payments_page(callback: CallbackQuery) -> None:
    if not await admin_only(callback):
        return
    try:
        offset = max(0, int((callback.data or "").split(":")[-1]))
    except ValueError:
        offset = 0
    async with await connect_db() as db:
        async with db.execute(
            """
            SELECT * FROM premium_payments
            WHERE status = 'pending' ORDER BY created_at ASC LIMIT 10 OFFSET ?
            """,
            (offset,),
        ) as cursor:
            payments = await cursor.fetchall()
    await callback.answer()
    if not payments:
        await callback.message.answer("Kutayotgan chek yo‘q.")  # type: ignore[union-attr]
        return
    for payment in payments:
        text = (
            f"💎 Premium to‘lovi #{payment['id']}\n"
            f"Foydalanuvchi ID: <code>{payment['user_id']}</code>\n"
            f"Yuborilgan: {plain(payment['created_at'])}"
        )
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="✅ Tasdiqlash",
                        callback_data=f"payment:approve:{payment['id']}",
                    ),
                    InlineKeyboardButton(
                        text="❌ Rad etish",
                        callback_data=f"payment:reject:{payment['id']}",
                    ),
                ]
            ]
        )
        if payment["receipt_type"] == "photo":
            await callback.message.answer_photo(  # type: ignore[union-attr]
                payment["receipt_file_id"], caption=text, reply_markup=keyboard
            )
        else:
            await callback.message.answer_document(  # type: ignore[union-attr]
                payment["receipt_file_id"], caption=text, reply_markup=keyboard
            )


async def list_movies_keyboard(action: str) -> InlineKeyboardMarkup | None:
    async with await connect_db() as db:
        async with db.execute(
            "SELECT id, code, title, year FROM movies ORDER BY title COLLATE NOCASE LIMIT 50"
        ) as cursor:
            movies = await cursor.fetchall()
    if not movies:
        return None
    rows = []
    for movie in movies:
        label = f"{movie['code']} · {movie['title']}"
        if movie["year"]:
            label += f" ({movie['year']})"
        rows.append(
            [
                InlineKeyboardButton(
                    text=label[:60],
                    callback_data=f"movie:{action}:{movie['id']}",
                )
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(F.text == "🎬 Kino qo‘shish")
async def add_movie_start(message: Message, state: FSMContext) -> None:
    if not await is_admin_message(message):
        return
    await state.clear()
    await state.set_state(AddMovie.waiting_for_video)
    await message.answer(
        "Kino videosini yuboring. Telegram video fayli sifatida yuborilishi kerak.",
        reply_markup=cancel_keyboard(),
    )


@router.message(AddMovie.waiting_for_video, F.video)
async def add_movie_video(message: Message, state: FSMContext) -> None:
    if not await is_admin_message(message):
        return
    await state.update_data(video_file_id=message.video.file_id)  # type: ignore[union-attr]
    await state.set_state(AddMovie.waiting_for_title)
    await message.answer("Kino nomini yuboring.", reply_markup=cancel_keyboard())


async def add_movie_field(
    message: Message,
    state: FSMContext,
    field: str,
    next_state: State | None,
    next_prompt: str | None,
) -> None:
    if not await is_admin_message(message):
        return
    value = (message.text or "").strip()
    if value == SKIP_TEXT:
        value = ""
    elif not value:
        await message.answer("Qiymat yuboring yoki o‘tkazib yuboring.")
        return
    limits = {
        "title": 160,
        "episodes": 32,
        "country": 80,
        "language": 80,
        "year": 4,
        "genre": 120,
    }
    if value and len(value) > limits[field]:
        await message.answer(f"Qiymat {limits[field]} belgidan oshmasin.")
        return
    await state.update_data(**{field: value or None})
    if next_state is None:
        data = await state.get_data()
        if not data.get("title"):
            await message.answer("Kino nomi bo‘sh bo‘lmasligi kerak.")
            await state.set_state(AddMovie.waiting_for_title)
            await message.answer("Kino nomini yuboring.", reply_markup=cancel_keyboard())
            return
        async with await connect_db() as db:
            await db.execute("BEGIN IMMEDIATE")
            code = await next_movie_code(db)
            await db.execute(
                """
                INSERT INTO movies(
                    code, title, video_file_id, episodes, country, language, year, genre, added_by, added_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    code,
                    data["title"],
                    data["video_file_id"],
                    data.get("episodes"),
                    data.get("country"),
                    data.get("language"),
                    data.get("year"),
                    data.get("genre"),
                    message.from_user.id if message.from_user else 0,
                    now_iso(),
                ),
            )
            await db.commit()
        await state.clear()
        await message.answer(
            f"✅ <b>{plain(data['title'])}</b> bazaga qo‘shildi.\n"
            f"🎟 Kino kodi: <code>{plain(code)}</code>",
            reply_markup=admin_keyboard(),
        )
        return
    await state.set_state(next_state)
    await message.answer(next_prompt or "", reply_markup=skip_keyboard())


@router.message(AddMovie.waiting_for_title, F.text)
async def add_movie_title(message: Message, state: FSMContext) -> None:
    await add_movie_field(
        message,
        state,
        "title",
        AddMovie.waiting_for_episodes,
        "Qismlar sonini kiriting yoki o‘tkazib yuboring.",
    )


@router.message(AddMovie.waiting_for_episodes, F.text)
async def add_movie_episodes(message: Message, state: FSMContext) -> None:
    value = (message.text or "").strip()
    if value != SKIP_TEXT and value and (not value.isdigit() or int(value) < 1):
        await message.answer("Qismlar sonini musbat butun son qilib kiriting yoki o‘tkazib yuboring.")
        return
    await add_movie_field(
        message,
        state,
        "episodes",
        AddMovie.waiting_for_country,
        "Davlatni kiriting yoki o‘tkazib yuboring.",
    )


@router.message(AddMovie.waiting_for_country, F.text)
async def add_movie_country(message: Message, state: FSMContext) -> None:
    await add_movie_field(
        message, state, "country", AddMovie.waiting_for_language, "Tilni kiriting yoki o‘tkazib yuboring."
    )


@router.message(AddMovie.waiting_for_language, F.text)
async def add_movie_language(message: Message, state: FSMContext) -> None:
    await add_movie_field(
        message, state, "language", AddMovie.waiting_for_year, "Yilni kiriting yoki o‘tkazib yuboring."
    )


@router.message(AddMovie.waiting_for_year, F.text)
async def add_movie_year(message: Message, state: FSMContext) -> None:
    value = (message.text or "").strip()
    if value != SKIP_TEXT and value and (not value.isdigit() or not 1888 <= int(value) <= 2200):
        await message.answer("Yilni 1888–2200 oralig‘ida kiriting yoki o‘tkazib yuboring.")
        return
    await add_movie_field(
        message, state, "year", AddMovie.waiting_for_genre, "Janrni kiriting yoki o‘tkazib yuboring."
    )


@router.message(AddMovie.waiting_for_genre, F.text)
async def add_movie_genre(message: Message, state: FSMContext) -> None:
    await add_movie_field(message, state, "genre", None, None)


@router.message(F.text == "🗑 Kino o‘chirish")
async def delete_movie_start(message: Message) -> None:
    if not await is_admin_message(message):
        return
    keyboard = await list_movies_keyboard("delete")
    await message.answer(
        "O‘chirmoqchi bo‘lgan kinoni tanlang (ro‘yxatda eng ko‘pi bilan 50 ta kino):"
        if keyboard
        else "Bazaga kino kiritilmagan.",
        reply_markup=keyboard,
    )


@router.callback_query(F.data.startswith("movie:delete:"))
async def delete_movie_callback(callback: CallbackQuery) -> None:
    if not await admin_only(callback):
        return
    try:
        movie_id = int((callback.data or "").rsplit(":", 1)[1])
    except ValueError:
        await callback.answer("Kino topilmadi.", show_alert=True)
        return
    async with await connect_db() as db:
        async with db.execute("SELECT title FROM movies WHERE id = ?", (movie_id,)) as cursor:
            movie = await cursor.fetchone()
    if not movie:
        await callback.answer("Kino topilmadi.", show_alert=True)
        return
    confirmation = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Ha, o‘chirish",
                    callback_data=f"movie:delete-confirm:{movie_id}",
                ),
                InlineKeyboardButton(
                    text="Bekor qilish",
                    callback_data="movie:delete-cancel",
                ),
            ]
        ]
    )
    await callback.answer()
    if callback.message:
        await callback.message.answer(
            f"<b>{plain(movie['title'])}</b> kinoni bazadan o‘chirasizmi?",
            reply_markup=confirmation,
        )


@router.callback_query(F.data.startswith("movie:delete-confirm:"))
async def delete_movie_confirm(callback: CallbackQuery) -> None:
    if not await admin_only(callback):
        return
    try:
        movie_id = int((callback.data or "").rsplit(":", 1)[1])
    except ValueError:
        await callback.answer("Kino topilmadi.", show_alert=True)
        return
    async with await connect_db() as db:
        await db.execute("DELETE FROM movies WHERE id = ?", (movie_id,))
        await db.commit()
    await callback.answer("Kino o‘chirildi.")
    if callback.message:
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.answer("Kino bazadan o‘chirildi.")


@router.callback_query(F.data == "movie:delete-cancel")
async def delete_movie_cancel(callback: CallbackQuery) -> None:
    if not await admin_only(callback):
        return
    await callback.answer("O‘chirish bekor qilindi.")
    if callback.message:
        await callback.message.edit_reply_markup(reply_markup=None)


@router.message(F.text == "✏️ Kino tahrirlash")
async def edit_movie_start(message: Message) -> None:
    if not await is_admin_message(message):
        return
    keyboard = await list_movies_keyboard("edit")
    await message.answer(
        "Tahrirlash uchun kinoni tanlang (ro‘yxatda eng ko‘pi bilan 50 ta kino):"
        if keyboard
        else "Bazaga kino kiritilmagan.",
        reply_markup=keyboard,
    )


@router.callback_query(F.data.startswith("movie:edit:"))
async def edit_movie_select(callback: CallbackQuery, state: FSMContext) -> None:
    if not await admin_only(callback):
        return
    try:
        movie_id = int((callback.data or "").rsplit(":", 1)[1])
    except ValueError:
        await callback.answer("Kino topilmadi.", show_alert=True)
        return
    async with await connect_db() as db:
        async with db.execute("SELECT id, title FROM movies WHERE id = ?", (movie_id,)) as cursor:
            movie = await cursor.fetchone()
    if not movie:
        await callback.answer("Kino topilmadi.", show_alert=True)
        return
    await state.update_data(movie_id=movie_id)
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=label,
                    callback_data=f"moviefield:{field}",
                )
            ]
            for field, (label, _) in MOVIE_FIELDS.items()
        ]
    )
    await callback.answer()
    await callback.message.answer(  # type: ignore[union-attr]
        f"<b>{plain(movie['title'])}</b> — qaysi ma’lumotni tahrirlaysiz?",
        reply_markup=keyboard,
    )


@router.callback_query(F.data.startswith("moviefield:"))
async def edit_movie_field(callback: CallbackQuery, state: FSMContext) -> None:
    if not await admin_only(callback):
        return
    field = (callback.data or "").split(":", 1)[1]
    if field not in MOVIE_FIELDS:
        await callback.answer("Maydon topilmadi.", show_alert=True)
        return
    await state.update_data(edit_field=field)
    await state.set_state(EditMovie.waiting_for_value)
    await callback.answer()
    await callback.message.answer(  # type: ignore[union-attr]
        f"{MOVIE_FIELDS[field][0]} uchun yangi qiymat yuboring. Maydonni bo‘shatish uchun <code>-</code> yuboring.",
        reply_markup=cancel_keyboard(),
    )


@router.message(EditMovie.waiting_for_value, F.text)
async def edit_movie_save(message: Message, state: FSMContext) -> None:
    if not await is_admin_message(message):
        return
    data = await state.get_data()
    field = data.get("edit_field")
    movie_id = data.get("movie_id")
    if field not in MOVIE_FIELDS or not movie_id:
        await state.clear()
        await message.answer("Tahrirlash holati topilmadi.")
        return
    value = (message.text or "").strip()
    value = "" if value == "-" else value
    limits = {
        "code": 12,
        "title": 160,
        "episodes": 32,
        "country": 80,
        "language": 80,
        "year": 4,
        "genre": 120,
    }
    if len(value) > limits[field]:
        await message.answer(f"Qiymat {limits[field]} belgidan oshmasin.")
        return
    if field == "code" and (not value or not value.isdigit()):
        await message.answer("Kino kodi bo‘sh bo‘lmasin va faqat raqamlardan iborat bo‘lsin.")
        return
    if field == "title" and not value:
        await message.answer("Kino nomini bo‘sh qoldirib bo‘lmaydi.")
        return
    if field == "code":
        async with await connect_db() as db:
            async with db.execute(
                "SELECT 1 FROM movies WHERE code = ? AND id != ?",
                (value, movie_id),
            ) as cursor:
                if await cursor.fetchone():
                    await message.answer("Bu kod boshqa kinoga berilgan. Boshqa kod kiriting.")
                    return
    if field in {"episodes", "year"} and value:
        valid = value.isdigit() and (
            int(value) >= 1 if field == "episodes" else 1888 <= int(value) <= 2200
        )
        if not valid:
            await message.answer("Son qiymati yaroqsiz.")
            return
    async with await connect_db() as db:
        await db.execute(f"UPDATE movies SET {field} = ? WHERE id = ?", (value or None, movie_id))
        await db.commit()
    await state.clear()
    await message.answer("Kino ma’lumoti saqlandi.", reply_markup=admin_keyboard())


@router.message(F.text == "👮 Adminlar")
async def admins_menu(message: Message) -> None:
    if not await is_admin_message(message):
        return
    async with await connect_db() as db:
        async with db.execute("SELECT user_id FROM admins ORDER BY user_id") as cursor:
            rows = await cursor.fetchall()
    lines = ["👮 <b>ADMINLAR</b>"]
    lines.extend(f"• <code>{row['user_id']}</code>" for row in rows)
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➕ Admin qo‘shish", callback_data="admin:add")],
            [InlineKeyboardButton(text="➖ Adminni o‘chirish", callback_data="admin:remove")],
        ]
    )
    await message.answer("\n".join(lines), reply_markup=keyboard)


@router.callback_query(F.data == "admin:add")
async def add_admin_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not await admin_only(callback):
        return
    await state.set_state(AddAdmin.waiting_for_user_id)
    await callback.answer()
    await callback.message.answer("Yangi adminning Telegram ID raqamini yuboring.")  # type: ignore[union-attr]


@router.message(AddAdmin.waiting_for_user_id, F.text)
async def add_admin_save(message: Message, state: FSMContext) -> None:
    if not await is_admin_message(message):
        return
    raw = (message.text or "").strip()
    if not raw.isdigit():
        await message.answer("Telegram ID faqat raqamlardan iborat bo‘lishi kerak.")
        return
    user_id = int(raw)
    async with await connect_db() as db:
        await db.execute(
            "INSERT OR IGNORE INTO admins(user_id, added_at) VALUES (?, ?)",
            (user_id, now_iso()),
        )
        await db.commit()
    await state.clear()
    await message.answer(f"✅ <code>{user_id}</code> adminlar ro‘yxatiga qo‘shildi.")


@router.callback_query(F.data == "admin:remove")
async def remove_admin_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not await admin_only(callback):
        return
    await state.set_state(RemoveAdmin.waiting_for_user_id)
    await callback.answer()
    await callback.message.answer("O‘chiriladigan adminning Telegram ID raqamini yuboring.")  # type: ignore[union-attr]


@router.message(RemoveAdmin.waiting_for_user_id, F.text)
async def remove_admin_save(message: Message, state: FSMContext) -> None:
    if not await is_admin_message(message):
        return
    raw = (message.text or "").strip()
    if not raw.isdigit():
        await message.answer("Telegram ID faqat raqamlardan iborat bo‘lishi kerak.")
        return
    user_id = int(raw)
    async with await connect_db() as db:
        async with db.execute("SELECT COUNT(*) AS n FROM admins") as cursor:
            count = int((await cursor.fetchone())["n"])
        if count <= 1:
            await message.answer("Oxirgi adminni o‘chirib bo‘lmaydi.")
            return
        await db.execute("DELETE FROM admins WHERE user_id = ?", (user_id,))
        await db.commit()
    await state.clear()
    await message.answer("Admin ro‘yxati yangilandi.", reply_markup=admin_keyboard())


@router.message(F.text == "📝 Start sozlamalari")
async def start_settings_menu(message: Message, state: FSMContext) -> None:
    if not await is_admin_message(message):
        return
    current = await setting("start_text")
    await state.set_state(StartSettings.waiting_for_text)
    await message.answer(
        "Yangi start xabarini yuboring (2500 belgigacha).\n\n"
        f"Hozirgi matn:\n{plain(current)}",
        reply_markup=cancel_keyboard(),
    )


@router.message(StartSettings.waiting_for_text, F.text)
async def start_settings_save(message: Message, state: FSMContext) -> None:
    if not await is_admin_message(message):
        return
    value = (message.text or "").strip()
    if not value or len(value) > 2500:
        await message.answer("Xabar 1–2500 belgi bo‘lishi kerak.")
        return
    await set_setting("start_text", value)
    await state.clear()
    await message.answer("Start xabari saqlandi.", reply_markup=admin_keyboard())


@router.message(F.text == "📣 Reklama")
async def broadcast_start(message: Message, state: FSMContext) -> None:
    if not await is_admin_message(message):
        return
    await state.set_state(Broadcast.waiting_for_message)
    await message.answer(
        "Foydalanuvchilarga yuboriladigan reklama matnini yuboring. "
        "HTML formatlash yoqilmagan; barcha foydalanuvchiga matn sifatida jo‘natiladi.",
        reply_markup=cancel_keyboard(),
    )


@router.message(Broadcast.waiting_for_message, F.text)
async def broadcast_send(message: Message, state: FSMContext) -> None:
    if not await is_admin_message(message):
        return
    text = (message.text or "").strip()
    if not text or len(text) > 3500:
        await message.answer("Reklama matni 1–3500 belgi bo‘lishi kerak.")
        return
    await state.update_data(broadcast_text=text)
    await state.set_state(Broadcast.waiting_for_confirmation)
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📣 Yuborish", callback_data="broadcast:confirm"),
                InlineKeyboardButton(text="❌ Bekor qilish", callback_data="broadcast:cancel"),
            ]
        ]
    )
    await message.answer(
        f"<b>Reklama matni:</b>\n\n{plain(text)}\n\n"
        "Ushbu xabar barcha bot foydalanuvchilariga yuborilsinmi?",
        reply_markup=keyboard,
    )


@router.callback_query(F.data == "broadcast:cancel")
async def broadcast_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    if not await admin_only(callback):
        return
    await state.clear()
    await callback.answer("Reklama bekor qilindi.")
    if callback.message:
        await callback.message.edit_reply_markup(reply_markup=None)


@router.callback_query(F.data == "broadcast:confirm")
async def broadcast_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    if not await admin_only(callback):
        return
    data = await state.get_data()
    text = data.get("broadcast_text")
    if await state.get_state() != Broadcast.waiting_for_confirmation.state or not text:
        await callback.answer("Yuborishga tayyor reklama topilmadi.", show_alert=True)
        return
    async with await connect_db() as db:
        async with db.execute("SELECT user_id FROM users ORDER BY user_id") as cursor:
            users = await cursor.fetchall()
    await state.clear()
    await callback.answer("Yuborish boshlandi.")
    sent = 0
    failed = 0
    for user in users:
        try:
            await bot.send_message(  # type: ignore[union-attr]
                int(user["user_id"]),
                f"📣 <b>Reklama</b>\n\n{plain(text)}",
            )
            sent += 1
            await asyncio.sleep(0.04)
        except TelegramAPIError:
            failed += 1
    if callback.message:
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.answer(
            f"Reklama yuborish yakunlandi.\n✅ Yetkazildi: {sent}\n⚠️ Yetkazilmadi: {failed}",
            reply_markup=admin_keyboard(),
        )


def status_member(event: ChatMemberUpdated, old: bool) -> bool:
    member = event.old_chat_member if old else event.new_chat_member
    return member_status_is_active(member)


@router.chat_member()
async def track_chat_member(event: ChatMemberUpdated, event_update: Update) -> None:
    if not bot:
        return
    try:
        async with await connect_db() as db:
            async with db.execute(
                "SELECT id FROM channels WHERE chat_id = ?", (str(event.chat.id),)
            ) as cursor:
                channel = await cursor.fetchone()
            if not channel:
                return
            old_is_member = status_member(event, old=True)
            new_is_member = status_member(event, old=False)
            old_status = str(event.old_chat_member.status)
            new_status = str(event.new_chat_member.status)
            stamp = now_iso()
            try:
                await db.execute(
                    "INSERT INTO processed_updates(update_id, processed_at) VALUES (?, ?)",
                    (event_update.update_id, stamp),
                )
            except aiosqlite.IntegrityError:
                return
            if old_is_member != new_is_member:
                event_type = "joined" if new_is_member else "left"
                await db.execute(
                    """
                    INSERT INTO channel_events(
                        update_id, channel_id, user_id, event_type, old_status, new_status, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event_update.update_id,
                        channel["id"],
                        event.new_chat_member.user.id,
                        event_type,
                        old_status,
                        new_status,
                        stamp,
                    ),
                )
                await db.execute(
                    """
                    INSERT INTO member_current(channel_id, user_id, is_member, status, last_changed_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(channel_id, user_id) DO UPDATE SET
                        is_member = excluded.is_member,
                        status = excluded.status,
                        last_changed_at = excluded.last_changed_at
                    """,
                    (
                        channel["id"],
                        event.new_chat_member.user.id,
                        1 if new_is_member else 0,
                        new_status,
                        stamp,
                    ),
                )
            else:
                await db.execute(
                    """
                    INSERT INTO member_current(channel_id, user_id, is_member, status, last_changed_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(channel_id, user_id) DO UPDATE SET
                        status = excluded.status,
                        last_changed_at = excluded.last_changed_at
                    """,
                    (
                        channel["id"],
                        event.new_chat_member.user.id,
                        1 if new_is_member else 0,
                        new_status,
                        stamp,
                    ),
                )
            await db.execute(
                "UPDATE channels SET last_update_at = ? WHERE id = ?",
                (stamp, channel["id"]),
            )
            await db.commit()
    except Exception:
        logger.exception("Failed to record a chat member update.")


@router.my_chat_member()
async def report_bot_channel_role(event: ChatMemberUpdated) -> None:
    try:
        async with await connect_db() as db:
            async with db.execute(
                "SELECT id FROM channels WHERE chat_id = ?", (str(event.chat.id),)
            ) as cursor:
                channel = await cursor.fetchone()
            if channel:
                await db.execute(
                    "UPDATE channels SET last_update_at = ? WHERE id = ?",
                    (now_iso(), channel["id"]),
                )
                await db.commit()
    except Exception:
        logger.exception("Failed to save a bot channel-role update.")


@router.message(F.text)
async def unknown_text(message: Message) -> None:
    if not message.from_user:
        return
    await record_user(message)
    text = (message.text or "").strip()
    if text == "💎 PREMIUM":
        await send_premium_info(message)
        return
    if not text.isdigit():
        await message.answer(
            "Kino kodini raqam bilan yozing.",
            reply_markup=user_keyboard(await is_admin(message.from_user.id)),
        )
        return
    if not await is_premium(message.from_user.id):
        channels = await fetch_channels()
        if not await user_has_required_subscriptions(message.from_user.id, channels):
            await show_subscription_prompt(message, channels=channels)
            return
    await send_movie_by_code(message)


async def send_movie_by_code(message: Message) -> None:
    code = (message.text or "").strip()
    if not code.isdigit():
        await message.answer(
            "Kino kodini raqam bilan yozing.",
            reply_markup=user_keyboard(await is_admin(message.from_user.id)),  # type: ignore[union-attr]
        )
        return
    movie = await find_movie_by_code(code)
    if not movie:
        await message.answer(
            "Bunday kino mavjud emas.",
            reply_markup=user_keyboard(await is_admin(message.from_user.id)),  # type: ignore[union-attr]
        )
        return
    lines = [
        f"🎬 <b>{plain(movie['title'])}</b>",
        f"🎟 Kino kodi: <code>{plain(movie['code'])}</code>",
    ]
    for key, label in (
        ("episodes", "Qismlar"),
        ("country", "Davlat"),
        ("language", "Til"),
        ("year", "Yil"),
        ("genre", "Janr"),
    ):
        if movie[key]:
            lines.append(f"{label}: {plain(movie[key])}")
    await message.answer_video(
        video=movie["video_file_id"],
        caption="\n".join(lines),
        reply_markup=user_keyboard(await is_admin(message.from_user.id)),  # type: ignore[union-attr]
    )


async def find_movie_by_code(code: str) -> aiosqlite.Row | None:
    async with await connect_db() as db:
        async with db.execute(
            "SELECT * FROM movies WHERE code = ?",
            (code,),
        ) as cursor:
            return await cursor.fetchone()


async def main() -> None:
    global bot_username, bot
    if not TOKEN or bot is None:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN topilmadi. Tokenni Replit Secrets’da yoki mahalliy .env faylida sozlang."
        )
    await init_db()
    if not ADMIN_IDS:
        logger.warning(
            "ADMIN_TELEGRAM_IDS bo‘sh: bot ishlaydi, lekin admin panelga kira oladigan admin yo‘q. "
            "Birinchi admin ID’sini sozlang."
        )
    me = await bot.get_me()
    bot_username = me.username or ""
    if not bot_username:
        logger.warning("Bot username’i topilmadi; referral havolasi yaratilmaydi.")
    await bot.delete_webhook(drop_pending_updates=False)
    logger.info("Tamosho bot polling boshlandi.")
    expiry_task = asyncio.create_task(expire_premium_loop())
    try:
        await dp.start_polling(
            bot,
            allowed_updates=["message", "callback_query", "chat_member", "my_chat_member"],
            close_bot_session=False,
        )
    finally:
        expiry_task.cancel()
        await asyncio.gather(expiry_task, return_exceptions=True)
        await bot.session.close()


async def expire_premium_loop() -> None:
    while True:
        try:
            async with await connect_db() as db:
                await db.execute(
                    "UPDATE premium_subscriptions SET active = 0 WHERE active = 1 AND expires_at <= ?",
                    (now_iso(),),
                )
                await db.commit()
        except Exception:
            logger.exception("Failed to expire Premium subscriptions.")
        await asyncio.sleep(300)


if __name__ == "__main__":
    asyncio.run(main())
