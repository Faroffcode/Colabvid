# Colabvid — Google Colab movie/video -> Instagram Reels -> Telegram
# No AI API key required.
# Uses FFmpeg, PySceneDetect and local Whisper.
#
# Supports:
# - Upload or URL input
# - Mandatory URL inspection
# - Direct media URLs without .mp4 extension
# - Pixeldrain public file URLs
# - Public HubCloud pages when they expose an ordinary direct media URL
# - ffprobe verification after download
# - Scene-aware clip planning
# - 9:16 rendering
# - Local Whisper subtitles
# - Telegram Bot API upload
#
# Does NOT bypass DRM, CAPTCHA, login, paywalls or anti-bot controls.

import os, re, sys, json, time, shutil, subprocess, html
from pathlib import Path
from urllib.parse import urlparse, unquote
import requests

ROOT = Path("/content/colabvid")
DOWNLOADS = ROOT / "downloads"
OUTPUTS = ROOT / "outputs"
for p in (DOWNLOADS, OUTPUTS):
    p.mkdir(parents=True, exist_ok=True)

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/128 Safari/537.36"

def cmd(args, check=True, capture=False):
    return subprocess.run([str(x) for x in args], check=check,
                          text=True, capture_output=capture)

def install():
    if shutil.which("ffmpeg") is None:
        cmd(["apt-get","update","-qq"])
        cmd(["apt-get","install","-y","-qq","ffmpeg"])
    cmd([sys.executable,"-m","pip","install","-q",
         "gradio>=6,<7","requests","pyscenedetect[opencv]","openai-whisper","telethon"])

def inspect_url(url):
    url = url.strip()
    if not re.match(r"^https?://", url, re.I):
        raise ValueError("Only HTTP/HTTPS URLs are supported.")
    host = (urlparse(url).hostname or "").lower()
    if "pixeldrain" in host:
        m = re.search(r"/(?:u|l)/([A-Za-z0-9_-]+)", urlparse(url).path)
        if not m: raise ValueError("Could not extract Pixeldrain file ID.")
        fid = m.group(1)
        r = requests.get(f"https://pixeldrain.com/api/file/{fid}/info",
                         headers={"User-Agent":UA}, timeout=30)
        if not r.ok: raise ValueError(f"Pixeldrain inspection failed: HTTP {r.status_code}")
        info = r.json()
        return {"provider":"pixeldrain","kind":"video_or_file",
                "name":info.get("name"),"mime":info.get("mime_type"),
                "size":info.get("size"),
                "download_url":f"https://pixeldrain.com/api/file/{fid}"}

    r = requests.head(url, headers={"User-Agent":UA}, allow_redirects=True, timeout=20)
    ct = (r.headers.get("content-type") or "").split(";")[0].lower()
    result = {"provider":"hubcloud_page" if ("hubcloud" in host or "gpdl" in host) else "direct",
              "source_url":url,"final_url":r.url,"status":r.status_code,
              "content_type":ct}
    if ct.startswith("video/") or ct == "application/octet-stream":
        result["kind"] = "media"
    elif "text/html" in ct:
        result["kind"] = "html"
    else:
        result["kind"] = "unknown"
    return result

def public_page_resolve(info):
    if "download_url" in info: return info["download_url"]
    if info.get("kind") == "media": return info["final_url"]
    if info.get("kind") != "html": return None
    r = requests.get(info["final_url"], headers={"User-Agent":UA}, timeout=30)
    urls = re.findall(r'https?://[^"\'<>\s]+', r.text, re.I)
    for u in urls:
        u = html.unescape(u)
        if any(x in u.lower().split("?")[0] for x in (".mp4",".webm",".mkv",".mov",".m4v")):
            return u
    return None

def ffprobe(path):
    r = cmd(["ffprobe","-v","error","-show_format","-show_streams","-of","json",str(path)],
            capture=True)
    data = json.loads(r.stdout)
    video = next((s for s in data.get("streams",[]) if s.get("codec_type")=="video"),None)
    if not video: raise ValueError("Downloaded file has no video stream.")
    return {"duration":float(data.get("format",{}).get("duration") or video.get("duration") or 0),
            "width":video.get("width"),"height":video.get("height"),
            "codec":video.get("codec_name"),"format":data.get("format",{}).get("format_name")}

def download(url, progress=None):
    info = inspect_url(url)
    print("URL INSPECTION:", json.dumps(info, indent=2))
    resolved = public_page_resolve(info)
    if not resolved:
        if info.get("kind") == "html":
            raise ValueError("This URL is a web page and did not expose a normal public direct media URL. CAPTCHA/auth/anti-bot bypass is not supported.")
        resolved = url
    name = Path(unquote(urlparse(resolved).path)).name or "source_video"
    name = re.sub(r"[^A-Za-z0-9_.-]+","_",name)[:160]
    dest = DOWNLOADS / f"{int(time.time())}_{name}"
    with requests.get(resolved, headers={"User-Agent":UA}, stream=True, timeout=(30,120)) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length") or 0); done = 0
        with open(dest,"wb") as f:
            for chunk in r.iter_content(1024*1024):
                if not chunk: continue
                f.write(chunk); done += len(chunk)
                if progress and total: progress(done/total, f"Downloading {done/1048576:.1f}/{total/1048576:.1f} MB")
    media = ffprobe(dest)
    print("MEDIA INSPECTION:", json.dumps(media, indent=2))
    return dest, media

def scenes(path, threshold=27):
    from scenedetect import open_video, SceneManager
    from scenedetect.detectors import ContentDetector
    v = open_video(str(path)); sm = SceneManager()
    sm.add_detector(ContentDetector(threshold=threshold, min_scene_len=12))
    sm.detect_scenes(video=v)
    return [(a.get_seconds(),b.get_seconds()) for a,b in sm.get_scene_list()]

def plan_clips(sc, duration, target=60, minimum=25, maximum=90):
    if not sc:
        return [(x,min(x+target,duration)) for x in range(0,int(duration),target)]
    out=[]; start=sc[0][0]; end=start
    for a,b in sc:
        if b-start <= maximum:
            end=b
            if end-start >= target:
                out.append((start,end)); start=b; end=b
        elif end-start >= minimum:
            out.append((start,end)); start=a; end=b
        else:
            e=min(start+maximum,duration); out.append((start,e)); start=e; end=e
    if start < duration and end > start:
        if end-start >= minimum or not out: out.append((start,min(end,duration)))
    return out

def render(src, start, end, out, subtitle=None):
    vf="scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920"
    if subtitle:
        p=str(Path(subtitle).resolve()).replace(":","\\:")
        vf += ",subtitles='"+p+"':force_style='FontName=DejaVu Sans,FontSize=18,Outline=2,Shadow=1,Alignment=2,MarginV=90'"
    cmd(["ffmpeg","-y","-ss",start,"-i",src,"-t",end-start,"-vf",vf,
         "-c:v","libx264","-preset","veryfast","-crf","23",
         "-pix_fmt","yuv420p","-c:a","aac","-b:a","128k","-movflags","+faststart",out])

WHISPER = None
def subtitles(video, srt, model="small"):
    global WHISPER
    import whisper
    if WHISPER is None: WHISPER=whisper.load_model(model)
    result=WHISPER.transcribe(str(video), fp16=False)
    def ts(x):
        ms=int(round((x-int(x))*1000)); n=int(x); s=n%60; m=n//60%60; h=n//3600
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"
    with open(srt,"w",encoding="utf-8") as f:
        for i,z in enumerate(result.get("segments",[]),1):
            t=(z.get("text") or "").strip()
            if t: f.write(f"{i}\n{ts(z['start'])} --> {ts(z['end'])}\n{t}\n\n")

def telegram_bot(token, channel, file, caption):
    if Path(file).stat().st_size > 50*1024*1024:
        raise ValueError("Telegram Bot API sendVideo limit is 50 MB for this upload.")
    with open(file,"rb") as f:
        r=requests.post(f"https://api.telegram.org/bot{token}/sendVideo",
                        data={"chat_id":channel,"caption":caption[:1024],"supports_streaming":"true"},
                        files={"video":(Path(file).name,f,"video/mp4")},timeout=600)
    if not r.ok: raise RuntimeError(f"Telegram upload failed: {r.text}")
    return r.json()

def process(source_mode, upload, url, target, minimum, maximum, threshold,
            use_subtitles, whisper_model, telegram, bot_token, channel_id, caption, progress=None):
    def p(v,d):
        print(d)
        if progress: progress(v,desc=d)
    if source_mode=="Upload":
        if not upload: raise ValueError("Upload a video first.")
        src=Path(upload); media=ffprobe(src)
    else:
        src,media=download(url, lambda v,d:p(.1*v,d))
    p(.22,"Detecting scenes...")
    sc=scenes(src,threshold)
    clips=plan_clips(sc,media["duration"],int(target),int(minimum),int(maximum))
    job=OUTPUTS/time.strftime("%Y%m%d_%H%M%S"); job.mkdir(parents=True)
    result=[]
    for i,(a,b) in enumerate(clips,1):
        p(.25+.65*(i-1)/len(clips),f"Rendering Part {i:02d}/{len(clips)}...")
        raw=job/f"Part_{i:02d}.source.mp4"; out=job/f"Part_{i:02d}.mp4"
        render(src,a,b,raw)
        if use_subtitles:
            srt=job/f"Part_{i:02d}.srt"; subtitles(raw,srt,whisper_model)
            render(raw,0,b-a,out,srt); raw.unlink(missing_ok=True); srt.unlink(missing_ok=True)
        else: raw.replace(out)
        result.append(str(out))
        if telegram:
            cap=caption.replace("{part}",f"{i:02d}").replace("{total}",str(len(clips)))
            p(.25+.65*i/len(clips),f"Uploading Part {i:02d}...")
            telegram_bot(bot_token,channel_id,out,cap)
    p(1,"Finished.")
    return result, f"Done — {len(result)} clips created. Output: {job}"

def ui():
    import gradio as gr
    with gr.Blocks(title="Colabvid",theme=gr.themes.Soft()) as app:
        gr.Markdown("# 🎬 Colabvid\n### Movie → Instagram Reels → Telegram\nNo AI API key required.")
        source=gr.Radio(["Upload","URL"],value="Upload",label="Video source")
        up=gr.File(label="Video",file_types=["video"],type="filepath")
        url=gr.Textbox(label="Video URL",placeholder="R2/CDN/Pixeldrain/direct URL",visible=False)
        source.change(lambda x:(gr.update(visible=x=="Upload"),gr.update(visible=x=="URL")),source,[up,url])
        with gr.Row():
            target=gr.Slider(30,90,60,5,label="Target clip seconds")
            minimum=gr.Slider(10,60,25,5,label="Minimum clip seconds")
            maximum=gr.Slider(45,120,90,5,label="Maximum clip seconds")
            threshold=gr.Slider(10,60,27,1,label="Scene sensitivity")
        with gr.Row():
            subs=gr.Checkbox(True,label="Local Whisper subtitles")
            model=gr.Dropdown(["tiny","base","small","medium"],value="small",label="Whisper model")
        tg=gr.Checkbox(True,label="Upload to Telegram")
        with gr.Row():
            token=gr.Textbox(label="BOT_TOKEN",type="password")
            channel=gr.Textbox(label="CHANNEL_ID")
        caption=gr.Textbox("🎬 Part {part}/{total}",label="Telegram caption")
        go=gr.Button("🚀 CREATE REELS",variant="primary")
        files=gr.File(label="Generated Reels",file_count="multiple")
        status=gr.Markdown("Ready.")
        go.click(process,[source,up,url,target,minimum,maximum,threshold,subs,model,tg,token,channel,caption],[files,status],show_progress="full")
    return app

if __name__=="__main__":
    install()
    ui().queue().launch(share=True,show_error=True,max_file_size="20gb")
