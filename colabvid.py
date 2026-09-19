import os, re, json, time, subprocess, html, asyncio
from pathlib import Path
from urllib.parse import urlparse, unquote
import requests
from telethon import TelegramClient, events, Button
from telethon.tl.types import DocumentAttributeVideo

ROOT = Path("/content/colabvid")
DOWNLOADS = ROOT / "downloads"
OUTPUTS = ROOT / "outputs"
DOWNLOADS.mkdir(parents=True, exist_ok=True)
OUTPUTS.mkdir(parents=True, exist_ok=True)

UA = "Mozilla/5.0 Colabvid/2.0"
API_ID = int(os.environ.get("COLABVID_API_ID", "0"))
API_HASH = os.environ.get("COLABVID_API_HASH", "")
BOT_TOKEN = os.environ.get("COLABVID_BOT_TOKEN", "")
CHANNEL_ID = os.environ.get("COLABVID_CHANNEL_ID", "")
BOT = None
USER_JOBS = {}

def log(message):
    print(f"[Colabvid {time.strftime('%H:%M:%S')}] {message}", flush=True)

def run(args, capture=False):
    return subprocess.run([str(x) for x in args], check=True, text=True, capture_output=capture)

def inspect_url(url):
    url = url.strip()
    if not re.match(r"^https?://", url, re.I):
        raise ValueError("Only HTTP/HTTPS URLs are supported.")
    p = urlparse(url)
    host = (p.hostname or "").lower()
    if "pixeldrain" in host:
        m = re.search(r"/(?:u|l)/([A-Za-z0-9_-]+)", p.path)
        if m:
            fid = m.group(1)
            r = requests.get(f"https://pixeldrain.com/api/file/{fid}/info", headers={"User-Agent": UA}, timeout=30)
            r.raise_for_status()
            info = r.json()
            return {"kind":"pixeldrain","final_url":f"https://pixeldrain.com/api/file/{fid}","content_type":info.get("mime_type",""),"name":info.get("name",""),"size":info.get("size")}
    r = requests.head(url, headers={"User-Agent": UA}, allow_redirects=True, timeout=30)
    ct = (r.headers.get("content-type") or "").split(";")[0].lower()
    kind = "media" if ct.startswith("video/") or ct == "application/octet-stream" else ("page" if "text/html" in ct else "unknown")
    return {"kind":kind,"source_url":url,"final_url":r.url,"status":r.status_code,"content_type":ct}

def resolve_public_page(info):
    if info["kind"] == "pixeldrain": return info["final_url"]
    if info["kind"] == "media": return info["final_url"]
    if info["kind"] != "page": return None
    r = requests.get(info["final_url"], headers={"User-Agent": UA}, timeout=30)
    r.raise_for_status()
    candidates = re.findall(r'https?://[^"\'<>\s]+', r.text, re.I)
    for value in candidates:
        value = html.unescape(value)
        if value.split("?")[0].lower().endswith((".mp4",".webm",".mkv",".mov",".m4v")):
            return value
    return None

def probe(path):
    result = run(["ffprobe","-v","error","-show_format","-show_streams","-of","json",str(path)], capture=True)
    data = json.loads(result.stdout)
    video = next((s for s in data.get("streams",[]) if s.get("codec_type")=="video"), None)
    if not video:
        raise ValueError("The file does not contain a video stream.")
    duration = float(data.get("format",{}).get("duration") or video.get("duration") or 0)
    if duration <= 0:
        raise ValueError("Could not determine video duration.")
    return {"duration":duration,"width":int(video.get("width") or 1080),"height":int(video.get("height") or 1920),"codec":video.get("codec_name")}

def download_url(url, progress_callback=None):
    info = inspect_url(url)
    resolved = resolve_public_page(info)
    if not resolved:
        if info["kind"] == "page":
            raise ValueError("The page did not expose a normal public direct video URL.")
        resolved = url
    name = Path(unquote(urlparse(resolved).path)).name or "source_video"
    name = re.sub(r"[^A-Za-z0-9_.-]+", "_", name)[:120]
    destination = DOWNLOADS / f"{int(time.time())}_{name}"
    log(f"Download started: {resolved}")
    with requests.get(resolved, headers={"User-Agent": UA}, stream=True, timeout=(30, 120)) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length") or 0)
        log(f"Download response: HTTP {r.status_code}, size={'unknown' if not total else f'{total/1024/1024:.1f} MB'}")
        downloaded = 0
        started = time.time()
        last_update = 0.0
        with open(destination, "wb") as f:
            for chunk in r.iter_content(1024 * 1024):
                if not chunk:
                    continue
                f.write(chunk)
                downloaded += len(chunk)
                now = time.time()
                if progress_callback and (now - last_update >= 4.0 or (total and downloaded >= total)):
                    speed = downloaded / max(now - started, 0.001)
                    progress_callback(downloaded, total, speed)
                    last_update = now
    log(f"Download complete: {destination} ({downloaded/1024/1024:.1f} MB)")
    return destination, probe(destination)

def make_plan(duration, target, clip_count):
    target = max(1, float(target))
    clip_count = max(1, int(clip_count))
    if duration <= target:
        return [(0, duration)]
    if clip_count == 1:
        return [(0, min(target, duration))]
    max_start = duration - target
    starts = [max_start * i / (clip_count - 1) for i in range(clip_count)]
    return [(start, min(start + target, duration)) for start in starts]

def duration_buttons():
    values = [15, 30, 45, 60, 90, 120]
    return [
        [Button.inline(f"{v}s", f"duration:{v}") for v in values[:3]],
        [Button.inline(f"{v}s", f"duration:{v}") for v in values[3:]],
    ]

def clip_buttons():
    values = [1, 5, 10, 15, 20, 30, 50, 100]
    return [
        [Button.inline(str(v), f"clips:{v}") for v in values[:4]],
        [Button.inline(str(v), f"clips:{v}") for v in values[4:]],
    ]

async def render_clip(source, start, end, output, progress_callback=None):
    vf = "scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2:color=black"
    duration = max(0.1, end - start)
    log(f"Clipping: {output.name} ({start:.1f}s -> {end:.1f}s)")
    cmd = ["ffmpeg", "-y", "-ss", str(start), "-i", str(source), "-t", str(duration),
           "-vf", vf, "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
           "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
           "-movflags", "+faststart", "-progress", "pipe:1", "-nostats", str(output)]
    process = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    last_update = 0.0
    while True:
        line = await process.stdout.readline()
        if not line:
            break
        text = line.decode("utf-8", "ignore").strip()
        if text.startswith("out_time_ms="):
            try:
                elapsed = int(text.split("=", 1)[1]) / 1_000_000
                percent = min(100, elapsed / duration * 100)
                now = time.time()
                if progress_callback and (now - last_update >= 4.0 or percent >= 100):
                    await progress_callback(percent, elapsed, duration)
                    last_update = now
            except ValueError:
                pass
    if await process.wait() != 0:
        raise RuntimeError(f"FFmpeg failed while creating {output.name}.")
    log(f"Clip complete: {output}")

def create_thumbnail(video_path, thumb_path):
    # Telegram works best with a JPEG thumbnail; generate it from ~1 second in.
    run([
        "ffmpeg", "-y", "-ss", "1", "-i", str(video_path),
        "-frames:v", "1", "-vf", "scale=320:-1",
        "-q:v", "4", str(thumb_path)
    ])
    return thumb_path

async def upload_to_channel(file_path, caption):
    if not CHANNEL_ID:
        raise ValueError("COLABVID_CHANNEL_ID is not configured.")

    target = CHANNEL_ID.strip()
    try:
        if target.lstrip("-").isdigit():
            target = int(target)
        else:
            target = await BOT.get_entity(target.lstrip("@"))
    except Exception as e:
        raise ValueError("CHANNEL_ID must be the numeric Telegram channel ID, usually starting with -100.") from e

    media = probe(file_path)
    duration = max(1, int(round(media["duration"])))
    width = media["width"]
    height = media["height"]

    thumb = file_path.with_suffix(".jpg")
    create_thumbnail(file_path, thumb)

    log(f"Uploading: {file_path.name} | duration={duration}s | {width}x{height} | thumbnail={thumb.name}")

    attributes = [
        DocumentAttributeVideo(
            duration=duration,
            w=width,
            h=height,
            supports_streaming=True,
        )
    ]

    try:
        await BOT.send_file(
            target,
            str(file_path),
            caption=caption[:1024],
            thumb=str(thumb),
            attributes=attributes,
            supports_streaming=True,
            force_document=False,
        )
    finally:
        try:
            thumb.unlink(missing_ok=True)
        except Exception:
            pass

    log(f"Upload complete: {file_path.name}")

async def create_clips(chat_id, source, duration, clip_count, status_message):
    media = probe(source)
    plan = make_plan(media["duration"], duration, clip_count)
    job_dir = OUTPUTS / f"{chat_id}_{time.strftime('%Y%m%d_%H%M%S')}"
    job_dir.mkdir(parents=True, exist_ok=True)
    log(f"Processing job: {len(plan)} clips x {duration}s from {media['duration']:.1f}s")

    await status_message.edit(f"📥 Download complete\n🎬 Source: {media['duration'] / 60:.1f} min\n✂️ Preparing {len(plan)} clips × {duration}s...")

    for index, (start, end) in enumerate(plan, 1):
        output = job_dir / f"Part_{index:02d}.mp4"

        async def render_status(percent, elapsed, total):
            await status_message.edit(f"🎬 Clipping {index}/{len(plan)}\n📊 Progress: {percent:.0f}%\n⏱ {elapsed:.0f}s / {total:.0f}s")
            log(f"Clipping {index}/{len(plan)}: {percent:.0f}%")

        await status_message.edit(f"🎬 Clipping {index}/{len(plan)}\n⏱ {end - start:.0f}s")
        await render_clip(source, start, end, output, render_status)
        await status_message.edit(f"📤 Uploading {index}/{len(plan)}...\n✅ Clip {index} ready")
        await upload_to_channel(output, f"Part {index:02d} • {duration}s")

    log(f"Job complete: uploaded {len(plan)} clips")
    await status_message.edit(f"✅ Finished!\nUploaded {len(plan)} clips to the Telegram channel.")

async def start_bot():
    global BOT
    if not API_ID or not API_HASH or not BOT_TOKEN or not CHANNEL_ID:
        raise RuntimeError("Set COLABVID_API_ID, COLABVID_API_HASH, COLABVID_BOT_TOKEN and COLABVID_CHANNEL_ID first.")

    log("Starting Colabvid Telegram bot...")
    log(f"Channel ID: {CHANNEL_ID}")
    BOT = TelegramClient(str(ROOT / "bot_session"), API_ID, API_HASH)
    await BOT.start(bot_token=BOT_TOKEN)
    log("Telegram bot connected successfully.")
    log("Waiting for /start or a video URL...")

    @BOT.on(events.NewMessage(incoming=True))
    async def on_message(event):
        if not event.is_private:
            return
        text = (event.raw_text or "").strip()
        log(f"Message received from chat {event.chat_id}: {text[:120]}")

        if text in ("/start", "/help"):
            await event.respond("🎬 Colabvid Bot\n\nSend me a public video URL.\nThen choose reel duration and number of clips.")
            return

        if not re.match(r"^https?://", text, re.I):
            await event.respond("📎 Send a video URL starting with http:// or https://")
            return

        status = await event.respond("🔎 Inspecting URL...")
        try:
            info = inspect_url(text)
            resolved = resolve_public_page(info)
            log(f"URL inspected: kind={info['kind']} type={info.get('content_type','unknown')}")
            if not resolved:
                if info["kind"] == "page":
                    raise ValueError("I couldn't find a normal public direct video URL on that page.")
                resolved = text
            USER_JOBS[event.chat_id] = {"url": text, "resolved": resolved, "info": info}
            await status.edit("✅ URL inspected.\n\nSelect your Instagram Reel duration:", buttons=duration_buttons())
        except Exception as e:
            log(f"URL inspection error: {type(e).__name__}: {e}")
            await status.edit(f"❌ {type(e).__name__}: {e}")

    @BOT.on(events.CallbackQuery(pattern=rb"duration:(\d+)"))
    async def on_duration(event):
        chat_id = event.chat_id
        job = USER_JOBS.get(chat_id)
        if not job:
            await event.answer("Send a video URL first.", alert=True)
            return
        duration = int(event.pattern_match.group(1))
        job["duration"] = duration
        log(f"Chat {chat_id} selected duration: {duration}s")
        await event.edit(f"✅ Reel duration: {duration}s\n\nNow select the number of clips:", buttons=clip_buttons())

    @BOT.on(events.CallbackQuery(pattern=rb"clips:(\d+)"))
    async def on_clips(event):
        chat_id = event.chat_id
        job = USER_JOBS.get(chat_id)
        if not job or "duration" not in job:
            await event.answer("Send a video URL first.", alert=True)
            return
        clip_count = int(event.pattern_match.group(1))
        job["clip_count"] = clip_count
        log(f"Chat {chat_id} selected {clip_count} clips at {job['duration']}s")
        await event.edit(f"🚀 Starting...\n\n🎞 Reel duration: {job['duration']}s\n🔢 Clips: {clip_count}\n\nDownloading and processing now...")
        try:
            loop = asyncio.get_running_loop()
            last_download_update = [0.0]

            def download_progress(downloaded, total, speed):
                now = time.time()
                if now - last_download_update[0] < 4.0 and not (total and downloaded >= total):
                    return
                last_download_update[0] = now
                if total:
                    text = f"⬇️ Downloading source\n📊 {downloaded / total * 100:.0f}% • {downloaded / 1024 / 1024:.1f}/{total / 1024 / 1024:.1f} MB\n⚡ {speed / 1024 / 1024:.2f} MB/s"
                else:
                    text = f"⬇️ Downloading source\n📦 {downloaded / 1024 / 1024:.1f} MB\n⚡ {speed / 1024 / 1024:.2f} MB/s"
                asyncio.run_coroutine_threadsafe(event.edit(text), loop)

            source, _ = await asyncio.to_thread(download_url, job["url"], download_progress)
            await create_clips(chat_id, source, job["duration"], clip_count, event)
        except Exception as e:
            log(f"Job error: {type(e).__name__}: {e}")
            await event.edit(f"❌ {type(e).__name__}: {e}")
        finally:
            USER_JOBS.pop(chat_id, None)

    print("🤖 Colabvid Telegram bot is running...", flush=True)
    print("📟 Terminal logs are enabled below.", flush=True)
    await BOT.run_until_disconnected()
