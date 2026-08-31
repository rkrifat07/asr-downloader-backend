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
    # Enforce pure direct MP4 format that Android native gallery players can play
    format_option = 'best[ext=mp4]/best' if not req.audio else 'bestaudio[ext=m4a]/bestaudio/best'

    ydl_opts = {
        'format': format_option,
        'quiet': True,
        'no_warnings': True,
        'extract_flat': False,
        'user_agent': 'Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Mobile Safari/537.36',
        'http_headers': {
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
        },
        'extractor_args': {
            'youtube': {
                'player_client': ['android', 'ios'],
                'skip': ['hls', 'dash']
            },
            'instagram': {
                'claim_manifest': False
            }
        }
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(req.url, download=False)
            
            # Retrieve direct MP4 play-able video URL
            video_url = info.get('url')
            
            if not video_url and 'formats' in info:
                # Find the best format with a valid direct URL
                for fmt in reversed(info['formats']):
                    if fmt.get('url') and fmt.get('ext') == 'mp4':
                        video_url = fmt.get('url')
                        break
                if not video_url:
                    video_url = info['formats'][-1].get('url')

            if video_url:
                return {"status": "success", "url": video_url}
            else:
                raise HTTPException(status_code=400, detail="No direct playable URL found.")
                
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
