import os, re, json, time, subprocess, html, asyncio
from pathlib import Path
from urllib.parse import urlparse, unquote
import requests
from telethon import TelegramClient, events, Button

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
    if not video: raise ValueError("The file does not contain a video stream.")
    duration = float(data.get("format",{}).get("duration") or video.get("duration") or 0)
    if duration <= 0: raise ValueError("Could not determine video duration.")
    return {"duration":duration,"width":video.get("width"),"height":video.get("height"),"codec":video.get("codec_name")}

def download_url(url):
    info = inspect_url(url)
    resolved = resolve_public_page(info)
    if not resolved:
        if info["kind"] == "page":
            raise ValueError("The page did not expose a normal public direct video URL. Login, CAPTCHA, anti-bot and DRM bypass are not supported.")
        resolved = url
    name = Path(unquote(urlparse(resolved).path)).name or "source_video"
    name = re.sub(r"[^A-Za-z0-9_.-]+","_",name)[:120]
    destination = DOWNLOADS / f"{int(time.time())}_{name}"
    with requests.get(resolved, headers={"User-Agent":UA}, stream=True, timeout=(30,120)) as r:
        r.raise_for_status()
        with open(destination,"wb") as f:
            for chunk in r.iter_content(1024*1024):
                if chunk: f.write(chunk)
    return destination, probe(destination)

def make_plan(duration, target, clip_count):
    target = max(1, float(target))
    clip_count = max(1, int(clip_count))
    available = max(0, duration - target)
    if clip_count == 1:
        return [(0, min(target, duration))]
    if available <= 0:
        return [(0, duration)]
    max_start = available
    starts = [
        (max_start * i) / (clip_count - 1)
        for i in range(clip_count)
    ]
    return [
        (start, min(start + target, duration))
        for start in starts
    ]


def render_clip(source, start, end, output):
    vf = "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920"

    run([
        "ffmpeg", "-y",
        "-ss", start, "-i", source,
        "-t", end - start,
        "-vf", vf,
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "23",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "128k",
        "-movflags", "+faststart",
        str(output),
    ])

def telegram_upload(api_id, api_hash, bot_token, channel_id, file_path, caption):
    global TELEGRAM_CLIENT

    if not all([api_id, api_hash, bot_token, channel_id]):
        raise ValueError(
            "Telegram requires API_ID, API_HASH, BOT_TOKEN and CHANNEL_ID."
        )

    from telethon import TelegramClient

    if TELEGRAM_CLIENT is None:
        TELEGRAM_CLIENT = TelegramClient(
            str(ROOT / "telegram_session"),
            int(api_id),
            api_hash,
        )
        TELEGRAM_CLIENT.start(bot_token=bot_token)

    return TELEGRAM_CLIENT.send_file(
        channel_id,
        str(file_path),
        caption=caption[:1024],
        video=True,
        supports_streaming=True,
    )



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


def render_clip(source, start, end, output):
    vf = "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920"
    run([
        "ffmpeg", "-y", "-ss", str(start), "-i", str(source),
        "-t", str(end - start), "-vf", vf,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart", str(output),
    ])


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


async def upload_to_channel(file_path, caption):
    if not CHANNEL_ID:
        raise ValueError("COLABVID_CHANNEL_ID is not configured.")
    await BOT.send_file(
        CHANNEL_ID, str(file_path),
        caption=caption[:1024],
        supports_streaming=True,
    )


async def create_clips(chat_id, source, duration, clip_count, status_message):
    media = probe(source)
    plan = make_plan(media["duration"], duration, clip_count)
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    job_dir = OUTPUTS / f"{chat_id}_{timestamp}"
    job_dir.mkdir(parents=True, exist_ok=True)

    await status_message.edit(
        f"⬇️ Source ready\n🎬 Movie: {media['duration'] / 60:.1f} min\n"
        f"✂️ Creating {len(plan)} clips × {duration}s..."
    )

    for index, (start, end) in enumerate(plan, 1):
        output = job_dir / f"Part_{index:02d}.mp4"
        await status_message.edit(
            f"🎬 Rendering clip {index}/{len(plan)}\n⏱ {end - start:.0f}s"
        )
        await asyncio.to_thread(render_clip, source, start, end, output)
        await status_message.edit(
            f"📤 Uploading clip {index}/{len(plan)} to Telegram..."
        )
        await upload_to_channel(output, f"Part {index:02d} • {duration}s")

    await status_message.edit(
        f"✅ Finished!\nUploaded {len(plan)} clips to the Telegram channel."
    )


async def start_bot():
    global BOT
    if not API_ID or not API_HASH or not BOT_TOKEN or not CHANNEL_ID:
        raise RuntimeError(
            "Set COLABVID_API_ID, COLABVID_API_HASH, COLABVID_BOT_TOKEN "
            "and COLABVID_CHANNEL_ID first."
        )

    BOT = TelegramClient(str(ROOT / "bot_session"), API_ID, API_HASH)
    await BOT.start(bot_token=BOT_TOKEN)

    @BOT.on(events.NewMessage(incoming=True))
    async def on_message(event):
        if not event.is_private:
            return
        text = (event.raw_text or "").strip()

        if text in ("/start", "/help"):
            await event.respond(
                "🎬 Colabvid Bot\n\n"
                "Send me a public video URL.\n"
                "Then choose reel duration and number of clips.\n\n"
                "Example: https://example.com/video"
            )
            return

        if not re.match(r"^https?://", text, re.I):
            await event.respond("📎 Send a video URL starting with http:// or https://")
            return

        status = await event.respond("🔎 Inspecting URL...")
        try:
            info = inspect_url(text)
            resolved = resolve_public_page(info)
            if not resolved:
                if info["kind"] == "page":
                    raise ValueError("I couldn't find a normal public direct video URL on that page.")
                resolved = text

            USER_JOBS[event.chat_id] = {
                "url": text,
                "resolved": resolved,
                "info": info,
            }
            await status.edit(
                "✅ URL inspected.\n\nSelect your Instagram Reel duration:",
                buttons=duration_buttons(),
            )
        except Exception as e:
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
        await event.edit(
            f"✅ Reel duration: {duration}s\n\nNow select the number of clips:",
            buttons=clip_buttons(),
        )

    @BOT.on(events.CallbackQuery(pattern=rb"clips:(\d+)"))
    async def on_clips(event):
        chat_id = event.chat_id
        job = USER_JOBS.get(chat_id)
        if not job or "duration" not in job:
            await event.answer("Send a video URL first.", alert=True)
            return

        clip_count = int(event.pattern_match.group(1))
        job["clip_count"] = clip_count
        await event.edit(
            f"🚀 Starting...\n\n🎞 Reel duration: {job['duration']}s\n"
            f"🔢 Clips: {clip_count}\n\nDownloading and processing now..."
        )

        try:
            source, _ = await asyncio.to_thread(download_url, job["url"])
            await create_clips(
                chat_id, source, job["duration"], clip_count, event
            )
        except Exception as e:
            await event.edit(f"❌ {type(e).__name__}: {e}")
        finally:
            USER_JOBS.pop(chat_id, None)

    print("🤖 Colabvid Telegram bot is running...")
    print("Send /start to your bot.")
    await BOT.run_until_disconnected()
