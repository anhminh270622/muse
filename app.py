import asyncio
import json
import os
import re
import sqlite3
import uuid
import secrets
import time
import io
import zipfile
import hashlib
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, UploadFile, File
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
import httpx

from media import merge, probe, last_frame, boundary_similarity, command
from postprocess import finish_episode
from muse import Muse, NeedsAttention, validate_session, validate_url

ROOT = Path(__file__).resolve().parent
DATA = Path(os.environ.get('MUSE_DATA_DIR', ROOT / 'data')).resolve()
DATA.mkdir(parents=True, exist_ok=True, mode=0o700)
os.chmod(DATA, 0o700)
DEFAULT = {'url': 'https://muse.ai/', 'input_selector': 'textarea, [contenteditable="true"][role="textbox"]',
           'send_selector': '', 'video_selector': '[data-testid="hatch-chat-attachment-presentation-video"] video', 'timeout_seconds': 900}
active = None
stopping = False
mutation_lock = asyncio.Lock()
browser_challenges = {}


def save_session(value):
    path = DATA / 'session.json'
    temporary = DATA / 'session.tmp'
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(value, stream)
    temporary.replace(path)
    os.chmod(path, 0o600)
    (DATA / 'runtime-session.json').unlink(missing_ok=True)


def db():
    connection = sqlite3.connect(DATA / 'jobs.sqlite3')
    connection.row_factory = sqlite3.Row
    return connection


def execute(sql, params=()):
    with db() as con:
        con.execute(sql, params)


def rows(sql, params=()):
    with db() as con:
        return [dict(x) for x in con.execute(sql, params).fetchall()]


def initialize():
    execute('CREATE TABLE IF NOT EXISTS projects (id TEXT PRIMARY KEY, title TEXT, style TEXT, status TEXT, message TEXT, created TEXT DEFAULT CURRENT_TIMESTAMP)')
    execute('CREATE TABLE IF NOT EXISTS scenes (id TEXT PRIMARY KEY, project_id TEXT, position INTEGER, prompt TEXT, status TEXT, path TEXT, duration REAL, conversation_url TEXT)')
    with db() as con:
        columns = {r['name'] for r in con.execute('PRAGMA table_info(projects)')}
        if 'continuity' not in columns:
            con.execute('ALTER TABLE projects ADD COLUMN continuity INTEGER NOT NULL DEFAULT 0')
        scene_columns = {r['name'] for r in con.execute('PRAGMA table_info(scenes)')}
        if 'boundary_score' not in scene_columns:
            con.execute('ALTER TABLE scenes ADD COLUMN boundary_score REAL')
    execute("UPDATE projects SET status='attention', message='Tool đã khởi động lại. Đối chiếu cảnh đã gửi trước khi tiếp tục.' WHERE status='running'")
    execute("UPDATE scenes SET status='uncertain' WHERE status='submitted'")


@asynccontextmanager
async def lifespan(app):
    initialize()
    yield
    global stopping
    stopping = True
    if active and not active.done():
        try:
            await asyncio.wait_for(asyncio.shield(active), timeout=8)
        except asyncio.TimeoutError:
            active.cancel()
        except Exception:
            pass


app = FastAPI(lifespan=lifespan)


@app.middleware('http')
async def local_only(request: Request, call_next):
    if request.url.hostname not in {'localhost', '127.0.0.1', 'testserver'}:
        return JSONResponse({'detail': 'Chỉ cho phép localhost'}, status_code=403)
    origin = request.headers.get('origin')
    if origin and origin != f'{request.url.scheme}://{request.headers.get("host")}':
        return JSONResponse({'detail': 'Origin không hợp lệ'}, status_code=403)
    if request.method in {'POST', 'PUT', 'PATCH', 'DELETE'} and request.headers.get('x-muse-local') != '1':
        return JSONResponse({'detail': 'Thiếu header X-Muse-Local'}, status_code=403)
    if request.method in {'POST', 'PUT', 'PATCH', 'DELETE'}:
        async with mutation_lock:
            return await call_next(request)
    return await call_next(request)


def config():
    path = DATA / 'config.json'
    return {**DEFAULT, **(json.loads(path.read_text()) if path.exists() else {})}


def idle():
    if active and not active.done():
        raise HTTPException(409, 'Worker đang chạy; hãy dừng và chờ trước khi thay đổi')


def project(pid):
    result = rows('SELECT * FROM projects WHERE id=?', (pid,))
    if not result:
        raise HTTPException(404, 'Không tìm thấy project')
    value = result[0]
    value['scenes'] = rows('SELECT * FROM scenes WHERE project_id=? ORDER BY position', (pid,))
    for scene in value['scenes']:
        scene.pop('path', None)
    value['output'] = (DATA / pid / 'output.mp4').exists()
    return value


class ProjectInput(BaseModel):
    title: str = Field(min_length=1, max_length=160)
    script: str = Field(min_length=1, max_length=60000)
    style: str = Field(default='Cinematic, consistent characters, vertical 9:16, no text or watermark.', max_length=4000)
    continuity: bool = True


class ConfigInput(BaseModel):
    url: str
    input_selector: str = Field(min_length=1, max_length=1000)
    send_selector: str = Field(default='', max_length=1000)
    video_selector: str = Field(min_length=1, max_length=1000)
    timeout_seconds: int = Field(ge=30, le=3600)


class TelegramInput(BaseModel):
    bot_token: str = Field(default='', max_length=256)
    chat_id: str = Field(min_length=1, max_length=128)


def telegram_config():
    path = DATA / 'telegram.json'
    return json.loads(path.read_text()) if path.exists() else {}


def save_telegram_config(value):
    path = DATA / 'telegram.json'
    temporary = DATA / 'telegram.tmp'
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(value, stream)
    temporary.replace(path)
    os.chmod(path, 0o600)


@app.get('/api/config')
def get_config():
    settings = config()
    verified = False
    marker = DATA / 'adapter-verified.json'
    if marker.exists():
        try:
            proof = json.loads(marker.read_text())
            verified = all(proof.get(key) == settings[key] for key in ('input_selector', 'video_selector'))
        except (ValueError, TypeError):
            pass
    return {'config': settings, 'session_imported': (DATA / 'session.json').exists(), 'headless': True,
            'adapter_verified': verified}


@app.put('/api/config')
def set_config(body: ConfigInput):
    idle()
    try:
        validate_url(body.url)
    except ValueError as e:
        raise HTTPException(400, str(e))
    (DATA / 'config.json').write_text(json.dumps(body.model_dump()))
    return {'ok': True}


@app.get('/api/telegram/config')
def get_telegram_config():
    saved = telegram_config()
    return {'configured': bool(saved.get('bot_token') and saved.get('chat_id')),
            'chat_id': saved.get('chat_id', '')}


@app.put('/api/telegram/config')
async def set_telegram_config(body: TelegramInput):
    saved = telegram_config()
    token = body.bot_token.strip() or saved.get('bot_token', '')
    chat_id = body.chat_id.strip()
    if not chat_id:
        raise HTTPException(400, 'Nhập chat ID Telegram')
    if not token:
        raise HTTPException(400, 'Nhập Bot Token trước')
    if not re.fullmatch(r'\d{5,15}:[A-Za-z0-9_-]{20,}', token):
        raise HTTPException(400, 'Bot Token không đúng định dạng')
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
            response = await client.get(f'https://api.telegram.org/bot{token}/getMe')
        result = response.json()
    except (httpx.HTTPError, ValueError):
        raise HTTPException(502, 'Không xác minh được Bot Token với Telegram')
    if response.status_code != 200 or not result.get('ok'):
        raise HTTPException(400, 'Telegram không chấp nhận Bot Token')
    save_telegram_config({'bot_token': token, 'chat_id': chat_id})
    return {'ok': True, 'bot_name': result.get('result', {}).get('username', '')}


@app.post('/api/projects/{pid}/telegram')
def send_project_telegram(pid: str, force: bool = False):
    settings = telegram_config()
    if not settings.get('bot_token') or not settings.get('chat_id'):
        raise HTTPException(400, 'Cấu hình Telegram ở khung bên trái trước')
    path = DATA / pid / 'output.mp4'
    if not path.is_file():
        raise HTTPException(404, 'Project chưa có MP4 hoàn chỉnh')
    size = path.stat().st_size
    if size > 50_000_000:
        raise HTTPException(413, f'File {size / 1_000_000:.1f} MB vượt giới hạn Bot API 50 MB')
    title = rows('SELECT title FROM projects WHERE id=?', (pid,))
    if not title:
        raise HTTPException(404, 'Không tìm thấy project')
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    receipt_path = DATA / pid / 'telegram-send-receipt.json'
    if receipt_path.exists():
        receipt = json.loads(receipt_path.read_text())
        if receipt.get('sha256') == digest and not force:
            return {'ok': True, 'already_sent': True, 'message': 'Video này đã gửi vào Telegram trước đó',
                    'message_id': receipt.get('message_id'), 'size_bytes': size}
    token, chat_id = settings['bot_token'], settings['chat_id']
    try:
        duration = round(probe(path)[0])
        thumbnail = DATA / pid / 'telegram-thumbnail.jpg'
        command(['ffmpeg', '-y', '-v', 'error', '-ss', '5', '-i', str(path), '-frames:v', '1',
                 '-vf', 'scale=320:568:force_original_aspect_ratio=decrease', '-q:v', '3', str(thumbnail)])
        with path.open('rb') as video, thumbnail.open('rb') as thumb, httpx.Client(timeout=httpx.Timeout(300, connect=20), follow_redirects=False) as client:
            response = client.post(
                f'https://api.telegram.org/bot{token}/sendVideo',
                data={'chat_id': chat_id, 'caption': title[0]['title'], 'supports_streaming': 'true',
                      'width': '1080', 'height': '1920', 'duration': str(duration)},
                files={'video': (path.name, video, 'video/mp4'),
                       'thumbnail': (thumbnail.name, thumb, 'image/jpeg')},
            )
        result = response.json()
    except (httpx.HTTPError, ValueError):
        raise HTTPException(502, 'Gửi thất bại; kiểm tra kết nối và quyền của bot trong chat Telegram')
    if response.status_code != 200 or not result.get('ok'):
        description = result.get('description', 'Telegram từ chối gửi video')
        raise HTTPException(502, f'Telegram: {description}')
    receipt = {'sha256': digest, 'message_id': result.get('result', {}).get('message_id'),
               'chat_id': chat_id, 'sent_at': time.time()}
    temporary = receipt_path.with_suffix('.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(receipt, stream)
    temporary.replace(receipt_path)
    os.chmod(receipt_path, 0o600)
    return {'ok': True, 'message': 'Đã gửi video vào Telegram',
            'message_id': result.get('result', {}).get('message_id'), 'size_bytes': size}


@app.get('/api/projects/{pid}/telegram/status')
def project_telegram_status(pid: str):
    project(pid)
    receipt_path = DATA / pid / 'telegram-send-receipt.json'
    if not receipt_path.exists():
        return {'sent': False}
    receipt = json.loads(receipt_path.read_text())
    return {'sent': True, 'message_id': receipt.get('message_id'),
            'sha256': receipt.get('sha256'), 'sent_at': receipt.get('sent_at')}


@app.post('/api/session')
async def import_session(file: UploadFile = File(...)):
    idle()
    content = await file.read(2_000_001)
    if len(content) > 2_000_000:
        raise HTTPException(400, 'File phiên tối đa 2 MB')
    try:
        value = validate_session(json.loads(content))
    except (ValueError, TypeError, KeyError):
        raise HTTPException(400, 'JSON phiên không hợp lệ. Dùng cookies JSON hoặc Playwright storageState của tài khoản Muse.')
    save_session(value)
    return {'ok': True}


@app.get('/api/browser/challenge')
def browser_challenge(request: Request):
    idle()
    now = time.monotonic()
    for token, (_, expires) in list(browser_challenges.items()):
        if expires < now:
            browser_challenges.pop(token, None)
    if len(browser_challenges) >= 100:
        raise HTTPException(429, 'Quá nhiều yêu cầu kết nối. Thử lại sau ít phút.')
    token = secrets.token_urlsafe(32)
    browser_challenges[token] = (request.headers.get('host'), now + 120)
    return {'protocol': 'muse-local-bridge-v1', 'token': token}


@app.post('/api/browser/connect')
async def browser_connect(request: Request):
    idle()
    # Limit before parsing; never echo validation details containing credentials.
    content = bytearray()
    async for chunk in request.stream():
        content.extend(chunk)
        if len(content) > 2_000_000:
            raise HTTPException(400, 'Phiên vượt quá 2 MB')
    try:
        body = json.loads(content)
        token = body.get('token')
        if not isinstance(token, str):
            raise ValueError()
        challenge = browser_challenges.pop(token, None)
        if not challenge or challenge[0] != request.headers.get('host') or challenge[1] < time.monotonic():
            raise HTTPException(403, 'Yêu cầu kết nối hết hạn. Bấm Kết nối lại.')
        value = validate_session(body.get('session'))
        # The bridge only has permissions for Muse; do not accept other account domains.
        value['cookies'] = [c for c in value['cookies'] if c['domain'].lstrip('.') in {'muse.ai', 'www.muse.ai'}]
        if not value['cookies'] and not value['origins']:
            raise ValueError()
    except (ValueError, TypeError, KeyError, AttributeError):
        raise HTTPException(400, 'Không nhận được phiên Muse hợp lệ')
    save_session(value)
    return {'ok': True, 'verified': False, 'message': 'Đã nhận phiên; cần kiểm tra đăng nhập.'}


@app.get('/api/browser/extension.zip')
def browser_extension():
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as package:
        for name in ('manifest.json', 'background.js', 'content.js'):
            package.write(ROOT / 'browser-extension' / name, name)
    return Response(archive.getvalue(), media_type='application/zip',
                    headers={'Content-Disposition': 'attachment; filename="muse-local-connector.zip"'})


@app.post('/api/connection/check')
async def check_connection():
    global active
    idle()
    async def check():
        try:
            async with Muse(config(), DATA) as muse:
                url = await muse.check()
                return {'ok': True, 'url': url, 'message': 'Đã thấy ô chat. Chưa xác minh tạo và tải video.'}
        except Exception:
            return {'ok': False, 'message': 'Chưa kết nối được ô chat Muse. Kiểm tra phiên, URL, selector và Chromium. Không có cửa sổ nào được mở.'}
    active = asyncio.create_task(check())
    return await active


@app.get('/api/projects')
def list_projects():
    return [project(p['id']) for p in rows('SELECT id FROM projects ORDER BY created DESC, rowid DESC')]


@app.post('/api/projects')
def create_project(body: ProjectInput):
    scenes = [s.strip() for s in re.split(r'\n\s*\n', body.script.strip()) if s.strip()]
    if not scenes or len(scenes) > 30:
        raise HTTPException(400, 'Cần 1–30 cảnh; cách mỗi cảnh bằng một dòng trống')
    pid = uuid.uuid4().hex
    (DATA / pid).mkdir()
    with db() as con:
        con.execute('INSERT INTO projects(id,title,style,status,message,continuity) VALUES(?,?,?,?,?,?)',
                    (pid, body.title, body.style, 'draft', 'Mỗi đoạn cách dòng trống là một cảnh. Chưa gửi Muse.', int(body.continuity)))
        for i, text in enumerate(scenes):
            con.execute('INSERT INTO scenes(id,project_id,position,prompt,status,path,duration,conversation_url) VALUES(?,?,?,?,?,?,?,?)',
                        (uuid.uuid4().hex, pid, i+1, text, 'pending', None, None, None))
    return project(pid)


async def worker(pid, render_only=False, max_new_clips=0):
    global stopping
    stopping = False
    execute("UPDATE projects SET status='running', message='Đang xử lý' WHERE id=?", (pid,))
    try:
        scenes = rows('SELECT * FROM scenes WHERE project_id=? ORDER BY position', (pid,))
        if any(s['status'] in ('uncertain', 'submitted', 'review') for s in scenes):
            raise NeedsAttention('Có cảnh đã gửi nhưng chưa xác nhận kết quả. Nhập clip hoặc xác nhận gửi lại riêng cảnh đó.')
        unfinished = [s for s in scenes if s['status'] != 'done']
        if render_only and unfinished:
            raise NeedsAttention('Chưa đủ clip. Nhập clip cho mọi cảnh trước khi ghép.')
        if unfinished:
            completed_now = 0
            settings = config()
            conversations = [s['conversation_url'] for s in scenes if s['conversation_url'] and '/thread/new' not in s['conversation_url']]
            if conversations:
                settings['url'] = validate_url(conversations[-1])
            async with Muse(settings, DATA) as muse:
                await muse.check()
                if not conversations and not any(s['status'] == 'done' for s in scenes):
                    new_chat = muse.page.get_by_role('button', name='New side chat', exact=True)
                    if await new_chat.count():
                        # A full navigation avoids the old main-chat composer remaining bound
                        # during the client-side transition into a new side chat.
                        await muse.page.goto('https://muse.ai/thread/new', wait_until='domcontentloaded')
                        await muse.check()
                        await muse.page.wait_for_timeout(1000)
                for scene in unfinished:
                    if stopping:
                        raise NeedsAttention('Đã dừng hàng đợi')
                    p = rows('SELECT style,continuity FROM projects WHERE id=?', (pid,))[0]
                    prompt = f"{p['style']}\n\nScene {scene['position']}/{len(scenes)}. Create one video clip approximately 10 seconds.\n{scene['prompt']}"
                    reference = None
                    opening_frame = DATA / pid / 'opening-frame.png'
                    if scene['position'] == 1 and opening_frame.is_file():
                        reference = opening_frame
                        prompt = ('OPENING FRAME ANIMATION: The attached image is the exact opening frame of this project. '
                                  'Animate directly from it; preserve the person, pose, framing, clothes and lighting at frame zero. '
                                  'Do not generate a different starting image or reuse any old video. Continue the action described below.\n\n' + prompt)
                    elif p['continuity'] and scene['position'] > 1:
                        previous = rows('SELECT path,status FROM scenes WHERE project_id=? AND position=?', (pid, scene['position']-1))
                        if not previous or previous[0]['status'] != 'done' or not previous[0]['path']:
                            raise NeedsAttention('Thiếu clip cảnh trước để lấy khung hình nối cảnh. Chưa gửi prompt mới.')
                        reference = DATA / pid / f"reference-{scene['position']:03}.png"
                        await asyncio.to_thread(last_frame, previous[0]['path'], reference)
                        prompt = ('IMAGE-TO-VIDEO CONTINUATION: Animate the attached image as the exact first frame of this 10-second clip. '
                                  'It is the final frame of the previous scene, not a mood board. Preserve its camera position, lens, '
                                  'character identity, wardrobe, pose, screen direction, environment geometry and lighting at the join. '
                                  'Do not restart actions already completed. Begin with continuous natural motion from the supplied pose; '
                                  'the attached frame takes precedence over any conflicting opening pose or establishing shot in the script. '
                                  'Do not show the image as a photograph, slideshow or inset. No cut, fade or scene reset at the beginning.\n\n' + prompt)
                    def submitted(url, sid=scene['id']):
                        execute("UPDATE scenes SET status='submitted', conversation_url=? WHERE id=?", (url, sid))
                    def located(url, sid=scene['id']):
                        execute('UPDATE scenes SET conversation_url=? WHERE id=?', (url, sid))
                    target = DATA / pid / f"scene-{scene['position']:03}.mp4"
                    execute("UPDATE projects SET message=? WHERE id=?", (f"Muse đang tạo cảnh {scene['position']}/{len(scenes)}", pid))
                    await muse.generate(prompt, target, submitted, located, lambda: stopping, reference=reference)
                    duration, _ = await asyncio.to_thread(probe, target)
                    if reference:
                        score = await asyncio.to_thread(boundary_similarity, reference, target)
                        execute('UPDATE scenes SET boundary_score=? WHERE id=?', (score, scene['id']))
                        if score < 0.85:
                            execute("UPDATE scenes SET status='review',path=?,duration=? WHERE id=?", (str(target), duration, scene['id']))
                            anchor = 'ảnh mở đầu' if scene['position'] == 1 else 'ảnh cuối cảnh trước'
                            raise NeedsAttention(f"Khung đầu cảnh {scene['position']} còn lệch {anchor}. Đã tải clip để đối chiếu và dừng trước cảnh tiếp theo; chưa tự gửi lại.")
                    execute("UPDATE scenes SET status='done', path=?, duration=? WHERE id=?", (str(target), duration, scene['id']))
                    completed_now += 1
                    if max_new_clips and completed_now >= max_new_clips and scene != unfinished[-1]:
                        execute("UPDATE projects SET status='draft',message='Đã tạo cảnh mới; tạm dừng để kiểm tra điểm nối.' WHERE id=?", (pid,))
                        return
        if stopping:
            raise NeedsAttention('Đã dừng trước bước ghép')
        execute("UPDATE projects SET message='Đang ghép MP4 bằng FFmpeg' WHERE id=?", (pid,))
        clips = rows('SELECT path FROM scenes WHERE project_id=? ORDER BY position', (pid,))
        await asyncio.to_thread(merge, [s['path'] for s in clips], DATA / pid)
        plan_file = DATA / pid / 'postproduction.json'
        if plan_file.is_file():
            execute("UPDATE projects SET message='Đang hậu kỳ hồi ức và giọng hệ thống' WHERE id=?", (pid,))
            plan = json.loads(plan_file.read_text())
            await asyncio.to_thread(finish_episode, DATA / pid / 'output.mp4', plan, DATA / pid, DATA)
        execute("UPDATE projects SET status='done', message='Đã xuất MP4. Thời lượng theo các clip thực tế.' WHERE id=?", (pid,))
    except Exception as e:
        execute("UPDATE scenes SET status='uncertain' WHERE project_id=? AND status='submitted'", (pid,))
        # Do not persist exception text from browser requests: it may contain signed URLs.
        message = str(e) if isinstance(e, NeedsAttention) else 'Lỗi xử lý. Kiểm tra phiên/selector Muse, file clip hoặc FFmpeg. Không tự gửi lại cảnh đã gửi.'
        execute("UPDATE projects SET status='attention', message=? WHERE id=?", (message, pid))


@app.post('/api/projects/{pid}/start')
async def start(pid: str, render_only: bool = False, max_new_clips: int = 0):
    global active
    idle()
    if not 0 <= max_new_clips <= 30:
        raise HTTPException(400, 'max_new_clips phải từ 0 đến 30')
    p = project(pid)
    if any(s['status'] in ('uncertain','submitted','review') for s in p['scenes']):
        raise HTTPException(409, 'Đối chiếu cảnh chưa rõ kết quả trước khi tiếp tục')
    active = asyncio.create_task(worker(pid, render_only, max_new_clips))
    return {'ok': True}


@app.post('/api/stop')
def stop():
    global stopping
    stopping = True
    return {'ok': True, 'message': 'Đang dừng ở điểm an toàn. FFmpeg đang ghép sẽ hoàn thành bước hiện tại.'}


@app.post('/api/projects/{pid}/scenes/{sid}/reset')
def reset_scene(pid: str, sid: str):
    idle()
    selected = rows('SELECT position FROM scenes WHERE id=? AND project_id=?', (sid, pid))
    if not selected:
        raise HTTPException(404, 'Không tìm thấy cảnh')
    chained = rows('SELECT continuity FROM projects WHERE id=?', (pid,))[0]['continuity']
    if chained:
        execute("UPDATE scenes SET status='pending',path=NULL,duration=NULL,boundary_score=NULL WHERE project_id=? AND position>=?", (pid, selected[0]['position']))
    else:
        execute("UPDATE scenes SET status='pending', path=NULL, duration=NULL,boundary_score=NULL WHERE id=?", (sid,))
    (DATA / pid / 'output.mp4').unlink(missing_ok=True)
    execute("UPDATE projects SET status='draft', message='Đã xác nhận cho phép gửi lại cảnh' WHERE id=?", (pid,))
    return {'ok': True, 'downstream_reset': bool(chained)}


@app.post('/api/projects/{pid}/scenes/{sid}/clip')
async def upload_clip(pid: str, sid: str, file: UploadFile = File(...)):
    idle()
    scene = rows('SELECT * FROM scenes WHERE id=? AND project_id=?', (sid, pid))
    if not scene:
        raise HTTPException(404, 'Không tìm thấy cảnh')
    target = DATA / pid / f"scene-{scene[0]['position']:03}.mp4"
    temporary = target.with_suffix('.upload.mp4')
    try:
        size = 0
        with temporary.open('wb') as stream:
            while chunk := await file.read(1024*1024):
                size += len(chunk)
                if size > 512*1024*1024:
                    raise ValueError('Clip tối đa 512 MB')
                stream.write(chunk)
        duration, _ = await asyncio.to_thread(probe, temporary)
        temporary.replace(target)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise HTTPException(400, 'Clip không hợp lệ hoặc vượt 512 MB')
    execute("UPDATE scenes SET status='done',path=?,duration=?,boundary_score=NULL WHERE id=?", (str(target), duration, sid))
    if rows('SELECT continuity FROM projects WHERE id=?', (pid,))[0]['continuity']:
        execute("UPDATE scenes SET status='pending',path=NULL,duration=NULL,boundary_score=NULL WHERE project_id=? AND position>?",
                (pid, scene[0]['position']))
    (DATA / pid / 'output.mp4').unlink(missing_ok=True)
    execute("UPDATE projects SET status='draft', message='Đã nhập clip' WHERE id=?", (pid,))
    return {'ok': True, 'duration': duration}


@app.get('/api/projects/{pid}/output')
def output(pid: str):
    project(pid)
    path = DATA / pid / 'output.mp4'
    if not path.exists():
        raise HTTPException(404, 'Chưa có video hoàn chỉnh')
    return FileResponse(path, media_type='video/mp4', filename=f'muse-{pid[:8]}.mp4')


@app.get('/api/projects/{pid}/scenes/{sid}/clip')
def clip(pid: str, sid: str):
    scene = rows("SELECT path FROM scenes WHERE id=? AND project_id=? AND status IN ('done','review')", (sid, pid))
    if not scene or not scene[0]['path'] or not Path(scene[0]['path']).is_file():
        raise HTTPException(404, 'Chưa có clip')
    return FileResponse(scene[0]['path'], media_type='video/mp4')


app.mount('/', StaticFiles(directory=ROOT / 'static', html=True), name='ui')
