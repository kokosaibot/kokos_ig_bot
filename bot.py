import asyncio
import glob
import os
import re
from pathlib import Path

import yt_dlp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

BOT_TOKEN = "8743059371:AAGRTfddWmFwjpfBzdp20_HbeOcWk0ifd3A"

INSTAGRAM_REGEX = r"(https?://)?(www\.)?instagram\.com/(reel|p)/[^\s]+"

DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)


def extract_url(text: str) -> str | None:
    match = re.search(INSTAGRAM_REGEX, text)
    if not match:
        return None

    url = match.group(0)
    if not url.startswith("http"):
        url = "https://" + url
    return url


def cleanup_old_files() -> None:
    for pattern in ["downloads/video.*"]:
        for file_path in glob.glob(pattern):
            try:
                os.remove(file_path)
            except OSError:
                pass


def get_post_text(url: str) -> str:
    ydl_opts = {
        "quiet": True,
        "skip_download": True,
        "noplaylist": True,
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)

    description = info.get("description") or "Текст поста не найден."
    return description


def download_video(url: str) -> str:
    cleanup_old_files()

    ydl_opts = {
        "outtmpl": str(DOWNLOAD_DIR / "video.%(ext)s"),
        "format": "mp4/best",
        "quiet": True,
        "noplaylist": True,
        "windowsfilenames": True,
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])

    files = glob.glob("downloads/video.*")
    if not files:
        raise FileNotFoundError("Видео не найдено после скачивания.")
    return files[0]


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return

    text = update.message.text or ""
    url = extract_url(text)

    if not url:
        await update.message.reply_text("Скинь нормальную ссылку Instagram.")
        return

    context.user_data["url"] = url

    await update.message.reply_text("Скачиваю видео...")

    try:
        file_path = await asyncio.to_thread(download_video, url)

        with open(file_path, "rb") as video_file:
            await update.message.reply_document(
                document=video_file,
                filename=os.path.basename(file_path),
            )

        keyboard = [
            [InlineKeyboardButton("📝 Получить текст поста", callback_data="text")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await update.message.reply_text(
            "Видео готово. Если нужен текст поста — нажми кнопку 👇",
            reply_markup=reply_markup,
        )

    except Exception as e:
        await update.message.reply_text(f"Ошибка: {e}")


async def handle_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return

    await query.answer()

    url = context.user_data.get("url")
    if not url:
        await query.message.reply_text("Ошибка: ссылка не найдена. Отправь ссылку заново.")
        return

    try:
        if query.data == "text":
            await query.message.reply_text("Получаю текст поста...")

            description = await asyncio.to_thread(get_post_text, url)

            if len(description) > 4000:
                description = description[:4000] + "\n\n...текст обрезан"

            await query.message.reply_text(description)

    except Exception as e:
        await query.message.reply_text(f"Ошибка: {e}")


def main() -> None:
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(handle_buttons))

    print("Бот запущен")
    app.run_polling()


if __name__ == "__main__":
    main()