import os
import yt_dlp
import asyncio
import concurrent.futures
import re
import time
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackContext, CallbackQueryHandler
load_dotenv()
API_TOKEN = os.getenv("API_TOKEN")
TEMP_DOWNLOAD_FOLDER = os.getenv("TEMP_DOWNLOAD_FOLDER") # 暫存資料夾
TELEGRAM_MAX_SIZE_MB = 50 # 限制檔案大小
executor = concurrent.futures.ThreadPoolExecutor(max_workers=5)  # 控制下載任務數量
last_update_time = 0  # 限制下載進度編輯頻率
def is_live_stream(url):
    """檢查是否為直播"""
    try:
        ydl_opts = {'quiet': True, 'no_warnings': True, 'simulate': True}
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            return info.get('is_live', False)
    except Exception as e:
        print(f"檢測直播錯誤： {e}")
        return False
def strip_ansi_codes(text):
    """移除 yt-dlp 下載進度中的 ANSI 終端機控制碼"""
    ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
    return ansi_escape.sub('', text)
async def download_video_task(url, destination_folder, message, format_type):
    """非同步處理下載並回傳進度"""
    if is_live_stream(url):
        await message.edit_text("❌ 這是直播，無法下載！")
        return
    loop = asyncio.get_running_loop()
    def progress_hook(d):
        """處理下載進度，避免頻繁編輯 Telegram 訊息"""
        global last_update_time
        current_time = time.time()
        if d['status'] == 'downloading':
            percent = strip_ansi_codes(d.get("_percent_str", "0%")).strip()
            if current_time - last_update_time >= 2:  # 更新秒數
                last_update_time = current_time
                asyncio.run_coroutine_threadsafe(
                    message.edit_text(f"⏳ 下載中... {percent}"), loop
                )
        elif d['status'] == 'finished':
            asyncio.run_coroutine_threadsafe(
                message.edit_text("✅ 下載完成，準備發送檔案..."), loop
            )
    def download_video_sync(url, destination_folder, format_type):
        """同步下載函式，在 ThreadPoolExecutor 中執行"""
        ydl_opts = {
            'format': 'bestaudio/best' if format_type == "audio" else 'best',
            'outtmpl': f'{destination_folder}/%(title)s.%(ext)s',
            'progress_hooks': [progress_hook],  # 限制下載進度編輯頻率
            'postprocessors': [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': '192'}] if format_type == "audio" else []
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
        file_path = max([os.path.join(destination_folder, f) for f in os.listdir(destination_folder)], key=os.path.getctime)
        return file_path
    try:
        file_path = await loop.run_in_executor(executor, download_video_sync, url, destination_folder, format_type)
        # 如果檔案大小超過 Telegram 限制就進行壓縮檔案
        if os.path.getsize(file_path) / (1024 * 1024) > TELEGRAM_MAX_SIZE_MB:
            await message.edit_text("📉 檔案過大，正在降低品質...")
            compressed_path = os.path.join(destination_folder, 'compressed_' + os.path.basename(file_path))
            if not reduce_quality_ffmpeg(file_path, compressed_path):
                await message.edit_text("❌ 無法壓縮檔案，請稍後再試！")
                return
            file_path = compressed_path
        await message.edit_text("📤 正在發送檔案...")
        await (message.reply_audio if format_type == "audio" else message.reply_video)(open(file_path, 'rb'))
        os.remove(file_path)
        await message.edit_text("✅ 檔案已成功發送！")
    except Exception as e:
        await message.edit_text(f"❌ 下載錯誤： {e}")
async def button_callback(update: Update, context: CallbackContext):
    query = update.callback_query
    await query.answer()
    url, format_type = query.data.split("|")
    message = await query.message.reply_text(f"🎬 開始下載 {format_type}...")
    # 非同步下載
    asyncio.create_task(download_video_task(url, TEMP_DOWNLOAD_FOLDER, message, format_type))
async def download(update: Update, context: CallbackContext):
    params = update.message.text.split(" ")
    if len(params) < 2:
        await update.message.reply_text("❗ 請提供有效的連結！")
        return
    url = params[1]
    keyboard = [
        [InlineKeyboardButton("🎥 下載影片 (MP4)", callback_data=f"{url}|video")],
        [InlineKeyboardButton("🎵 下載音軌 (MP3)", callback_data=f"{url}|audio")]
    ]
    await update.message.reply_text("🔽 請選擇下載格式：", reply_markup=InlineKeyboardMarkup(keyboard))
async def help_command(update: Update, context: CallbackContext):
    help_text = ("📌 **可用指令列表**：\n\n"
                 "/download <影片連結> - 下載影片或音軌\n"
                 "/start - 查看指令列表\n\n"
                 "📌 **如何使用？**\n"
                 "1️⃣ 發送 /download <影片連結> 來選擇下載影片或音軌。\n"
                 "2️⃣ 選擇格式（影片 / 音樂）。\n"
                 "3️⃣ 等待機器人下載並發送檔案。\n\n"
                 "📌 **支援下載平台**\n"
                 "🔥 YouTube、推特、抖音。")
    await update.message.reply_text(help_text, parse_mode="Markdown")
def main():
    application = ApplicationBuilder().token(API_TOKEN).build()
    application.add_handler(CommandHandler('download', download))
    application.add_handler(CommandHandler('start', help_command))
    application.add_handler(CallbackQueryHandler(button_callback))
    application.run_polling()
if __name__ == "__main__":
    main()