import logging
import os
import re
import uuid
from pathlib import Path

import yt_dlp
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from database import add_user, get_users_count, get_all_user_ids, add_stat, get_stats_summary


# ========= CONFIG =========
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN not found in environment variables")

DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ========= STATE =========
broadcast_waiting = set()

# ========= URL REGEX =========
URL_RE = re.compile(r"(https?://[^\s]+)", re.IGNORECASE)
YOUTUBE_RE = re.compile(r"(youtube\.com|youtu\.be)", re.IGNORECASE)
INSTAGRAM_RE = re.compile(r"(instagram\.com)", re.IGNORECASE)
TIKTOK_RE = re.compile(r"(tiktok\.com)", re.IGNORECASE)


# ========= HELPERS =========
def extract_url(text: str) -> str | None:
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


def safe_unlink(path: str | Path | None) -> None:
    if not path:
        return
    try:
        Path(path).unlink(missing_ok=True)
    except Exception:
        pass


def build_video_opts(output_path: str) -> dict:
    return {
        "outtmpl": output_path,
        "format": "mp4/bestvideo+bestaudio/best",
        "merge_output_format": "mp4",
        "quiet": True,
        "noplaylist": True,
    }


def build_audio_opts(output_template: str) -> dict:
    return {
        "outtmpl": output_template,
        "format": "bestaudio/best",
        "quiet": True,
        "noplaylist": True,
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }
        ],
    }


def download_video(url: str) -> tuple[str, str]:
    file_id = str(uuid.uuid4())
    output_template = str(DOWNLOAD_DIR / f"{file_id}.%(ext)s")

    with yt_dlp.YoutubeDL(build_video_opts(output_template)) as ydl:
        info = ydl.extract_info(url, download=True)
        final_path = ydl.prepare_filename(info)

        requested_downloads = info.get("requested_downloads")
        if requested_downloads:
            possible = requested_downloads[0].get("filepath")
            if possible and Path(possible).exists():
                final_path = possible

        ext = Path(final_path).suffix.lower()
        if ext != ".mp4":
            mp4_candidate = str(Path(final_path).with_suffix(".mp4"))
            if Path(mp4_candidate).exists():
                final_path = mp4_candidate

        title = info.get("title", "video")
        return final_path, title


def download_audio(url: str) -> tuple[str, str]:
    file_id = str(uuid.uuid4())
    output_template = str(DOWNLOAD_DIR / f"{file_id}.%(ext)s")

    with yt_dlp.YoutubeDL(build_audio_opts(output_template)) as ydl:
        info = ydl.extract_info(url, download=True)
        title = info.get("title", "audio")
        mp3_path = str(DOWNLOAD_DIR / f"{file_id}.mp3")
        return mp3_path, title


async def send_broadcast(app, text: str) -> tuple[int, int]:
    user_ids = get_all_user_ids()
    sent = 0
    failed = 0

    for user_id in user_ids:
        try:
            await app.bot.send_message(chat_id=user_id, text=text)
            sent += 1
        except Exception:
            failed += 1

    return sent, failed


# ========= COMMANDS =========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    add_user(user.id, user.username, user.first_name)

    await update.message.reply_text(
        "Салам 👋\n\n"
        "Отправь ссылку на:\n"
        "- Instagram\n"
        "- YouTube\n"
        "- TikTok\n\n"
        "Если это YouTube и нужна только музыка — отправь так:\n"
        "audio https://ссылка\n\n"
        "Админ-команды:\n"
        "/users\n"
        "/stats\n"
        "/broadcast"
    )


async def users_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_ID:
        return

    count = get_users_count()
    await update.message.reply_text(f"👥 Пользователей: {count}")


async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_ID:
        return

    stats = get_stats_summary()
    if not stats:
        await update.message.reply_text("Пока статистики нет.")
        return

    text = "📊 Статистика скачиваний:\n\n"
    for platform, media_type, count in stats:
        text += f"{platform} | {media_type} — {count}\n"

    await update.message.reply_text(text)


async def broadcast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_ID:
        return

    broadcast_waiting.add(update.effective_user.id)
    await update.message.reply_text("Отправь следующим сообщением текст для рассылки.")


# ========= MAIN MESSAGE HANDLER =========
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    user = update.effective_user

    if not message or not user:
        return

    add_user(user.id, user.username, user.first_name)

    text = (message.text or "").strip()

    # --- BROADCAST MODE ---
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

    # --- URL ---
    url = extract_url(text)
    if not url:
        await message.reply_text(
            "Отправь ссылку.\n\n"
            "Примеры:\n"
            "https://www.instagram.com/reel/...\n"
            "https://youtu.be/...\n"
            "audio https://youtu.be/..."
        )
        return

    platform = detect_platform(url)
    want_audio = text.lower().startswith("audio ")

    if platform == "unknown":
        await message.reply_text("Пока поддерживаются только Instagram, YouTube и TikTok.")
        return

    status = await message.reply_text("⏳ Обрабатываю ссылку...")

    file_path = None
    try:
        if want_audio:
            if platform != "youtube":
                await status.edit_text("Аудио-режим сейчас работает только для YouTube.")
                return

            file_path, title = download_audio(url)

            if not Path(file_path).exists():
                await status.edit_text("Не удалось подготовить mp3.")
                return

            with open(file_path, "rb") as audio_file:
                await message.reply_audio(
                    audio=audio_file,
                    title=title[:64],
                    caption="🎵 Готово"
                )

            add_stat(user.id, platform, "audio")
            await status.delete()
            return

        # video mode
        file_path, title = download_video(url)

        if not Path(file_path).exists():
            await status.edit_text("Файл не найден после загрузки.")
            return

        with open(file_path, "rb") as video_file:
            await message.reply_video(
                video=video_file,
                caption=f"✅ {title[:800]}"
            )

        media_type = "video"
        if platform == "instagram":
            media_type = "reel_or_post"

        add_stat(user.id, platform, media_type)
        await status.delete()

    except Exception as e:
        logger.exception("Download error: %s", e)
        await status.edit_text(
            "❌ Ошибка при скачивании.\n\n"
            "Что попробовать:\n"
            "1. Проверь ссылку\n"
            "2. Если Instagram приватный — бот не сможет\n"
            "3. Попробуй еще раз позже"
        )
    finally:
        safe_unlink(file_path)


def main() -> None:
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("users", users_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CommandHandler("broadcast", broadcast_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Bot is running...")
    app.run_polling()


if __name__ == "__main__":
    main()
