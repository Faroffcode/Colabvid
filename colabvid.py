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
VIDEO_ENCODER = None

def log(message):
    print(f"[Colabvid {time.strftime('%H:%M:%S')}] {message}", flush=True)

def run(args, capture=False):
    return subprocess.run([str(x) for x in args], check=True, text=True, capture_output=capture)

def detect_video_encoder():
    """Detect a usable H.264 encoder once and cache the result."""
    global VIDEO_ENCODER
    if VIDEO_ENCODER in ("nvenc", "cpu"):
        return VIDEO_ENCODER

    try:
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-encoders"],
            check=True, text=True, capture_output=True
        )
        nvenc_available = "h264_nvenc" in result.stdout
        if nvenc_available:
            subprocess.run(
                ["nvidia-smi", "-L"],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
            VIDEO_ENCODER = "nvenc"
            log("NVIDIA GPU detected; using h264_nvenc for this Colab session.")
            return VIDEO_ENCODER
    except Exception as e:
        log(f"NVENC detection unavailable: {e}")

    VIDEO_ENCODER = "cpu"
    log("NVIDIA NVENC unavailable; using CPU libx264 for this Colab session.")
    return VIDEO_ENCODER

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
            r = requests.get(
                f"https://pixeldrain.com/api/file/{fid}/info",
                headers={"User-Agent": UA}, timeout=30
            )
            r.raise_for_status()
            info = r.json()
            return {
                "kind": "pixeldrain",
                "final_url": f"https://pixeldrain.com/api/file/{fid}",
                "content_type": info.get("mime_type", ""),
                "name": info.get("name", ""),
                "size": info.get("size")
            }

    r = requests.head(url, headers={"User-Agent": UA}, allow_redirects=True, timeout=30)
    ct = (r.headers.get("content-type") or "").split(";")[0].lower()
    kind = (
        "media" if ct.startswith("video/") or ct == "application/octet-stream"
        else ("page" if "text/html" in ct else "unknown")
    )
    return {
        "kind": kind,
        "source_url": url,
        "final_url": r.url,
        "status": r.status_code,
        "content_type": ct
    }

def resolve_public_page(info):
    if info["kind"] in ("pixeldrain", "media"):
        return info["final_url"]
    if info["kind"] != "page":
        return None

    r = requests.get(info["final_url"], headers={"User-Agent": UA}, timeout=30)
    r.raise_for_status()
    candidates = re.findall(r'https?://[^"\'<>\s]+', r.text, re.I)
    for value in candidates:
        value = html.unescape(value)
        if value.split("?")[0].lower().endswith((".mp4", ".webm", ".mkv", ".mov", ".m4v")):
            return value
    return None

def probe(path):
    result = run(
        ["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)],
        capture=True
    )
    data = json.loads(result.stdout)
    video = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    if not video:
        raise ValueError("The file does not contain a video stream.")
    duration = float(data.get("format", {}).get("duration") or video.get("duration") or 0)
    if duration <= 0:
        raise ValueError("Could not determine video duration.")
    return {
        "duration": duration,
        "width": int(video.get("width") or 1080),
        "height": int(video.get("height") or 1920),
        "codec": video.get("codec_name")
    }

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
        log(
            f"Download response: HTTP {r.status_code}, "
            f"size={'unknown' if not total else f'{total/1024/1024:.1f} MB'}"
        )
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
                if progress_callback and (
                    now - last_update >= 4.0 or (total and downloaded >= total)
                ):
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

def sanitize_filename(name):
    name = re.sub(r"\.[A-Za-z0-9]{1,5}$", "", name.strip())
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    if not name:
        raise ValueError("File name cannot be empty.")
    return name[:120]

async def render_clip(source, start, end, output, progress_callback=None):
    vf = "scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2:color=black"
    duration = max(0.1, end - start)
    log(f"Clipping: {output.name} ({start:.1f}s -> {end:.1f}s)")

    encoder = detect_video_encoder()
    if encoder == "nvenc":
        video_args = [
            "-c:v", "h264_nvenc", "-preset", "p4",
            "-rc", "vbr", "-cq", "23", "-b:v", "0"
        ]
    else:
        video_args = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23"]

    cmd = [
        "ffmpeg", "-y", "-ss", str(start), "-i", str(source), "-t", str(duration),
        "-vf", vf, *video_args, "-pix_fmt", "yuv420p",
        "-map", "0:v:0", "-map", "0:a?",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart", "-progress", "pipe:1", "-nostats",
        str(output)
    ]

    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )

    last_update = 0.0
    stderr_chunks = []

    async def read_stderr():
        while True:
            line = await process.stderr.readline()
            if not line:
                break
            stderr_chunks.append(line.decode("utf-8", "ignore"))

    stderr_task = asyncio.create_task(read_stderr())

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

    await stderr_task
    return_code = await process.wait()

    if return_code != 0:
        error_text = "".join(stderr_chunks).strip()
        if len(error_text) > 1800:
            error_text = error_text[-1800:]
        raise RuntimeError(
            f"FFmpeg failed while creating {output.name}.\n"
            f"{error_text or 'No FFmpeg error output was available.'}"
        )

    log(f"Clip complete: {output}")

async def retry_async(operation, attempts=3, label="operation"):
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            return await operation()
        except Exception as e:
            last_error = e
            if attempt >= attempts:
                break
            delay = attempt * 2
            log(f"{label} failed (attempt {attempt}/{attempts}): {e}. Retrying in {delay}s...")
            await asyncio.sleep(delay)
    raise last_error


def cleanup_path(path):
    try:
        path = Path(path)
        if path.is_dir():
            import shutil
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink(missing_ok=True)
    except Exception as e:
        log(f"Cleanup warning for {path}: {e}")


def save_job_manifest(manifest_path, data):
    manifest_path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def load_job_manifest(manifest_path):
    return json.loads(manifest_path.read_text(encoding="utf-8"))


def create_thumbnail(video_path, thumb_path):
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
        raise ValueError(
            "CHANNEL_ID must be the numeric Telegram channel ID, usually starting with -100."
        ) from e

    media = probe(file_path)
    duration = max(1, int(round(media["duration"])))
    width = media["width"]
    height = media["height"]

    thumb = file_path.with_suffix(".jpg")
    create_thumbnail(file_path, thumb)

    log(
        f"Uploading: {file_path.name} | duration={duration}s | "
        f"{width}x{height} | thumbnail={thumb.name}"
    )

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

async def create_clips(
    chat_id, source, duration, clip_count, status_message, base_name,
    job_dir=None, manifest_path=None, manifest=None
):
    media = probe(source)
    plan = make_plan(media["duration"], duration, clip_count)
    if job_dir is None:
        job_dir = OUTPUTS / f"{chat_id}_{time.strftime('%Y%m%d_%H%M%S')}"
    job_dir.mkdir(parents=True, exist_ok=True)

    if manifest is None:
        manifest = {
            "chat_id": chat_id,
            "source": str(source),
            "base_name": base_name,
            "duration": duration,
            "clip_count": clip_count,
            "plan": plan,
            "completed": []
        }
    if manifest_path is None:
        manifest_path = job_dir / "job.json"
    save_job_manifest(manifest_path, manifest)

    completed = set(manifest.get("completed", []))
    log(
        f"Processing job: {len(plan)} clips x {duration}s from "
        f"{media['duration']:.1f}s ({len(completed)}/{len(plan)} already complete)"
    )

    await status_message.edit(
        f"📥 Download complete\n"
        f"🎬 Source: {media['duration'] / 60:.1f} min\n"
        f"✂️ {len(plan)} clips × {duration}s\n"
        f"✅ Resuming: {len(completed)}/{len(plan)} complete"
    )

    for index, (start, end) in enumerate(plan, 1):
        output = job_dir / f"{base_name} {index:02d}.mp4"

        if index in completed:
            log(f"Resume: skipping completed clip {index}/{len(plan)}")
            continue

        async def render_status(percent, elapsed, total):
            await status_message.edit(
                f"🎬 Clip {index}/{len(plan)}\n"
                f"📊 Encoding: {percent:.0f}%\n"
                f"⏱ {elapsed:.0f}s / {total:.0f}s\n"
                f"✅ Uploaded: {len(completed)}/{len(plan)}"
            )
            log(f"Clipping {index}/{len(plan)}: {percent:.0f}%")

        await status_message.edit(
            f"🎬 Clip {index}/{len(plan)}\n"
            f"📍 {start:.0f}s → {end:.0f}s\n"
            f"📊 Encoding: 0%\n"
            f"✅ Uploaded: {len(completed)}/{len(plan)}"
        )

        if output.exists() and output.stat().st_size > 0:
            log(f"Resume: reusing existing output {output.name}")
        else:
            await retry_async(
                lambda: render_clip(source, start, end, output, render_status),
                attempts=3,
                label=f"Render clip {index}"
            )

        await status_message.edit(
            f"📤 Uploading clip {index}/{len(plan)}\n"
            f"📦 {output.name}\n"
            f"🔁 Retry protection: 3 attempts"
        )
        await retry_async(
            lambda: upload_to_channel(
                output,
                f"🎬 {base_name}\nPart {index:02d}/{len(plan)} • {duration}s"
            ),
            attempts=3,
            label=f"Upload clip {index}"
        )

        completed.add(index)
        manifest["completed"] = sorted(completed)
        save_job_manifest(manifest_path, manifest)

        await status_message.edit(
            f"✅ Clip {index}/{len(plan)} uploaded\n"
            f"📈 Overall: {len(completed)}/{len(plan)} complete"
        )

    log(f"Job complete: uploaded {len(plan)} clips")
    await status_message.edit(
        f"✅ Finished!\n"
        f"Uploaded {len(plan)} clips to the Telegram channel.\n"
        f"🧹 Cleaning temporary files..."
    )
    cleanup_path(source)
    cleanup_path(job_dir)
    log("Source and temporary output files cleaned up after successful upload.")
    await status_message.edit(
        f"✅ Finished!\n"
        f"Uploaded {len(plan)} clips to the Telegram channel.\n"
        f"🧹 Temporary files cleaned up."
    )

async def start_bot():
    global BOT
    if not API_ID or not API_HASH or not BOT_TOKEN or not CHANNEL_ID:
        raise RuntimeError(
            "Set COLABVID_API_ID, COLABVID_API_HASH, "
            "COLABVID_BOT_TOKEN and COLABVID_CHANNEL_ID first."
        )

    log("Starting Colabvid Telegram bot...")
    log(f"Channel ID: {CHANNEL_ID}")
    log(f"Video encoder: {detect_video_encoder()}")
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
            await event.respond(
                "🎬 Colabvid Bot\n\n"
                "Send me a public video URL.\n"
                "Then choose reel duration and number of clips.\n\n"
                "♻️ Failed jobs keep their progress until they finish."
            )
            return

        if text == "/resume":
            candidates = sorted(
                OUTPUTS.glob(f"{event.chat_id}_*/job.json"),
                key=lambda p: p.stat().st_mtime,
                reverse=True
            )
            if not candidates:
                await event.respond("ℹ️ No resumable job was found.")
                return

            manifest_path = candidates[0]
            try:
                manifest = load_job_manifest(manifest_path)
                source = Path(manifest["source"])
                if not source.exists():
                    raise FileNotFoundError("The downloaded source file is no longer available.")

                completed = set(manifest.get("completed", []))
                total = len(manifest["plan"])
                status = await event.respond(
                    f"♻️ Resuming job...\n"
                    f"📝 {manifest['base_name']}.mp4\n"
                    f"📈 {len(completed)}/{total} clips already complete"
                )
                await create_clips(
                    event.chat_id,
                    source,
                    manifest["duration"],
                    manifest["clip_count"],
                    status,
                    manifest["base_name"],
                    manifest_path.parent,
                    manifest_path,
                    manifest
                )
            except Exception as e:
                log(f"Resume error: {type(e).__name__}: {e}")
                await event.respond(f"❌ Could not resume job: {type(e).__name__}: {e}")
            return

        if event.chat_id in USER_JOBS and USER_JOBS[event.chat_id].get("awaiting_name"):
            try:
                name = sanitize_filename(text)
                USER_JOBS[event.chat_id]["name"] = name
                USER_JOBS[event.chat_id]["awaiting_name"] = False
                await event.respond(
                    f"✅ File name: {name}.mp4\n\n"
                    "Select your Instagram Reel duration:",
                    buttons=duration_buttons()
                )
            except Exception as e:
                await event.respond(
                    f"❌ {e}\n\n"
                    "Send the file name again (without extension)."
                )
            return

        if not re.match(r"^https?://", text, re.I):
            await event.respond("📎 Send a video URL starting with http:// or https://")
            return

        status = await event.respond("🔎 Inspecting URL...")
        try:
            info = inspect_url(text)
            resolved = resolve_public_page(info)
            log(
                f"URL inspected: kind={info['kind']} "
                f"type={info.get('content_type', 'unknown')}"
            )
            if not resolved:
                if info["kind"] == "page":
                    raise ValueError(
                        "I couldn't find a normal public direct video URL on that page."
                    )
                resolved = text
            USER_JOBS[event.chat_id] = {
                "url": text,
                "resolved": resolved,
                "info": info,
                "awaiting_name": True
            }
            await status.edit(
                "✅ URL inspected.\n\n"
                "📝 Send the file name you want to use (without extension)."
            )
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
        await event.edit(
            f"✅ Reel duration: {duration}s\n\n"
            "Now select the number of clips:",
            buttons=clip_buttons()
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
        log(f"Chat {chat_id} selected {clip_count} clips at {job['duration']}s")

        if not job.get("name"):
            await event.answer("Send the file name first.", alert=True)
            return

        await event.edit(
            f"🚀 Starting...\n\n"
            f"📝 File: {job['name']}.mp4\n"
            f"🎞 Reel duration: {job['duration']}s\n"
            f"🔢 Clips: {clip_count}\n\n"
            "Downloading and processing now..."
        )

        try:
            loop = asyncio.get_running_loop()
            last_download_update = [0.0]

            def download_progress(downloaded, total, speed):
                now = time.time()
                if (
                    now - last_download_update[0] < 4.0
                    and not (total and downloaded >= total)
                ):
                    return
                last_download_update[0] = now

                if total:
                    text = (
                        "⬇️ Downloading source\n"
                        f"📊 {downloaded / total * 100:.0f}% • "
                        f"{downloaded / 1024 / 1024:.1f}/{total / 1024 / 1024:.1f} MB\n"
                        f"⚡ {speed / 1024 / 1024:.2f} MB/s"
                    )
                else:
                    text = (
                        "⬇️ Downloading source\n"
                        f"📦 {downloaded / 1024 / 1024:.1f} MB\n"
                        f"⚡ {speed / 1024 / 1024:.2f} MB/s"
                    )

                asyncio.run_coroutine_threadsafe(event.edit(text), loop)

            source, _ = await retry_async(
                lambda: asyncio.to_thread(
                    download_url, job["url"], download_progress
                ),
                attempts=3,
                label="Source download"
            )

            media = probe(source)
            job_dir = OUTPUTS / f"{chat_id}_{time.strftime('%Y%m%d_%H%M%S')}"
            job_dir.mkdir(parents=True, exist_ok=True)
            manifest_path = job_dir / "job.json"
            manifest = {
                "chat_id": chat_id,
                "source": str(source),
                "url": job["url"],
                "resolved": job.get("resolved"),
                "base_name": job["name"],
                "duration": job["duration"],
                "clip_count": clip_count,
                "source_duration": media["duration"],
                "plan": make_plan(media["duration"], job["duration"], clip_count),
                "completed": []
            }
            save_job_manifest(manifest_path, manifest)

            await create_clips(
                chat_id, source, job["duration"], clip_count,
                event, job["name"], job_dir, manifest_path, manifest
            )
        except Exception as e:
            log(f"Job error: {type(e).__name__}: {e}")
            await event.edit(
                f"❌ Job paused: {type(e).__name__}\n"
                f"{e}\n\n"
                f"♻️ Progress is saved. Send /resume to continue."
            )
        finally:
            USER_JOBS.pop(chat_id, None)

    print("🤖 Colabvid Telegram bot is running...", flush=True)
    print("📟 Terminal logs are enabled below.", flush=True)
    await BOT.run_until_disconnected()
