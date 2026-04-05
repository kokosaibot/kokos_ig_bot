import asyncio
import glob
import os
import re
import uuid
from pathlib import Path

import yt_dlp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

BOT_TOKEN = os.getenv("BOT_TOKEN", "8743059371:AAGRTfddWmFwjpfBzdp20_HbeOcWk0ifd3A")

DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)

INSTAGRAM_RE = re.compile(r"(https?://)?(www\.)?instagram\.com/(reel|p)/[^\s]+", re.IGNORECASE)
TIKTOK_RE = re.compile(r"(https?://)?(www\.)?(vm\.)?tiktok\.com/[^\s]+|(https?://)?(www\.)?tiktok\.com/@[^\s]+/video/\d+", re.IGNORECASE)
YOUTUBE_RE = re.compile(
    r"(https?://)?(www\.)?(youtube\.com/watch\?v=[\w\-]+[^\s]*|youtu\.be/[\w\-]+[^\s]*)",
    re.IGNORECASE,
)


def normalize_url(url: str) -> str:
    if not url.startswith("http"):
        return "https://" + url
    return url


def extract_supported_url(text: str) -> tuple[str | None, str | None]:
    for platform, pattern in (
        ("instagram", INSTAGRAM_RE),
        ("tiktok", TIKTOK_RE),
        ("youtube", YOUTUBE_RE),
    ):
        match = pattern.search(text or "")
        if match:
            return normalize_url(match.group(0)), platform
    return None, None


def cleanup_files(prefix: str) -> None:
    for file_path in glob.glob(f"downloads/{prefix}.*"):
        try:
            os.remove(file_path)
        except OSError:
            pass


def get_text_from_url(url: str) -> str:
    ydl_opts = {
        "quiet": True,
        "skip_download": True,
        "noplaylist": True,
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)

    description = (
        info.get("description")
        or info.get("title")
        or "Текст / описание не найдено."
    )
    return description


def download_best_video(url: str, prefix: str) -> str:
    cleanup_files(prefix)

    ydl_opts = {
        "outtmpl": str(DOWNLOAD_DIR / f"{prefix}.%(ext)s"),
        "format": "mp4/best",
        "quiet": True,
        "noplaylist": True,
        "windowsfilenames": True,
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])

    files = glob.glob(f"downloads/{prefix}.*")
    if not files:
        raise FileNotFoundError("Файл не найден после скачивания.")
    return files[0]


def get_youtube_options(url: str) -> dict:
    ydl_opts = {
        "quiet": True,
        "skip_download": True,
        "noplaylist": True,
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)

    formats = info.get("formats", [])

    progressive = []
    seen_heights = set()

    # Готовые видео с аудио внутри, чтобы работало без ffmpeg
    for f in sorted(
        formats,
        key=lambda x: (x.get("height") or 0, x.get("tbr") or 0),
        reverse=True,
    ):
        if (
            f.get("vcodec") != "none"
            and f.get("acodec") != "none"
            and f.get("ext") == "mp4"
            and f.get("format_id")
            and f.get("height")
        ):
            height = f["height"]
            if height not in seen_heights:
                seen_heights.add(height)
                progressive.append(
                    {
                        "format_id": f["format_id"],
                        "label": f"{height}p",
                    }
                )

    audio_formats = []
    seen_audio_ext = set()

    for f in formats:
        if (
            f.get("acodec") != "none"
            and f.get("vcodec") == "none"
            and f.get("format_id")
        ):
            ext = f.get("ext") or "audio"
            abr = f.get("abr")
            label = f"{ext}"
            if abr:
                label = f"{ext} {int(abr)}k"

            key = (ext, int(abr or 0))
            if key not in seen_audio_ext:
                seen_audio_ext.add(key)
                audio_formats.append(
                    {
                        "format_id": f["format_id"],
                        "label": label,
                    }
                )

    # ограничим, чтобы не завалить кнопками
    progressive = progressive[:6]
    audio_formats = audio_formats[:4]

    return {
        "title": info.get("title") or "YouTube video",
        "video_options": progressive,
        "audio_options": audio_formats,
    }


def download_youtube_format(url: str, format_id: str, prefix: str) -> str:
    cleanup_files(prefix)

    ydl_opts = {
        "outtmpl": str(DOWNLOAD_DIR / f"{prefix}.%(ext)s"),
        "format": format_id,
        "quiet": True,
        "noplaylist": True,
        "windowsfilenames": True,
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])

    files = glob.glob(f"downloads/{prefix}.*")
    if not files:
        raise FileNotFoundError("Файл не найден после скачивания.")
    return files[0]


def get_session_store(context: ContextTypes.DEFAULT_TYPE) -> dict:
    store = context.application.bot_data.setdefault("sessions", {})
    return store


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    await update.message.reply_text(
        "Скинь ссылку Instagram / TikTok / YouTube.\n"
        "Instagram и TikTok — скачаю сразу.\n"
        "YouTube — дам кнопки качества и аудио."
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    await update.message.reply_text(
        "Поддержка:\n"
        "• Instagram\n"
        "• TikTok\n"
        "• YouTube\n\n"
        "В группе бот реагирует только на сообщения со ссылкой."
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return

    text = update.message.text or ""
    url, platform = extract_supported_url(text)

    # в группах молчим, если нет ссылки
    if not url:
        if update.effective_chat and update.effective_chat.type == "private":
            await update.message.reply_text("Скинь ссылку Instagram / TikTok / YouTube.")
        return

    session_id = uuid.uuid4().hex[:8]
    store = get_session_store(context)
    store[session_id] = {"url": url, "platform": platform}

    if platform in ("instagram", "tiktok"):
        await update.message.reply_text("Скачиваю...")

        try:
            file_path = await asyncio.to_thread(download_best_video, url, f"media_{session_id}")

            with open(file_path, "rb") as media_file:
                await update.message.reply_document(
                    document=media_file,
                    filename=os.path.basename(file_path),
                )

            keyboard = [
                [InlineKeyboardButton("📝 Получить текст", callback_data=f"txt:{session_id}")]
            ]
            await update.message.reply_text(
                "Готово 👇",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )

        except Exception as e:
            await update.message.reply_text(f"Ошибка: {e}")

        return

    if platform == "youtube":
        try:
            yt_data = await asyncio.to_thread(get_youtube_options, url)
            store[session_id]["yt"] = yt_data

            keyboard = []

            for item in yt_data["video_options"]:
                keyboard.append([
                    InlineKeyboardButton(
                        f"🎬 {item['label']}",
                        callback_data=f"ytv:{session_id}:{item['format_id']}",
                    )
                ])

            for item in yt_data["audio_options"]:
                keyboard.append([
                    InlineKeyboardButton(
                        f"🎵 {item['label']}",
                        callback_data=f"yta:{session_id}:{item['format_id']}",
                    )
                ])

            keyboard.append([
                InlineKeyboardButton("📝 Описание", callback_data=f"txt:{session_id}")
            ])

            await update.message.reply_text(
                f"Выбери, что скачать:\n{yt_data['title']}",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )

        except Exception as e:
            await update.message.reply_text(f"Ошибка YouTube: {e}")


async def handle_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return

    await query.answer()

    store = get_session_store(context)

    data = query.data or ""
    parts = data.split(":")

    if len(parts) < 2:
        await query.message.reply_text("Ошибка кнопки.")
        return

    action = parts[0]
    session_id = parts[1]

    session = store.get(session_id)
    if not session:
        await query.message.reply_text("Сессия устарела. Отправь ссылку заново.")
        return

    url = session["url"]

    try:
        if action == "txt":
            await query.message.reply_text("Получаю текст...")
            description = await asyncio.to_thread(get_text_from_url, url)

            if len(description) > 4000:
                description = description[:4000] + "\n\n...текст обрезан"

            await query.message.reply_text(description)
            return

        if action in ("ytv", "yta"):
            if len(parts) < 3:
                await query.message.reply_text("Ошибка формата.")
                return

            format_id = parts[2]
            await query.message.reply_text("Скачиваю...")

            prefix = f"{action}_{session_id}"
            file_path = await asyncio.to_thread(download_youtube_format, url, format_id, prefix)

            with open(file_path, "rb") as media_file:
                await query.message.reply_document(
                    document=media_file,
                    filename=os.path.basename(file_path),
                )
            return

        await query.message.reply_text("Неизвестная команда.")

    except Exception as e:
        await query.message.reply_text(f"Ошибка: {e}")


def main() -> None:
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(handle_buttons))

    print("Бот запущен")
    app.run_polling()


if __name__ == "__main__":
    main()
