from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import yt_dlp

app = FastAPI()

class DownloadRequest(BaseModel):
    url: str
    quality: str = "1080"
    audio: bool = False

@app.get("/health")
def health_check():
    return {"status": "ok", "service": "asr-guardian-child-backend"}

@app.post("/extract")
def extract_video(req: DownloadRequest):
    # Dynamic headers to bypass bot detection on FB/Insta/YT without cookies
    ydl_opts = {
        'format': 'best',
        'quiet': True,
        'no_warnings': True,
        'extract_flat': False,
        'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36',
        'http_headers': {
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
            'Sec-Fetch-Mode': 'navigate',
        }
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(req.url, download=False)
            
            # Extract direct streaming URL
            video_url = info.get('url')
            if not video_url and 'formats' in info:
                # Fallback to best available format URL
                video_url = info['formats'][-1].get('url')

            if video_url:
                return {"status": "success", "url": video_url}
            else:
                raise HTTPException(status_code=400, detail="No direct video URL found.")
                
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
