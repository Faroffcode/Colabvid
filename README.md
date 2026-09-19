# 🎬 Colabvid

Movie/video → Instagram Reels → Telegram automation for Google Colab.

Colabvid is a Telegram-controlled video processing bot. Send it a supported public video URL, choose a custom filename, reel duration, and number of clips, then Colabvid downloads the source, creates fixed-duration 9:16 clips, and uploads them directly to your Telegram channel.

## 🚀 Open in Google Colab

[![Open in Colab](https://colab.research.google.com/assets/colab-badge.svg)](https://colab.research.google.com/github/Faroffcode/Colabvid/blob/main/Colabvid.ipynb)

## ✨ Features

- 🤖 Telegram bot interface — no Gradio UI required
- 🔗 Inspect URLs before downloading
- 🎥 Public direct-media URLs, including URLs without a `.mp4` extension
- ☁️ Pixeldrain public file URL support
- 🌐 Public pages that expose normal direct video URLs
- 📱 Fixed 9:16 vertical output at 1080×1920
- 🖼️ Full-frame preservation with dark/black padding instead of hard cropping
- ⚡ Automatic NVIDIA NVENC detection with CPU fallback
- 🔁 Automatic retry for downloads, rendering, and Telegram uploads
- ♻️ Resume interrupted jobs with `/resume`
- 💾 Persistent per-job progress using `job.json`
- ⏭️ Already-completed clips are skipped when resuming
- 📊 Telegram progress notifications updated every 2 seconds
- 🧹 Automatic cleanup after all clips upload successfully
- 📝 Custom output filename and numbered parts
- 📢 Direct upload to a Telegram channel
- 🎞️ No scene detection, subtitles, or AI API key required

## 🔄 How it works

1. Start the bot in Google Colab.
2. Send a supported public video URL.
3. Colabvid inspects the URL before downloading.
4. Enter the desired filename without an extension.
5. Choose the reel duration.
6. Choose the number of clips.
7. Colabvid downloads and probes the source video.
8. Clips are rendered as 1080×1920 vertical videos while preserving the full frame.
9. Each finished clip is uploaded directly to the configured Telegram channel.
10. Progress is saved after every successful upload.
11. Temporary files are automatically removed after the complete job succeeds.

If a job is interrupted, start the bot again and send `/resume` to continue from the saved progress.

## 🎛️ Telegram controls

### Commands

- `/start` — show bot instructions
- `/help` — show bot instructions
- `/resume` — resume the latest unfinished job for your chat

### Reel duration

Available durations:

`15s` · `30s` · `45s` · `60s` · `90s` · `120s`

### Clip count

Available counts:

`1` · `5` · `10` · `15` · `20` · `30` · `50` · `100`

## 🔁 Reliability

Colabvid uses retry protection for the three main stages:

- **Download:** up to 3 attempts
- **Rendering:** up to 3 attempts per clip
- **Telegram upload:** up to 3 attempts per clip

A job manifest records which clips have already been uploaded. If processing stops after several successful uploads, `/resume` reuses the saved manifest and skips completed clips.

## ⚡ Encoding

Colabvid checks for an available NVIDIA GPU/NVENC encoder when the bot starts.

- NVIDIA NVENC available → `h264_nvenc`
- NVENC unavailable → CPU `libx264`

The encoder is detected once per Colab session.

## 📊 Progress

Telegram status messages are refreshed approximately every **2 seconds** during download and encoding.

Progress can include:

- Download percentage and transferred size
- Download speed
- Current clip number
- Encoding percentage
- Encoding elapsed/target time
- Overall uploaded clip count
- Current clip time range
- Upload status

## 🧹 Cleanup

After every planned clip has uploaded successfully:

- The downloaded source video is removed.
- Generated temporary clip files are removed.
- The job manifest/output directory is removed.

If a job fails, temporary files and the manifest are intentionally kept so the job can be resumed.

## ⚙️ Setup

The Colab notebook asks for these values securely at runtime:

- `COLABVID_API_ID`
- `COLABVID_API_HASH`
- `COLABVID_BOT_TOKEN`
- `COLABVID_CHANNEL_ID`

The channel ID should normally be the numeric Telegram channel ID, typically beginning with `-100`.

The bot must have permission to post videos in the destination channel.

## 📁 Files

- `Colabvid.ipynb` — Google Colab launcher
- `colabvid.py` — main Telegram bot and video-processing application
- `README.md` — project documentation

## ⚠️ Access and copyright

Use Colabvid only with videos you have the right to download, process, and repost.

The downloader supports public media sources and does **not** bypass DRM, CAPTCHA, authentication, paywalls, or anti-bot protections.

## 📄 License

See the repository for licensing information.
