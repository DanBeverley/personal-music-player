import os
filepath = r'c:\Users\ADMIN\Documents\GitHub\personal-music-player\ingestion\python_proxy\server.py'
with open(filepath, 'r', encoding='utf-8') as f:
    text = f.read()

text = text.replace(
    'from fastapi import FastAPI, HTTPException',
    'from fastapi import FastAPI, HTTPException, Request\nfrom fastapi.responses import StreamingResponse\nimport requests'
)

new_code = '''
@app.get("/proxy_stream/{video_id}")
def proxy_stream(video_id: str, request: Request):
    ydl_opts = {'format': 'bestaudio/best', 'quiet': True, 'no_warnings': True}
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
            url = info['url']
        headers = {}
        if "range" in request.headers:
            headers["range"] = request.headers["range"]
        req = requests.get(url, headers=headers, stream=True)
        resp_headers = {}
        for k, v in req.headers.items():
            if k.lower() in ['content-type', 'content-length', 'content-range', 'accept-ranges']:
                resp_headers[k] = v
        return StreamingResponse(
            req.iter_content(chunk_size=1024*64), 
            status_code=req.status_code, 
            headers=resp_headers,
            media_type=req.headers.get("content-type", "audio/webm")
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
'''

text = text.replace('@app.get("/direct_url/{video_id}")', new_code + '\n@app.get("/direct_url/{video_id}")')

with open(filepath, 'w', encoding='utf-8') as f:
    f.write(text)

print('Server updated via Python script')
