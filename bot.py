import os
import yt_dlp
import asyncio
import concurrent.futures
import re
import time
import json
import subprocess
import zipfile
import shutil
import asyncio.exceptions

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackContext, CallbackQueryHandler
load_dotenv()
API_TOKEN = os.getenv("API_TOKEN")
TEMP_DOWNLOAD_FOLDER = os.getenv("TEMP_DOWNLOAD_FOLDER") # 暫存資料夾
TELEGRAM_MAX_SIZE_MB = 50 # 限制檔案大小
executor = concurrent.futures.ThreadPoolExecutor(max_workers=5)  # 控制下載任務數量
last_update_time = 0  # 限制下載進度編輯頻率
def get_video_format_buttons(url):
    try:
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'skip_download': True,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            formats = info.get('formats', [])
            buttons = []
            used_labels = set()
            for f in formats:
                vcodec = f.get('vcodec')
                acodec = f.get('acodec')
                ext = f.get('ext')
                format_id = f.get('format_id')
                height = f.get('height')
                filesize = f.get('filesize')
                resolution = f"{height}p" if height else "未知畫質"
                size_label = f"{round(filesize / (1024*1024), 1)}MB" if filesize else "未知大小"
                # 過濾掉音訊-only與storyboard
                if vcodec and vcodec != "none" and vcodec != "images":
                    label = f"{resolution} [{ext}, {size_label}]"
                    if label not in used_labels:
                        used_labels.add(label)
                        buttons.append([
                            InlineKeyboardButton(label, callback_data=f"{url}|{format_id}")
                        ])
            return buttons
    except Exception as e:
        print(f"畫質按鈕產生錯誤：{e}")
        return []
def get_video_duration(input_path):
    """使用 ffprobe 取得影片總時長（秒）"""
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                input_path
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
        duration = float(result.stdout.decode().strip())
        return duration
    except Exception as e:
        print(f"取得影片時長失敗：{e}")
        return None
def get_video_formats(url):
    """回傳可下載的畫質選項清單，含 video only（補音訊）"""
    try:
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'skip_download': True,
            'force_generic_extractor': False,
            'simulate': True,
            'listformats': True
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            formats = info.get('formats', [])
            format_list = []
            for f in formats:
                vcodec = f.get('vcodec')
                acodec = f.get('acodec')
                ext = f.get('ext')
                format_id = f.get('format_id')
                height = f.get('height')
                filesize = f.get('filesize')
                filesize_mb = f"{round(filesize / (1024*1024), 1)}MB" if filesize else "未知大小"
                # 過濾 storyboard、純音訊
                if vcodec and vcodec != "none" and vcodec != "images":
                    # 解析度顯示處理
                    resolution = f"{height}p" if height else "未知畫質"
                    audio_tag = "🔇（將自動補音訊）" if acodec == "none" else "🔊"
                    format_list.append((height or 0, f"{format_id}: {resolution} [{ext}, {filesize_mb}] {audio_tag}"))
            # 依照高度由高到低排序
            format_list.sort(reverse=True, key=lambda x: x[0])
            return [x[1] for x in format_list]

    except Exception as e:
        print(f"格式查詢錯誤：{e}")
        return []
async def list_formats(update: Update, context: CallbackContext):
    params = update.message.text.split(" ")
    if len(params) < 2:
        await update.message.reply_text("❗ 請提供影片連結")
        return
    url = params[1]
    await update.message.reply_text("📦 讀取畫質清單中，請稍候...")
    formats = get_video_formats(url)
    if not formats:
        await update.message.reply_text("❌ 無法取得格式資訊，請確認影片連結。")
        return
    format_text = "📋 **可用畫質列表**：\n\n" + "\n".join(formats[:20])  # 顯示前 20 筆
    await update.message.reply_text(format_text, parse_mode="Markdown")
async def async_reduce_quality_ffmpeg(input_path, output_path, message, target_bitrate="500k"):
    """
    非同步壓縮影片，並回應進度條。
    """
    total_duration = get_video_duration(input_path)
    if not total_duration:
        await message.edit_text("⚠️ 無法取得影片長度，進度條將停用。")
    command = [
        "ffmpeg",
        "-i", input_path,
        "-b:v", target_bitrate,
        "-bufsize", target_bitrate,
        "-preset", "fast",
        "-y",
        "-progress", "pipe:1",
        "-nostats",
        output_path
    ]
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    await message.edit_text("📉 檔案過大，正在壓縮中...")
    last_report_time = 0
    while True:
        line = await process.stdout.readline()
        if not line:
            break
        decoded_line = line.decode("utf-8").strip()
        if "out_time_ms=" in decoded_line:
            value = decoded_line.split("=")[1]
            try:
                current_ms = float(value)
                current_sec = current_ms / 1_000_000
            except ValueError:
                continue 
            progress_ratio = current_sec / total_duration if total_duration else 0
            percent = int(progress_ratio * 100)
            minutes = int(current_sec // 60)
            secs = int(current_sec % 60)
            bar_length = 10
            filled = int(progress_ratio * bar_length)
            bar = "▓" * filled + "░" * (bar_length - filled)
            if time.time() - last_report_time > 5:
                await message.edit_text(
                    f"📉 檔案過大，正在壓縮中...\n{bar} {percent}%\n🕒 已處理：{minutes:02d}:{secs:02d}"
                )
                last_report_time = time.time()
    return await process.wait() == 0
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
            if current_time - last_update_time >= 2:
                last_update_time = current_time
                asyncio.run_coroutine_threadsafe(
                    message.edit_text(f"⏳ 下載中... {percent}"), loop
                )
        elif d['status'] == 'finished':
            asyncio.run_coroutine_threadsafe(
                message.edit_text("✅ 下載完成，準備發送檔案..."), loop
            )
    def download_video_sync(url, destination_folder, format_type):
        """同步下載函式：依照指定格式下載"""
        ydl_extract_opts = {
            'quiet': True,
            'no_warnings': True,
            'skip_download': True
        }
        with yt_dlp.YoutubeDL(ydl_extract_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            formats = info.get("formats", [])
            if format_type == "audio":
                audio_candidates = [
                    f for f in formats
                    if f.get("vcodec") in [None, "none"]
                    and f.get("acodec") not in [None, "none"]
                ]
                if not audio_candidates:
                    raise Exception("找不到可用的音訊格式")
                best_audio = max(audio_candidates, key=lambda f: f.get("abr", 0))
                chosen_format = best_audio["format_id"]
            else:
                chosen_format = format_type
        ydl_download_opts = {
            'outtmpl': f'{destination_folder}/%(title)s.%(ext)s',
            'progress_hooks': [progress_hook],
            'format': chosen_format,
        }
        if format_type == "audio":
            ydl_download_opts['postprocessors'] = [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }]
        else:
            ydl_download_opts['postprocessors'] = [{
                'key': 'FFmpegVideoConvertor',
                'preferedformat': 'mp4',
            }]
        with yt_dlp.YoutubeDL(ydl_download_opts) as ydl:
            ydl.download([url])
        file_path = max(
            [os.path.join(destination_folder, f) for f in os.listdir(destination_folder)],
            key=os.path.getctime
        )
        return file_path
    try:
        # 先下載檔案，取得檔案目錄
        file_path = await asyncio.wait_for(
            loop.run_in_executor(
                executor,
                download_video_sync,
                url,
                destination_folder,
                format_type
            ),
            timeout=300
        )
        file_size_mb = os.path.getsize(file_path) / (1024 * 1024)
        if file_size_mb > TELEGRAM_MAX_SIZE_MB:
            await message.edit_text("📦 檔案過大，正在打包為 ZIP...")
            zip_path = file_path.replace(".mp4", ".zip")
            try:
                with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
                    zipf.write(file_path, arcname=os.path.basename(file_path))
            except Exception as e:
                await message.edit_text(f"❌ 打包失敗：{e}")
                return

            zip_size_mb = os.path.getsize(zip_path) / (1024 * 1024)
            if zip_size_mb <= TELEGRAM_MAX_SIZE_MB:
                file_path = zip_path
            else:
                await message.edit_text("📉 打包後仍超過 50MB，正在壓縮影片...")
                compressed_path = os.path.join(destination_folder, 'compressed_' + os.path.basename(file_path))
                if not await async_reduce_quality_ffmpeg(file_path, compressed_path, message):
                    await message.edit_text("❌ 壓縮失敗，請稍後再試。")
                    return
                file_path = compressed_path
        await message.edit_text("📤 正在發送檔案...")
        await (message.reply_audio if format_type == "audio" else message.reply_video)(open(file_path, 'rb'))
        os.remove(file_path)
        await message.edit_text("✅ 檔案已成功發送！")
    except asyncio.TimeoutError:
        try:
            files = os.listdir(destination_folder)
            if files:
                latest_file = max(
                    [os.path.join(destination_folder, f) for f in files],
                    key=os.path.getctime
                )
                file_size_mb = os.path.getsize(latest_file) / (1024 * 1024)
                if file_size_mb > TELEGRAM_MAX_SIZE_MB:
                    await message.edit_text("📦 檔案太大，處理 ZIP 或壓縮中...")
                else:
                    await message.edit_text("📤 雖然超時，但檔案已下載完成，正在嘗試發送...")
                    await (message.reply_audio if format_type == "audio" else message.reply_video)(open(latest_file, 'rb'))
                    os.remove(latest_file)
                    await message.edit_text("✅ 檔案已成功發送！")
            else:
                await message.edit_text("⚠️ 超時且找不到檔案，請重新下載。")
        except Exception as e:
            await message.edit_text(f"❌ 超時後處理失敗：{e}")
    except Exception as e:
        await message.edit_text(f"❌ 下載錯誤： {e}")
async def button_callback(update: Update, context: CallbackContext):
    query = update.callback_query
    await query.answer()

    data = query.data.split("|")
    action = data[0]

    if action == "choose_type":
        chosen_type = data[1]
        url = context.user_data.get('download_url')
        if not url:
            await query.message.reply_text("❗ 找不到下載連結，請重新使用 /download 指令。")
            return
        if chosen_type == "audio":
            message = await query.message.reply_text("🎵 開始下載最高音質 mp3...")
            asyncio.create_task(download_video_task(url, TEMP_DOWNLOAD_FOLDER, message, "audio"))
        else:
            await query.message.reply_text("📦 正在讀取可用畫質，請稍候...")
            buttons = get_video_format_buttons(url)
            if not buttons:
                await query.message.reply_text("❌ 無法取得影片格式或影片無法下載。")
                return
            await query.message.reply_text(
                "🎬 請選擇要下載的影片畫質（建議下載最高畫質 + 未知大小，避免檔案毀損）",
                reply_markup=InlineKeyboardMarkup(buttons)
            )
    else:
        url, format_id = data
        message = await query.message.reply_text("🎬 開始下載選定畫質...")
        asyncio.create_task(download_video_task(url, TEMP_DOWNLOAD_FOLDER, message, format_id))
async def download(update: Update, context: CallbackContext):
    params = update.message.text.split(" ")
    if len(params) < 2:
        await update.message.reply_text("❗ 請提供有效的連結！")
        return
    url = params[1]
    context.user_data['download_url'] = url
    buttons = [
        [
            InlineKeyboardButton("🎬 下載影片 (MP4)", callback_data=f"choose_type|video"),
            InlineKeyboardButton("🎵 下載音樂 (MP3)", callback_data=f"choose_type|audio")
        ]
    ]
    await update.message.reply_text("🔽 請選擇要下載的格式：", reply_markup=InlineKeyboardMarkup(buttons))
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
    application.add_handler(CommandHandler('formats', list_formats))
    application.add_handler(CallbackQueryHandler(button_callback))
    application.run_polling()
if __name__ == "__main__":
    main()