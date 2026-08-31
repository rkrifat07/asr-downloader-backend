from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import yt_dlp
import json
import urllib.request

app = FastAPI()

class DownloadRequest(BaseModel):
    url: str
    quality: str = "1080"
    audio: bool = False

INVIDIOUS_INSTANCES = [
    "https://inv.tux.stream",
    "https://invidious.nerdvpn.de",
    "https://yt.artemislena.eu"
]

@app.get("/health")
def health_check():
    return {"status": "ok", "service": "asr-guardian-child-backend"}

def get_youtube_fallback(url: str):
    video_id = url.split("v=")[-1].split("&")[0].split("?")[0].split("/")[-1]
    headers = {"User-Agent": "Mozilla/5.0"}
    
    for instance in INVIDIOUS_INSTANCES:
        try:
            req = urllib.request.Request(f"{instance}/api/v1/videos/{video_id}", headers=headers)
            with urllib.request.urlopen(req, timeout=5) as response:
                if response.status == 200:
                    data = json.loads(response.read().decode("utf-8"))
                    format_streams = data.get("formatStreams", [])
                    if format_streams:
                        return format_streams[-1].get("url")
        except Exception:
            continue
    return None

@app.post("/extract")
def extract_video(req: DownloadRequest):
    ydl_opts = {
        'format': 'best[ext=mp4]/best',
        'quiet': True,
        'no_warnings': True,
        'extract_flat': False,
        'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(req.url, download=False)
            video_url = info.get('url')
            
            if not video_url and 'formats' in info:
                for fmt in reversed(info['formats']):
                    if fmt.get('url') and fmt.get('ext') == 'mp4':
                        video_url = fmt.get('url')
                        break

            if video_url:
                return {"status": "success", "url": video_url}
                
    except Exception:
        if "youtube.com" in req.url or "youtu.be" in req.url:
            fallback_url = get_youtube_fallback(req.url)
            if fallback_url:
                return {"status": "success", "url": fallback_url}

    raise HTTPException(status_code=400, detail="Failed to extract stream.")
