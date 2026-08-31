from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import json
import urllib.request

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
    payload = json.dumps({
        "url": req.url,
        "videoQuality": req.quality,
        "downloadMode": "audio" if req.audio else "auto"
    }).encode("utf-8")
    
    # Real Browser User-Agent added to bypass Cobalt API blocks
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    
    for instance in COBALT_INSTANCES:
        try:
            request = urllib.request.Request(f"{instance}/", data=payload, headers=headers, method="POST")
            with urllib.request.urlopen(request, timeout=10) as response:
                if response.status in [200, 201]:
                    data = json.loads(response.read().decode("utf-8"))
                    
                    # Handles direct URL, Picker, or Stream responses
                    video_url = data.get("url")
                    if not video_url and data.get("picker"):
                        video_url = data.get("picker")[0].get("url")
                    
                    if video_url:
                        return {"status": "success", "url": video_url}
        except Exception:
            continue
            
    raise HTTPException(status_code=400, detail="Failed to extract video stream.")
