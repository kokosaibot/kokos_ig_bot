import asyncio
import logging
import os
import re
import sqlite3
from pathlib import Path
from typing import Optional

import yt_dlp
from telegram import InputFile, InputMediaPhoto, InputMediaVideo, Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# =========================
# SAVE COOKIES FROM RAILWAY VARIABLES
# =========================
def save_env_file(env_name: str, filename: str) -> None:
    data = os.getenv(env_name)
    if not data:
        print(f"⚠️ Нет {env_name}")
        return

    with open(filename, "w", encoding="utf-8") as f:
        f.write(data)

    print(f"✅ {env_name} сохранены в {filename}")


save_env_file("INSTAGRAM_COOKIES", "instagram_cookies.txt")
save_env_file("TIKTOK_COOKIES", "tiktok_cookies.txt")

# =========================
# CONFIG
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN")

try:
    ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
except ValueError:
    ADMIN_ID = 0

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN not found in environment variables")

BASE_DIR = Path(__file__).resolve().parent
DOWNLOAD_DIR = BASE_DIR / "downloads"
DOWNLOAD_DIR.mkdir(exist_ok=True)

DB_PATH = BASE_DIR / "bot.db"
INSTAGRAM_COOKIE_FILE = BASE_DIR / "instagram_cookies.txt"
TIKTOK_COOKIE_FILE = BASE_DIR / "tiktok_cookies.txt"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
logger = logging.getLogger("insta_tiktok_bot")

# =========================
# DATABASE
# =========================
conn = sqlite3.connect(DB_PATH, check_same_thread=False)
cursor = conn.cursor()

cursor.execute("""
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    username TEXT,
    first_name TEXT,
    joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS stats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    platform TEXT,
    media_type TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
""")
conn.commit()


def add_user(user_id: int, username: Optional[str], first_name: Optional[str]) -> None:
    cursor.execute("""
        INSERT OR IGNORE INTO users (user_id, username, first_name)
        VALUES (?, ?, ?)
    """, (user_id, username, first_name))
    conn.commit()


def get_users_count() -> int:
    cursor.execute("SELECT COUNT(*) FROM users")
    row = cursor.fetchone()
    return int(row[0]) if row else 0


def get_all_user_ids() -> list[int]:
    cursor.execute("SELECT user_id FROM users")
    return [int(row[0]) for row in cursor.fetchall()]


def add_stat(user_id: int, platform: str, media_type: str) -> None:
    cursor.execute("""
        INSERT INTO stats (user_id, platform, media_type)
        VALUES (?, ?, ?)
    """, (user_id, platform, media_type))
    conn.commit()


def get_stats_summary() -> list[tuple[str, str, int]]:
    cursor.execute("""
        SELECT platform, media_type, COUNT(*)
        FROM stats
        GROUP BY platform, media_type
        ORDER BY COUNT(*) DESC
    """)
    return cursor.fetchall()


# =========================
# STATE
# =========================
URL_RE = re.compile(r"(https?://[^\s]+)", re.IGNORECASE)
INSTAGRAM_RE = re.compile(r"(instagram\.com)", re.IGNORECASE)
TIKTOK_RE = re.compile(r"(tiktok\.com)", re.IGNORECASE)

broadcast_waiting: set[int] = set()


# =========================
# HELPERS
# =========================
def extract_url(text: str) -> Optional[str]:
    match = URL_RE.search(text or "")
    return match.group(1) if match else None


def detect_platform(url: str) -> str:
    if INSTAGRAM_RE.search(url):
        return "instagram"
    if TIKTOK_RE.search(url):
        return "tiktok"
    return "unknown"


def cleanup_old_downloads() -> None:
    for item in DOWNLOAD_DIR.iterdir():
        try:
            if item.is_file():
                item.unlink()
        except Exception:
            pass


def safe_delete(paths: list[Path]) -> None:
    for path in paths:
        try:
            if path.exists():
                path.unlink()
        except Exception:
            pass


def guess_media_type(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in {".jpg", ".jpeg", ".png", ".webp"}:
        return "photo"
    if ext in {".mp4", ".mov", ".mkv", ".webm"}:
        return "video"
    return "file"


def social_base_opts(platform: str) -> dict:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 30,
        "retries": 2,
        "fragment_retries": 2,
        "noplaylist": False,
        "outtmpl": str(DOWNLOAD_DIR / "%(id)s_%(autonumber)s.%(ext)s"),
    }

    if platform == "instagram" and INSTAGRAM_COOKIE_FILE.exists():
        opts["cookiefile"] = str(INSTAGRAM_COOKIE_FILE)

    if platform == "tiktok" and TIKTOK_COOKIE_FILE.exists():
        opts["cookiefile"] = str(TIKTOK_COOKIE_FILE)

    return opts


def collect_paths_from_info(info: dict, ydl: yt_dlp.YoutubeDL) -> list[Path]:
    found: list[Path] = []

    def add_path(p: Optional[str]) -> None:
        if not p:
            return
        path = Path(p)
        if path.exists() and path not in found:
            found.append(path)

        mp4_candidate = path.with_suffix(".mp4")
        if mp4_candidate.exists() and mp4_candidate not in found:
            found.append(mp4_candidate)

    def walk(obj: dict) -> None:
        if not isinstance(obj, dict):
            return

        requested_downloads = obj.get("requested_downloads")
        if requested_downloads:
            for item in requested_downloads:
                add_path(item.get("filepath"))

        filepath = obj.get("_filename")
        add_path(filepath)

        try:
            prepared = ydl.prepare_filename(obj)
            add_path(prepared)
        except Exception:
            pass

        entries = obj.get("entries")
        if entries:
            for entry in entries:
                if isinstance(entry, dict):
                    walk(entry)

    walk(info)

    unique = []
    seen = set()
    for p in found:
        if p.exists() and str(p) not in seen:
            unique.append(p)
            seen.add(str(p))

    return unique


def download_social(url: str, platform: str) -> tuple[list[Path], str]:
    opts = social_base_opts(platform)

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        title = info.get("title") or "media"
        paths = collect_paths_from_info(info, ydl)

        if not paths:
            raise RuntimeError("Не удалось найти скачанные файлы после загрузки")

        return paths, title


async def send_media_items(message, files: list[Path], title: str) -> str:
    if len(files) == 1:
        path = files[0]
        media_type = guess_media_type(path)

        with open(path, "rb") as f:
            if media_type == "photo":
                await message.reply_photo(photo=f, caption=f"✅ {title[:900]}")
                return "photo"

            if media_type == "video":
                await message.reply_video(video=f, caption=f"✅ {title[:900]}")
                return "video"

            await message.reply_document(
                document=InputFile(f, filename=path.name),
                caption=f"✅ {title[:900]}"
            )
            return "file"

    album = []
    handles = []

    try:
        for idx, path in enumerate(files):
            media_type = guess_media_type(path)
            if media_type not in {"photo", "video"}:
                raise ValueError("Contains non-album file")

            f = open(path, "rb")
            handles.append(f)

            if media_type == "photo":
                album.append(
                    InputMediaPhoto(
                        media=InputFile(f, filename=path.name),
                        caption=f"✅ {title[:900]}" if idx == 0 else None
                    )
                )
            else:
                album.append(
                    InputMediaVideo(
                        media=InputFile(f, filename=path.name),
                        caption=f"✅ {title[:900]}" if idx == 0 else None
                    )
                )

        for i in range(0, len(album), 10):
            await message.reply_media_group(album[i:i + 10])

        return "carousel"

    except Exception:
        for f in handles:
            try:
                f.close()
            except Exception:
                pass

        for idx, path in enumerate(files):
            media_type = guess_media_type(path)
            with open(path, "rb") as f:
                caption = f"✅ {title[:900]}" if idx == 0 else None

                if media_type == "photo":
                    await message.reply_photo(photo=f, caption=caption)
                elif media_type == "video":
                    await message.reply_video(video=f, caption=caption)
                else:
                    await message.reply_document(
                        document=InputFile(f, filename=path.name),
                        caption=caption
                    )

        return "carousel"

    finally:
        for f in handles:
            try:
                f.close()
            except Exception:
                pass


async def send_broadcast(application, text: str) -> tuple[int, int]:
    sent = 0
    failed = 0

    for user_id in get_all_user_ids():
        try:
            await application.bot.send_message(chat_id=user_id, text=text)
            sent += 1
        except Exception:
            failed += 1

    return sent, failed


# =========================
# COMMANDS
# =========================
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    add_user(user.id, user.username, user.first_name)

    text = (
        "Салам 👋\n\n"
        "Отправь ссылку на:\n"
        "- Instagram\n"
        "- TikTok\n\n"
        "Поддержка:\n"
        "- reels\n"
        "- posts\n"
        "- фото\n"
        "- карусели\n"
        "- видео"
    )

    if user.id == ADMIN_ID:
        text += (
            "\n\nАдмин-команды:\n"
            "/users\n"
            "/stats\n"
            "/broadcast"
        )

    await update.message.reply_text(text)


async def users_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_ID:
        return
    await update.message.reply_text(f"👥 Пользователей: {get_users_count()}")


async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_ID:
        return

    stats = get_stats_summary()
    if not stats:
        await update.message.reply_text("Пока статистики нет.")
        return

    text = "📊 Статистика:\n\n"
    for platform, media_type, count in stats:
        text += f"{platform} | {media_type} — {count}\n"

    await update.message.reply_text(text)


async def broadcast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_ID:
        return

    broadcast_waiting.add(update.effective_user.id)
    await update.message.reply_text("Отправь следующим сообщением текст для рассылки.")


# =========================
# MESSAGE HANDLER
# =========================
async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    user = update.effective_user

    if not message or not user:
        return

    add_user(user.id, user.username, user.first_name)
    text = (message.text or "").strip()

    if user.id == ADMIN_ID and user.id in broadcast_waiting:
        if text.startswith("/"):
            broadcast_waiting.discard(user.id)
            await message.reply_text("Рассылка отменена.")
            return

        await message.reply_text("⏳ Начал рассылку...")
        sent, failed = await send_broadcast(context.application, text)
        broadcast_waiting.discard(user.id)

        await message.reply_text(
            f"✅ Рассылка завершена\n\n"
            f"Отправлено: {sent}\n"
            f"Ошибок: {failed}"
        )
        return

    url = extract_url(text)
    if not url:
        await message.reply_text("Скинь нормальную ссылку из Instagram или TikTok.")
        return

    platform = detect_platform(url)
    if platform == "unknown":
        await message.reply_text("Пока поддерживаются только Instagram и TikTok.")
        return

    status = await message.reply_text("⏳ Обрабатываю ссылку...")

    files: list[Path] = []

    try:
        files, title = await asyncio.to_thread(download_social, url, platform)

        if not files:
            await status.edit_text("❌ Ничего не скачалось.")
            return

        media_type = await send_media_items(message, files, title)
        add_stat(user.id, platform, media_type)

        await status.delete()

    except Exception as e:
        logger.exception("SOCIAL DOWNLOAD ERROR")
        await status.edit_text(
            f"❌ Ошибка:\n{str(e)[:350]}\n\n"
            "Что попробовать:\n"
            "1. Если пост приватный — нужны cookies\n"
            "2. Попробуй другую ссылку\n"
            "3. Попробуй позже, если платформа режет лимит"
        )

    finally:
        safe_delete(files)


# =========================
# ERROR HANDLER
# =========================
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Unhandled error", exc_info=context.error)


# =========================
# MAIN
# =========================
def main() -> None:
    cleanup_old_downloads()

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("users", users_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CommandHandler("broadcast", broadcast_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    app.add_error_handler(error_handler)

    logger.info("Instagram/TikTok bot started")
    app.run_polling()


if __name__ == "__main__":
    main()
