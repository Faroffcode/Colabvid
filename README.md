# 🎬 Colabvid

Movie/video → Instagram Reels → Telegram automation for Google Colab.

Colabvid provides a Gradio web UI for uploading a video or inspecting a supported public video URL, detecting scenes, creating scene-aware vertical 9:16 clips, optionally generating local Whisper subtitles, and uploading finished clips to a Telegram channel.

## 🚀 Open in Google Colab

[![Open in Colab](https://colab.research.google.com/assets/colab-badge.svg)](https://colab.research.google.com/github/Faroffcode/Colabvid/blob/main/Colabvid.ipynb)

## Features

- 🎥 Upload a video or provide a URL
- 🔍 Inspect URLs before downloading
- 🔗 Direct media URLs without a `.mp4` extension
- ☁️ Pixeldrain public file URL support
- 🌐 Public direct-media URL resolution where available
- 🎞️ Scene detection with PySceneDetect
- ✂️ Scene-aware clip generation
- 📱 Automatic 9:16 vertical rendering
- 🗣️ Local Whisper subtitles — no AI API key required
- 📢 Automatic Telegram channel upload
- 🎨 Gradio web interface

## ⚠️ Access and copyright

Use Colabvid only with videos you have the right to process and repost. The downloader does not bypass DRM, CAPTCHA, authentication, paywalls, or anti-bot protections.

## Files

- `Colabvid.ipynb` — one-click Google Colab launcher
- `colabvid.py` — main application
