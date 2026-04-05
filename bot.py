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

# 🔐 Токен берем из Railway Variables
BOT_TOKEN = os.getenv("BOT_TOKEN")

DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)

INSTAGRAM_RE = re.compile(r"(https?://)?(www\.)?instagram\.com/(reel|p)/[^\s]+", re.IGNORECASE)
TIKTOK_RE = re.compile(r"(https?://)?(www\.)?(vm\.)?tiktok\.com/[^\s]+|(https?://)?(www\.)?tiktok\.com/@[^\s]+/video/\d+", re.IGNORECASE)
YOUTUBE_RE = re.compile(r"(https?://)?(www\.)?(youtube\.com|youtu\.be)/[^\s]+", re.IGNORECASE)


def normalize_url(url: str) -> str:
    return url if url.startswith("http") else "https://" + url


def extract_url(text: str):
    for name, pattern in [
        ("instagram", INSTAGRAM_RE),
        ("tiktok", TIKTOK_RE),
        ("youtube", YOUTUBE_RE),
    ]:
        match = pattern.search(text or "")
        if match:
            return normalize_url(match.group(0)), name
    return None, None


def cleanup(prefix):
    for f in glob.glob(f"downloads/{prefix}.*"):
        try:
            os.remove(f)
        except:
            pass


def download_video(url, prefix):
    cleanup(prefix)

    ydl_opts = {
        "outtmpl": f"downloads/{prefix}.%(ext)s",
        "format": "mp4/best",
        "quiet": True,
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])

    files = glob.glob(f"downloads/{prefix}.*")
    return files[0] if files else None


def get_text(url):
    ydl_opts = {"quiet": True, "skip_download": True}
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
    return info.get("description") or info.get("title") or "Нет текста"


def get_youtube_formats(url):
    ydl_opts = {"quiet": True, "skip_download": True}

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)

    formats = info.get("formats", [])

    video = []
    audio = []

    for f in formats:
        if f.get("vcodec") != "none" and f.get("acodec") != "none":
            if f.get("height"):
                video.append((f["format_id"], f"{f['height']}p"))

        if f.get("acodec") != "none" and f.get("vcodec") == "none":
            audio.append((f["format_id"], f"{f.get('ext')}"))

    return info.get("title"), video[:6], audio[:4]


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Скинь ссылку Instagram / TikTok / YouTube"
    )


async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    text = update.message.text or ""
    url, platform = extract_url(text)

    if not url:
        return

    session = str(uuid.uuid4())[:8]
    context.bot_data[session] = {"url": url}

    if platform in ["instagram", "tiktok"]:
        await update.message.reply_text("Скачиваю...")

        file = await asyncio.to_thread(download_video, url, session)

        if file:
            with open(file, "rb") as f:
                await update.message.reply_document(f)

        kb = [[InlineKeyboardButton("📝 Текст", callback_data=f"text:{session}")]]
        await update.message.reply_text("Готово", reply_markup=InlineKeyboardMarkup(kb))

    elif platform == "youtube":
        title, videos, audios = await asyncio.to_thread(get_youtube_formats, url)

        kb = []

        for f_id, label in videos:
            kb.append([InlineKeyboardButton(f"🎬 {label}", callback_data=f"v:{session}:{f_id}")])

        for f_id, label in audios:
            kb.append([InlineKeyboardButton(f"🎵 {label}", callback_data=f"a:{session}:{f_id}")])

        kb.append([InlineKeyboardButton("📝 Текст", callback_data=f"text:{session}")])

        await update.message.reply_text(title, reply_markup=InlineKeyboardMarkup(kb))


async def buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data.split(":")
    action = data[0]
    session = data[1]

    url = context.bot_data.get(session, {}).get("url")

    if not url:
        await query.message.reply_text("Ошибка")
        return

    if action == "text":
        text = await asyncio.to_thread(get_text, url)
        await query.message.reply_text(text[:4000])

    if action in ["v", "a"]:
        fmt = data[2]
        file = await asyncio.to_thread(download_video, url, session + "_yt")

        if file:
            with open(file, "rb") as f:
                await query.message.reply_document(f)


def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT, handle))
    app.add_handler(CallbackQueryHandler(buttons))

    print("Bot started")
    app.run_polling()


if __name__ == "__main__":
    main()
