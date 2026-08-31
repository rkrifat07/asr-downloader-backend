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
    
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json"
    }
    
    for instance in COBALT_INSTANCES:
        try:
            request = urllib.request.Request(f"{instance}/", data=payload, headers=headers, method="POST")
            with urllib.request.urlopen(request, timeout=8) as response:
                if response.status == 200:
                    data = json.loads(response.read().decode("utf-8"))
                    video_url = data.get("url") or (data.get("picker", [{}])[0].get("url") if data.get("picker") else None)
                    if video_url:
                        return {"status": "success", "url": video_url}
        except Exception:
            continue
            
    raise HTTPException(status_code=400, detail="Failed to extract video stream.")
