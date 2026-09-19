import os, re, json, time, subprocess, html
from pathlib import Path
from urllib.parse import urlparse, unquote
import requests
import gradio as gr

ROOT = Path("/content/colabvid")
DOWNLOADS = ROOT / "downloads"
OUTPUTS = ROOT / "outputs"
DOWNLOADS.mkdir(parents=True, exist_ok=True)
OUTPUTS.mkdir(parents=True, exist_ok=True)

UA = "Mozilla/5.0 Colabvid/1.0"
TELEGRAM_CLIENT = None
WHISPER_MODEL = None


def run(args, capture=False):
    return subprocess.run(
        [str(x) for x in args],
        check=True,
        text=True,
        capture_output=capture,
    )


def inspect_url(url):
    """Mandatory inspection before every URL download."""
    url = url.strip()
    if not re.match(r"^https?://", url, re.I):
        raise ValueError("Only HTTP/HTTPS URLs are supported.")

    p = urlparse(url)
    host = (p.hostname or "").lower()

    # Pixeldrain public file/page.
    if "pixeldrain" in host:
        m = re.search(r"/(?:u|l)/([A-Za-z0-9_-]+)", p.path)
        if m:
            fid = m.group(1)
            r = requests.get(
                f"https://pixeldrain.com/api/file/{fid}/info",
                headers={"User-Agent": UA},
                timeout=30,
            )
            r.raise_for_status()
            info = r.json()
            return {
                "kind": "pixeldrain",
                "final_url": f"https://pixeldrain.com/api/file/{fid}",
                "content_type": info.get("mime_type", ""),
                "name": info.get("name", ""),
                "size": info.get("size"),
            }

    r = requests.head(
        url, headers={"User-Agent": UA}, allow_redirects=True, timeout=30
    )
    ct = (r.headers.get("content-type") or "").split(";")[0].lower()

    if ct.startswith("video/") or ct == "application/octet-stream":
        kind = "media"
    elif "text/html" in ct:
        kind = "page"
    else:
        kind = "unknown"

    return {
        "kind": kind,
        "source_url": url,
        "final_url": r.url,
        "status": r.status_code,
        "content_type": ct,
    }


def resolve_public_page(info):
    if info["kind"] == "pixeldrain":
        return info["final_url"]

    if info["kind"] == "media":
        return info["final_url"]

    if info["kind"] != "page":
        return None

    r = requests.get(
        info["final_url"], headers={"User-Agent": UA}, timeout=30
    )
    r.raise_for_status()

    # Only ordinary public direct-media links. No DRM/auth/CAPTCHA bypass.
    candidates = re.findall(r'https?://[^"\'<>\s]+', r.text, re.I)
    for value in candidates:
        value = html.unescape(value)
        clean = value.split("?")[0].lower()
        if clean.endswith((".mp4", ".webm", ".mkv", ".mov", ".m4v")):
            return value

    return None


def probe(path):
    result = run(
        [
            "ffprobe", "-v", "error",
            "-show_format", "-show_streams",
            "-of", "json", str(path),
        ],
        capture=True,
    )
    data = json.loads(result.stdout)
    video = next(
        (s for s in data.get("streams", []) if s.get("codec_type") == "video"),
        None,
    )
    if not video:
        raise ValueError("The file does not contain a video stream.")

    duration = float(
        data.get("format", {}).get("duration")
        or video.get("duration")
        or 0
    )
    if duration <= 0:
        raise ValueError("Could not determine video duration.")

    return {
        "duration": duration,
        "width": video.get("width"),
        "height": video.get("height"),
        "codec": video.get("codec_name"),
    }


def download_url(url, progress):
    info = inspect_url(url)
    progress(0.05, "URL inspected")

    resolved = resolve_public_page(info)
    if not resolved:
        if info["kind"] == "page":
            raise ValueError(
                "The page did not expose a normal public direct video URL. "
                "Login, CAPTCHA, anti-bot and DRM bypass are not supported."
            )
        resolved = url

    name = Path(unquote(urlparse(resolved).path)).name or "source_video"
    name = re.sub(r"[^A-Za-z0-9_.-]+", "_", name)[:120]
    destination = DOWNLOADS / f"{int(time.time())}_{name}"

    with requests.get(
        resolved,
        headers={"User-Agent": UA},
        stream=True,
        timeout=(30, 120),
    ) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length") or 0)
        done = 0

        with open(destination, "wb") as f:
            for chunk in r.iter_content(1024 * 1024):
                if not chunk:
                    continue
                f.write(chunk)
                done += len(chunk)
                if total:
                    progress(
                        0.05 + 0.25 * done / total,
                        f"Downloading {done / 1048576:.1f} / {total / 1048576:.1f} MB",
                    )

    media = probe(destination)
    progress(0.30, "Video verified")
    return destination, media


def detect_scenes(path):
    try:
        from scenedetect import open_video, SceneManager
        from scenedetect.detectors import ContentDetector

        video = open_video(str(path))
        manager = SceneManager()
        manager.add_detector(ContentDetector(threshold=27, min_scene_len=12))
        manager.detect_scenes(video=video)
        scenes = manager.get_scene_list()

        if scenes:
            return [(a.get_seconds(), b.get_seconds()) for a, b in scenes]
    except Exception:
        pass

    duration = probe(path)["duration"]
    return [
        (x, min(x + 60, duration))
        for x in range(0, int(duration), 60)
    ]


def make_plan(scenes, duration, target, minimum, maximum):
    if not scenes:
        return [(x, min(x + target, duration))
                for x in range(0, int(duration), target)]

    clips = []
    start = scenes[0][0]
    end = start

    for a, b in scenes:
        if b - start <= maximum:
            end = b
            if end - start >= target:
                clips.append((start, end))
                start = b
                end = b
        else:
            if end - start >= minimum:
                clips.append((start, end))
                start = a
                end = b
            else:
                stop = min(start + maximum, duration)
                clips.append((start, stop))
                start = stop
                end = stop

    if start < duration and end > start:
        if end - start >= minimum or not clips:
            clips.append((start, min(end, duration)))

    return clips


def create_srt(video, output, model_name):
    global WHISPER_MODEL
    import whisper

    if WHISPER_MODEL is None:
        WHISPER_MODEL = whisper.load_model(model_name)

    result = WHISPER_MODEL.transcribe(str(video), fp16=False)

    def stamp(seconds):
        ms = int(round((seconds - int(seconds)) * 1000))
        total = int(seconds)
        h, rem = divmod(total, 3600)
        m, s = divmod(rem, 60)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    with open(output, "w", encoding="utf-8") as f:
        for i, seg in enumerate(result.get("segments", []), 1):
            text = (seg.get("text") or "").strip()
            if text:
                f.write(
                    f"{i}\n{stamp(seg['start'])} --> {stamp(seg['end'])}\n"
                    f"{text}\n\n"
                )


def render_clip(source, start, end, output, srt=None):
    vf = "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920"

    if srt:
        subtitle_path = str(Path(srt).resolve()).replace(":", "\\:")
        vf += (
            ",subtitles='" + subtitle_path +
            "':force_style='FontName=DejaVu Sans,FontSize=18,"
            "Outline=2,Shadow=1,Alignment=2,MarginV=90'"
        )

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


def process(
    source_mode, upload, url,
    target, minimum, maximum,
    subtitles, whisper_model,
    send_telegram,
    api_id, api_hash, bot_token, channel_id,
    caption_template,
    progress=gr.Progress(),
):
    try:
        if source_mode == "Upload":
            if not upload:
                raise ValueError("Upload a video first.")
            source = Path(upload)
            media = probe(source)
        else:
            if not url.strip():
                raise ValueError("Enter a video URL.")
            source, media = download_url(url, progress)

        duration = media["duration"]
        progress(0.35, "Detecting scenes...")
        scene_list = detect_scenes(source)
        plan = make_plan(
            scene_list, duration,
            float(target), float(minimum), float(maximum)
        )

        if not plan:
            raise ValueError("No clips could be created.")

        if subtitles:
            try:
                import whisper  # noqa: F401
            except ImportError:
                raise ValueError(
                    "Whisper is not installed. Re-run the dependency cell."
                )

        timestamp = time.strftime("%Y%m%d_%H%M%S")
        job_dir = OUTPUTS / timestamp
        job_dir.mkdir(parents=True, exist_ok=True)

        files = []
        total = len(plan)

        for index, (start, end) in enumerate(plan, 1):
            output = job_dir / f"Part_{index:02d}.mp4"
            srt = None

            if subtitles:
                srt = job_dir / f"Part_{index:02d}.srt"
                create_srt(source, srt, whisper_model)

            progress(
                0.35 + 0.55 * (index - 1) / total,
                f"Rendering Part {index}/{total}...",
            )
            render_clip(source, start, end, output, srt)
            files.append(str(output))

            if send_telegram:
                caption = caption_template.replace(
                    "{part}", f"{index:02d}"
                )
                telegram_upload(
                    api_id, api_hash, bot_token, channel_id,
                    output, caption
                )

        progress(1.0, "Finished")
        return files, f"Done — {len(files)} clips created."

    except Exception as e:
        return [], f"❌ {type(e).__name__}: {e}"


def ui():
    with gr.Blocks(title="Colabvid") as app:
        gr.Markdown(
            "# 🎬 Colabvid
"
            "### Full movie → Instagram Reels → Telegram"
        )

        with gr.Row():
            with gr.Column():
                source_mode = gr.Radio(
                    ["Upload", "URL"],
                    value="Upload",
                    label="Source",
                )
                upload = gr.File(
                    label="Video file",
                    file_types=["video"],
                    type="filepath",
                )
                url = gr.Textbox(
                    label="Video URL",
                    placeholder="https://...",
                    visible=False,
                )

                target = gr.Slider(
                    30, 90, value=60, step=1,
                    label="Target clip duration (seconds)",
                )
                minimum = gr.Slider(
                    15, 60, value=25, step=1,
                    label="Minimum clip duration",
                )
                maximum = gr.Slider(
                    60, 120, value=90, step=1,
                    label="Maximum clip duration",
                )

            with gr.Column():
                subtitles = gr.Checkbox(
                    False,
                    label="Add local Whisper subtitles",
                )
                whisper_model = gr.Dropdown(
                    ["tiny", "base", "small", "medium"],
                    value="small",
                    label="Whisper model",
                )

                send_telegram = gr.Checkbox(
                    False,
                    label="Upload finished clips to Telegram",
                )
                api_id = gr.Textbox(
                    label="Telegram API_ID",
                    type="password",
                )
                api_hash = gr.Textbox(
                    label="Telegram API_HASH",
                    type="password",
                )
                bot_token = gr.Textbox(
                    label="Telegram BOT_TOKEN",
                    type="password",
                )
                channel_id = gr.Textbox(
                    label="Telegram CHANNEL_ID",
                )
                caption = gr.Textbox(
                    value="Part {part}",
                    label="Telegram caption",
                )

        create = gr.Button(
            "🚀 CREATE REELS",
            variant="primary",
            size="lg",
        )
        files = gr.File(
            label="Generated clips",
            file_count="multiple",
        )
        status = gr.Textbox(
            label="Status",
            interactive=False,
        )

        def toggle_source(mode):
            return (
                gr.update(visible=mode == "Upload"),
                gr.update(visible=mode == "URL"),
            )

        source_mode.change(
            toggle_source,
            source_mode,
            [upload, url],
        )

        create.click(
            process,
            inputs=[
                source_mode, upload, url,
                target, minimum, maximum,
                subtitles, whisper_model,
                send_telegram,
                api_id, api_hash, bot_token, channel_id,
                caption,
            ],
            outputs=[files, status],
        )

    return app
