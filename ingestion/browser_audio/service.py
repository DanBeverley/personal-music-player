"""Private, bounded browser compatibility transport; no public ports."""
import asyncio
import base64
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
import http.cookiejar
import os
from pathlib import Path
import re
import tempfile
import time
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from playwright.async_api import async_playwright

CAPTURE = r"""(() => {
 const add=MediaSource.prototype.addSourceBuffer, make=URL.createObjectURL.bind(URL);
 const groups=new WeakMap(); let next=0;
 const group=s=>{if(!groups.has(s))groups.set(s,++next);return groups.get(s)};
 window.__audioGroups={}; window.__audioWrites=[];
 URL.createObjectURL=s=>{const u=make(s);if(s instanceof MediaSource)window.__audioGroups[u]=group(s);return u};
 MediaSource.prototype.addSourceBuffer=function(mime){
  const b=add.call(this,mime);if(!mime.startsWith('audio/'))return b;
  const id=group(this), append=b.appendBuffer.bind(b);let seq=0;
  b.appendBuffer=data=>{
   const a=new Uint8Array(data.buffer||data,data.byteOffset||0,data.byteLength);let s='';
   for(let i=0;i<a.length;i+=8192)s+=String.fromCharCode(...a.subarray(i,i+8192));
   window.__audioWrites.push(window.audioChunk({group:id,mime,seq:seq++,data:btoa(s)}));
   return append(data);
  };return b;
 };
})();"""
STATE = """() => {
 const v=document.querySelector('video'),p=document.querySelector('#movie_player');
 if(!v)return {}; v.muted=true;v.playbackRate=8;
 return {group:window.__audioGroups[v.currentSrc],id:p?.getVideoData?.()?.video_id,
   ad:p?.classList.contains('ad-showing'),duration:v.duration,ended:v.ended,
   expected:Number(window.ytInitialPlayerResponse?.videoDetails?.lengthSeconds),
   codecs:p?.getStatsForNerds?.()?.codecs,
   formats:window.ytInitialPlayerResponse?.streamingData?.adaptiveFormats?.filter(f=>f.mimeType.startsWith('audio/')).map(f=>({itag:f.itag,mime:f.mimeType,length:f.contentLength,index:f.indexRange}))};
}"""

@dataclass
class Job:
    video: str
    directory: Path
    created: float = field(default_factory=time.monotonic)
    changed: asyncio.Condition = field(default_factory=asyncio.Condition)
    groups: dict = field(default_factory=dict)
    selected: int | None = None
    duration: float = 0
    done: bool = False
    error: bool = False
    readers: int = 0
    task: asyncio.Task | None = None
    foreground: bool = True
    running: bool = False
    started: float = 0
    priority: int = 0

    @property
    def audio(self):
        return self.groups.get(self.selected)

    async def signal(self):
        async with self.changed:
            self.changed.notify_all()

    async def wait(self, predicate, timeout=22):
        async with self.changed:
            await asyncio.wait_for(self.changed.wait_for(lambda: predicate() or self.error), timeout)
        if self.error:
            raise HTTPException(502, 'Browser source unavailable')

jobs = {}
slots = asyncio.Condition()
browser = None
root = None


async def wake_slots():
    async with slots:
        slots.notify_all()


@asynccontextmanager
async def capture_slot(job):
    async with slots:
        def rank(j):
            return j.priority if j.foreground else 2
        await slots.wait_for(lambda: sum(j.running for j in jobs.values()) < 2 and
            not any(not j.running and not j.done and not j.error and rank(j) < rank(job)
                    for j in jobs.values() if j is not job) and
            (rank(job) == 0 or not any(j.running and rank(j) > 0 for j in jobs.values())))
        job.running = True
        job.started = time.monotonic()
    try:
        yield
    finally:
        job.running = False
        await wake_slots()

def captured_length(state, mime):
    # MSE receives the WebM initialization and clusters, excluding the source's
    # separate Cues/index range. Match the player's actual audio format first.
    match = re.search(r'/\s*opus\s*\((\d+)\)', state.get('codecs') or '')
    if mime != 'audio/webm' or not match:
        return None
    for fmt in state.get('formats') or []:
        if str(fmt.get('itag')) != match[1]:
            continue
        try:
            index = fmt['index']
            total = int(fmt['length']) - (int(index['end'])-int(index['start'])+1)
            return total if 0 < total <= 32*1024*1024 else None
        except (KeyError, TypeError, ValueError):
            return None
    return None

async def capture(job):
    context = None
    try:
        async with capture_slot(job):
            async with asyncio.timeout(200):
                context = await browser.new_context(locale='en-US')
                jar = http.cookiejar.MozillaCookieJar(os.environ['BROWSER_AUDIO_COOKIES'])
                jar.load(ignore_discard=True, ignore_expires=False)
                await context.add_cookies([dict(name=c.name, value=c.value, domain=c.domain,
                    path=c.path, secure=c.secure, **({'expires': c.expires} if c.expires else {})) for c in jar])
                async def receive(source, item):
                    if source['frame'] != source['page'].main_frame:
                        return
                    group = job.groups.setdefault(item['group'], {
                        'path': job.directory / str(item['group']), 'size': 0,
                        'mime': item['mime'].split(';')[0], 'sequence': 0})
                    if item['seq'] != group['sequence']:
                        raise RuntimeError('Out of order capture')
                    blob = base64.b64decode(item['data'], validate=True)
                    if sum(g['size'] for g in job.groups.values()) + len(blob) > 32*1024*1024:
                        raise RuntimeError('Capture limit')
                    with group['path'].open('ab') as f:
                        f.write(blob)
                    group['size'] += len(blob)
                    group['sequence'] += 1
                    await job.signal()
                await context.expose_binding('audioChunk', receive)
                await context.add_init_script(CAPTURE)
                page = await context.new_page()
                await page.goto(f'https://www.youtube.com/watch?v={job.video}',
                                wait_until='domcontentloaded', timeout=40000)
                await page.wait_for_selector('video', timeout=15000)
                for label in ('Reject all', 'Accept all'):
                    button = page.get_by_role('button', name=label, exact=True)
                    if await button.count():
                        await button.first.click()
                        break
                await page.locator('.ytp-play-button').first.click(timeout=10000)
                while True:
                    await page.wait_for_timeout(250)
                    state = await page.evaluate(STATE)
                    eligible = (state.get('id') == job.video and not state.get('ad')
                        and state.get('group') in job.groups and state.get('expected')
                        and state.get('duration') and abs(state['duration']-state['expected']) < 2)
                    if eligible:
                        if job.selected is not None and job.selected != state['group']:
                            raise RuntimeError('Media representation changed')
                        job.selected = state['group']
                        job.duration = state['duration']
                        total = captured_length(state, job.audio['mime'])
                        if total:
                            previous = job.audio.setdefault('total', total)
                            if previous != total or job.audio['size'] > total:
                                raise RuntimeError('Media size changed')
                        await job.signal()
                        if capture_complete(job, state.get('ended')):
                            await page.evaluate('async()=>{await Promise.all(window.__audioWrites)}')
                            if job.audio.get('total', job.audio['size']) != job.audio['size']:
                                raise RuntimeError('Incomplete media capture')
                            job.done = True
                            await job.signal()
                            break
    except BaseException as exc:
        job.error = True
        print(f'browser_audio failed video={job.video} error={type(exc).__name__}', flush=True)
        await job.signal()
    finally:
        if context:
            await context.close()

def capture_complete(job, ended=False):
    return ended or bool(job.audio and job.audio.get('total') and
                         job.audio['size'] == job.audio['total'])


def get_job(video, *, background=False, priority=0):
    if not re.fullmatch(r'[A-Za-z0-9_-]{11}', video):
        raise HTTPException(400, 'Invalid source ID')
    for key, old in list(jobs.items()):
        if (old.done or old.error) and not old.readers and (
                time.monotonic()-old.created > (30 if old.error else 1800)
                or len(jobs) >= 12):
            for file in old.directory.iterdir():
                file.unlink()
            old.directory.rmdir()
            del jobs[key]
    if video not in jobs:
        if len(jobs) >= 12 or sum(not(j.done or j.error) for j in jobs.values()) >= (3 if background else 4):
            raise HTTPException(503, 'Browser preparation capacity reached')
        job = jobs[video] = Job(video, Path(tempfile.mkdtemp(dir=root)), foreground=not background,
                               priority=2 if background else max(0, min(priority, 2)))
        job.task = asyncio.create_task(capture(job))
    elif not background and not jobs[video].foreground:
        jobs[video].foreground = True
        jobs[video].priority = priority
        asyncio.create_task(wake_slots())
    elif not background and priority < jobs[video].priority:
        jobs[video].priority = priority
        asyncio.create_task(wake_slots())
    return jobs[video]

@asynccontextmanager
async def lifespan(app):
    global browser, root
    with tempfile.TemporaryDirectory(prefix='browser-audio-') as directory:
        root = directory
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=False, args=['--no-sandbox'])
            yield
            for job in jobs.values():
                if job.task:
                    job.task.cancel()
            await asyncio.gather(*(j.task for j in jobs.values()), return_exceptions=True)
            await browser.close()
            jobs.clear()

app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

@app.get('/health')
async def health():
    return {'ready': browser is not None and browser.is_connected()}

@app.post('/resolve/{video}')
async def resolve(video: str, background: bool = False, priority: int = 0):
    started = time.monotonic()
    job = get_job(video, background=True) if background else (get_job(video, priority=priority) if priority else get_job(video))
    try:
        await job.wait(lambda: job.audio and job.audio['size'] >= 65536
                       and (job.audio.get('total') or job.done), timeout=75)
    except TimeoutError:
        raise HTTPException(504, 'Browser preparation timed out')
    print(f'browser_audio ready video={video} background={background} '
          f'requestMs={int((time.monotonic()-started)*1000)} '
          f'queueMs={max(0,int((job.started-job.created)*1000))} complete={job.done}', flush=True)
    return {'mime_type': job.audio['mime'], 'duration': job.duration,
            'complete': job.done, 'bytes': job.audio['size'],
            'capture': job.directory.name}

@app.get('/stream/{video}')
async def stream(video: str, request: Request):
    capture = request.query_params.get('capture')
    if capture and (video not in jobs or jobs[video].directory.name != capture):
        raise HTTPException(410, 'Playback capture expired')
    background = request.query_params.get('background') == '1'
    priority = int(request.query_params.get('priority', '0'))
    job = get_job(video, background=True) if background else (get_job(video, priority=priority) if priority else get_job(video))
    header = request.headers.get('range')
    match = re.fullmatch(r'bytes=(\d+)-(\d*)', header or '')
    if header and not match:
        raise HTTPException(416, 'Unsupported range')
    start = int(match[1]) if match else 0
    end = int(match[2]) if match and match[2] else None
    try:
        await job.wait(lambda: job.audio and (job.audio['size'] > start or job.done
                       or job.audio.get('total') is not None and start >= job.audio['total']), timeout=60)
        if end is not None and not job.audio.get('total'):
            await job.wait(lambda: job.done or job.audio['size'] > end, timeout=60)
        elif start and not job.audio.get('total'):
            await job.wait(lambda: job.done, timeout=60)
    except TimeoutError:
        raise HTTPException(504, 'Requested audio is not ready')
    size = job.audio['size']
    if (job.done and start >= size or job.audio.get('total') is not None and start >= job.audio['total']
            or end is not None and end < start):
        raise HTTPException(416, 'Invalid range')
    headers = {'Accept-Ranges': 'bytes', 'Cache-Control': 'no-store'}
    status = 200
    total = size if job.done else job.audio.get('total')
    if total is not None or end is not None:
        limit = total if total is not None else size
        end = min(end, limit-1) if end is not None else limit-1
        headers['Content-Length'] = str(end-start+1)
        if match:
            status = 206
            headers['Content-Range'] = f'bytes {start}-{end}/{total if total is not None else "*"}'
    async def body():
        job.readers += 1
        offset = start
        try:
            with job.audio['path'].open('rb') as f:
                f.seek(start)
                while True:
                    if job.error:
                        raise RuntimeError('Audio capture failed')
                    limit = 65536 if end is None else min(65536, end-offset+1)
                    chunk = f.read(limit)
                    if chunk:
                        offset += len(chunk)
                        yield chunk
                    elif job.done:
                        break
                    else:
                        await job.wait(lambda: job.done or job.audio['size'] > offset, timeout=60)
                    if end is not None and offset > end:
                        break
        finally:
            job.readers -= 1
    return StreamingResponse(body(), status_code=status, headers=headers, media_type=job.audio['mime'])
