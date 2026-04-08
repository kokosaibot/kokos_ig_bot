import asyncio
import logging
import os
import re
import sqlite3
import uuid
from pathlib import Path
from typing import Optional, Tuple

import yt_dlp
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputFile, Update
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# =========================
# SAVE COOKIES FROM RAILWAY VARIABLE
# =========================
def save_cookies() -> None:
    cookies = os.getenv("YOUTUBE_COOKIES")
    if not cookies:
        print("❌ Нет YOUTUBE_COOKIES в переменных")
        return

    with open("cookies.txt", "w", encoding="utf-8") as f:
        f.write(cookies)

    print("✅ YouTube cookies загружены")


save_cookies()

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
COOKIE_FILE = BASE_DIR / "cookies.txt"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
logger = logging.getLogger("kokos_bot")

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
YOUTUBE_RE = re.compile(r"(youtube\.com|youtu\.be)", re.IGNORECASE)
INSTAGRAM_RE = re.compile(r"(instagram\.com)", re.IGNORECASE)
TIKTOK_RE = re.compile(r"(tiktok\.com)", re.IGNORECASE)

broadcast_waiting: set[int] = set()
pending_youtube: dict[str, dict] = {}


# =========================
# HELPERS
# =========================
def extract_url(text: str) -> Optional[str]:
    match = URL_RE.search(text or "")
    return match.group(1) if match else None


def detect_platform(url: str) -> str:
    if YOUTUBE_RE.search(url):
        return "youtube"
    if INSTAGRAM_RE.search(url):
        return "instagram"
    if TIKTOK_RE.search(url):
        return "tiktok"
    return "unknown"


def safe_delete(path: Optional[Path]) -> None:
    if not path:
        return
    try:
        if path.exists():
            path.unlink()
    except Exception:
        pass


def cleanup_old_downloads() -> None:
    for item in DOWNLOAD_DIR.iterdir():
        try:
            if item.is_file():
                item.unlink()
        except Exception:
            pass


def yt_base_opts() -> dict:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 30,
        "retries": 2,
        "fragment_retries": 2,
    }

    if COOKIE_FILE.exists():
        opts["cookiefile"] = str(COOKIE_FILE)

    return opts


def get_youtube_info(url: str) -> dict:
    opts = yt_base_opts() | {
        "skip_download": True,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)


def build_youtube_formats(info: dict) -> list[dict]:
    """
    Собираем доступные видео-форматы по реальным данным ролика.
    Делаем максимум несколько кнопок, без дублей по высоте.
    """
    formats = info.get("formats", [])
    grouped: dict[int, dict] = {}

    for f in formats:
        height = f.get("height")
        fmt_id = f.get("format_id")
        vcodec = f.get("vcodec")
        ext = f.get("ext")

        if not height or not fmt_id:
            continue

        # Пропускаем чисто аудио
        if vcodec == "none":
            continue

        # Берем только адекватные качества
        if height < 144:
            continue

        # Сохраняем один лучший кандидат на каждую высоту
        current = grouped.get(height)
        score = 0

        # Предпочтение mp4 / формату с аудио / более полному описанию
        if ext == "mp4":
            score += 3
        if f.get("acodec") and f.get("acodec") != "none":
            score += 2
        if f.get("filesize"):
            score += 1
        if f.get("tbr"):
            score += 1

        candidate = {
            "height": height,
            "format_id": fmt_id,
            "ext": ext or "",
            "has_audio": f.get("acodec") not in (None, "none"),
            "score": score,
        }

        if current is None or candidate["score"] > current["score"]:
            grouped[height] = candidate

    # Сортируем по высоте
    result = sorted(grouped.values(), key=lambda x: x["height"])

    # Ограничим разумным числом кнопок
    if len(result) > 6:
        # берем 6 распределенных качеств
        indices = []
        for i in range(6):
            idx = round(i * (len(result) - 1) / 5)
            if idx not in indices:
                indices.append(idx)
        result = [result[i] for i in indices]

    return result


def youtube_keyboard(token: str, format_buttons: list[dict]) -> InlineKeyboardMarkup:
    rows = []
    current_row = []

    for item in format_buttons:
        label = f'{item["height"]}p'
        current_row.append(
            InlineKeyboardButton(label, callback_data=f'yt|fmt|{token}|{item["height"]}')
        )
        if len(current_row) == 3:
            rows.append(current_row)
            current_row = []

    if current_row:
        rows.append(current_row)

    rows.append([
        InlineKeyboardButton("🎧 MP3", callback_data=f"yt|mp3|{token}|0"),
        InlineKeyboardButton("🖼 Превью", callback_data=f"yt|thumb|{token}|0"),
    ])

    return InlineKeyboardMarkup(rows)


def build_download_format(item: dict) -> str:
    """
    Если у выбранного формата нет аудио — добавляем bestaudio.
    Если аудио уже есть — качаем его как есть.
    """
    fmt_id = item["format_id"]
    if item["has_audio"]:
        return fmt_id
    return f"{fmt_id}+bestaudio/best"


def download_youtube_video_by_format(url: str, item: dict) -> Tuple[Path, str]:
    file_id = str(uuid.uuid4())
    outtmpl = str(DOWNLOAD_DIR / f"{file_id}.%(ext)s")

    opts = yt_base_opts() | {
        "outtmpl": outtmpl,
        "format": build_download_format(item),
        "merge_output_format": "mp4",
    }

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)

        final_path = None

        requested_downloads = info.get("requested_downloads")
        if requested_downloads:
            for entry in requested_downloads:
                possible = entry.get("filepath")
                if possible and Path(possible).exists():
                    final_path = Path(possible)
                    break

        if final_path is None:
            final_path = Path(ydl.prepare_filename(info))

        mp4_candidate = final_path.with_suffix(".mp4")
        if mp4_candidate.exists():
            final_path = mp4_candidate

        title = info.get("title") or "video"
        return final_path, title


def download_youtube_audio(url: str) -> Tuple[Path, str]:
    file_id = str(uuid.uuid4())
    outtmpl = str(DOWNLOAD_DIR / f"{file_id}.%(ext)s")

    opts = yt_base_opts() | {
        "outtmpl": outtmpl,
        "format": "bestaudio/best",
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }],
    }

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        title = info.get("title") or "audio"
        mp3_path = DOWNLOAD_DIR / f"{file_id}.mp3"
        return mp3_path, title


def download_generic_media(url: str) -> Tuple[Path, str, str]:
    file_id = str(uuid.uuid4())
    outtmpl = str(DOWNLOAD_DIR / f"{file_id}.%(ext)s")

    opts = yt_base_opts() | {
        "outtmpl": outtmpl,
        "format": "best",
    }

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        final_path = Path(ydl.prepare_filename(info))
        title = info.get("title") or "media"
        ext = final_path.suffix.lower().lstrip(".")
        return final_path, title, ext


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
        "- YouTube\n"
        "- TikTok"
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
        await message.reply_text("Скинь нормальную ссылку.")
        return

    platform = detect_platform(url)
    if platform == "unknown":
        await message.reply_text("Пока поддерживаются только YouTube, Instagram и TikTok.")
        return

    status = await message.reply_text("⏳ Обрабатываю ссылку...")

    try:
        if platform == "youtube":
            info = await asyncio.to_thread(get_youtube_info, url)
            title = info.get("title") or "YouTube"
            thumb = info.get("thumbnail")
            duration = info.get("duration")

            format_buttons = build_youtube_formats(info)
            if not format_buttons:
                await status.edit_text("❌ Не удалось получить доступные форматы видео.")
                return

            token = uuid.uuid4().hex[:12]
            pending_youtube[token] = {
                "url": url,
                "title": title,
                "thumb": thumb,
                "duration": duration,
                "user_id": user.id,
                "formats": format_buttons,
            }

            caption = f"🎬 {title}"
            if duration:
                caption += f"\n⏱ {duration} сек"
            caption += "\n\nФорматы для скачивания ↓"

            keyboard = youtube_keyboard(token, format_buttons)

            if thumb:
                try:
                    await message.reply_photo(
                        photo=thumb,
                        caption=caption,
                        reply_markup=keyboard,
                    )
                except Exception:
                    await message.reply_text(
                        caption,
                        reply_markup=keyboard,
                    )
            else:
                await message.reply_text(
                    caption,
                    reply_markup=keyboard,
                )

            await status.delete()
            return

        media_path, title, ext = await asyncio.to_thread(download_generic_media, url)

        if not media_path.exists():
            await status.edit_text("❌ Файл не найден после загрузки.")
            return

        ext_lower = ext.lower()

        with open(media_path, "rb") as f:
            if ext_lower in {"jpg", "jpeg", "png", "webp"}:
                await message.reply_photo(photo=f, caption=f"✅ {title[:900]}")
                add_stat(user.id, platform, "photo")
            elif ext_lower in {"mp4", "mov", "mkv", "webm"}:
                await message.reply_video(video=f, caption=f"✅ {title[:900]}")
                add_stat(user.id, platform, "video")
            else:
                await message.reply_document(
                    document=InputFile(f, filename=media_path.name),
                    caption=f"✅ {title[:900]}"
                )
                add_stat(user.id, platform, "file")

        await status.delete()
        safe_delete(media_path)

    except Exception as e:
        logger.exception("TEXT HANDLER ERROR")
        await status.edit_text(f"❌ Ошибка:\n{str(e)[:350]}")


# =========================
# CALLBACK HANDLER
# =========================
async def youtube_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    user = update.effective_user
    parts = (query.data or "").split("|")

    if len(parts) != 4 or parts[0] != "yt":
        return

    action = parts[1]
    token = parts[2]
    value = parts[3]

    item = pending_youtube.get(token)

    if not item:
        try:
            await query.edit_message_caption(
                caption="❌ Ссылка устарела. Отправь заново.",
                reply_markup=None,
            )
        except Exception:
            await query.message.reply_text("❌ Ссылка устарела. Отправь заново.")
        return

    if item["user_id"] != user.id and user.id != ADMIN_ID:
        await query.answer("Это не твоя кнопка", show_alert=True)
        return

    url = item["url"]
    title = item["title"]

    try:
        if action == "thumb":
            thumb = item.get("thumb")
            if thumb:
                await query.message.reply_photo(photo=thumb, caption=f"🖼 {title[:900]}")
            else:
                await query.message.reply_text("У этого видео нет превью.")
            return

        processing = await query.message.reply_text("⏳ Скачиваю...")

        if action == "mp3":
            audio_path, audio_title = await asyncio.to_thread(download_youtube_audio, url)

            if not audio_path.exists():
                await processing.edit_text("❌ MP3 не собрался.")
                return

            with open(audio_path, "rb") as f:
                await query.message.reply_audio(
                    audio=f,
                    title=audio_title[:64],
                    caption="🎧 Готово"
                )

            add_stat(user.id, "youtube", "audio")
            safe_delete(audio_path)
            await processing.delete()
            return

        if action == "fmt":
            selected_height = int(value)
            formats_list = item.get("formats", [])
            selected = next((x for x in formats_list if x["height"] == selected_height), None)

            if not selected:
                await processing.edit_text("❌ Формат больше недоступен.")
                return

            video_path, video_title = await asyncio.to_thread(download_youtube_video_by_format, url, selected)

            if not video_path.exists():
                await processing.edit_text("❌ Видео не собралось.")
                return

            with open(video_path, "rb") as f:
                await query.message.reply_video(
                    video=f,
                    caption=f"✅ {video_title[:900]}"
                )

            add_stat(user.id, "youtube", f'video_{selected["height"]}p')
            safe_delete(video_path)
            await processing.delete()
            return

    except Exception as e:
        logger.exception("YOUTUBE CALLBACK ERROR")
        await query.message.reply_text(f"❌ Ошибка:\n{str(e)[:350]}")


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
    app.add_handler(CallbackQueryHandler(youtube_callback, pattern=r"^yt\|"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    app.add_error_handler(error_handler)

    logger.info("Bot started")
    app.run_polling()


if __name__ == "__main__":
    main()
