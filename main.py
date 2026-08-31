from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import requests

app = FastAPI()

class DownloadRequest(BaseModel):
    url: str
    quality: str = "1080"
    audio: bool = False

COBALT_INSTANCES = [
    "https://cobalt-api.kwiatekmiki.com",
    "https://api.cobalt.tools",
    "https://co.wuk.sh"
]

@app.get("/health")
def health_check():
    return {"status": "ok", "service": "asr-guardian-child-backend"}

@app.post("/extract")
def extract_video(req: DownloadRequest):
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json"
    }
    payload = {
        "url": req.url,
        "videoQuality": req.quality,
        "downloadMode": "audio" if req.audio else "auto"
    }
    
    for instance in COBALT_INSTANCES:
        try:
            res = requests.post(f"{instance}/", json=payload, headers=headers, timeout=8)
            if res.status_code == 200:
                data = res.json()
                video_url = data.get("url") or data.get("picker", [{}])[0].get("url")
                if video_url:
                    return {"status": "success", "url": video_url}
        except Exception:
            continue
            
    raise HTTPException(status_code=400, detail="Failed to extract video stream.")
