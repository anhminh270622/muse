import importlib
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv('MUSE_DATA_DIR', str(tmp_path / 'data'))
    import app
    module = importlib.reload(app)
    with TestClient(module.app) as client:
        yield client, module


def create(c):
    response = c.post('/api/projects', headers={'X-Muse-Local':'1'}, json={
        'title':'Test story', 'script':'Scene one\n\nScene two', 'style':'Vertical cinematic'})
    assert response.status_code == 200, response.text
    return response.json()


def test_local_access_and_session_validation(client):
    c, app = client
    assert c.post('/api/projects', json={}).status_code == 403
    assert c.post('/api/stop', headers={'X-Muse-Local':'1','Origin':'https://evil.example'}).status_code == 403
    assert c.post('/api/session', headers={'X-Muse-Local':'1'}, files={'file':('session.json',b'{"token":"secret"}')}).status_code == 400
    state = b'{"cookies":[{"name":"session","value":"private-cookie-fixture-123","domain":".muse.ai","path":"/"}],"origins":[]}'
    assert c.post('/api/session', headers={'X-Muse-Local':'1'}, files={'file':('session.json',state)}).status_code == 200
    assert (app.DATA / 'session.json').stat().st_mode & 0o777 == 0o600
    assert 'private-cookie-fixture-123' not in c.get('/api/config').text


def test_crash_recovery_blocks_resubmit(client):
    c, app = client
    p = create(c)
    app.execute("UPDATE scenes SET status='submitted' WHERE id=?", (p['scenes'][0]['id'],))
    app.execute("UPDATE projects SET status='running' WHERE id=?", (p['id'],))
    app.initialize()
    p = c.get('/api/projects').json()[0]
    assert p['status'] == 'attention'
    assert p['scenes'][0]['status'] == 'uncertain'
    assert c.post(f"/api/projects/{p['id']}/start", headers={'X-Muse-Local':'1'}).status_code == 409


def test_merge_real_clips_and_resume(client, tmp_path):
    c, app = client
    p = create(c)
    for i, scene in enumerate(p['scenes']):
        path = tmp_path / f'{i}.mp4'
        # Exercise normalization: different aspect ratios, one clip with audio and one without.
        args = ['ffmpeg','-y','-v','error','-f','lavfi','-i',f'color=c={"red" if i == 0 else "blue"}:s={"160x90" if i == 0 else "90x160"}:r=15']
        if i:
            args += ['-f','lavfi','-i','sine=frequency=440:sample_rate=44100']
        args += ['-t','0.4','-c:v','libx264','-pix_fmt','yuv420p', str(path)]
        subprocess.run(args,check=True)
        with path.open('rb') as stream:
            r = c.post(f"/api/projects/{p['id']}/scenes/{scene['id']}/clip", headers={'X-Muse-Local':'1'}, files={'file':('clip.mp4',stream,'video/mp4')})
        assert r.status_code == 200, r.text
    # Run the actual worker through its event loop; completed clips must bypass Muse entirely.
    import asyncio
    asyncio.run(app.worker(p['id'],render_only=True))
    final = c.get('/api/projects').json()[0]
    assert final['status'] == 'done', final
    duration, audio = app.probe(app.DATA / p['id'] / 'output.mp4')
    assert abs(duration - .8) < .001
    assert audio
    assert c.get(f"/api/projects/{p['id']}/output").status_code == 200
    sid = p['scenes'][0]['id']
    assert c.post(f"/api/projects/{p['id']}/scenes/{sid}/reset",headers={'X-Muse-Local':'1'}).status_code == 200
    assert c.get(f"/api/projects/{p['id']}/output").status_code == 404
    assert all(s['status']=='pending' for s in c.get('/api/projects').json()[0]['scenes'])


def test_headless_blob_download(tmp_path):
    import asyncio
    import base64
    import json
    from playwright.async_api import async_playwright
    from muse import Muse, validate_url
    sample = tmp_path / 'sample.mp4'
    subprocess.run(['ffmpeg','-y','-v','error','-f','lavfi','-i','color=c=green:s=80x120:r=15',
                    '-t','0.4','-c:v','libx264','-pix_fmt','yuv420p',str(sample)], check=True)
    encoded = base64.b64encode(sample.read_bytes()).decode()
    async def run():
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            context = await browser.new_context(accept_downloads=True)
            page = await context.new_page()
            await page.set_content('<textarea></textarea><video src="blob:old-video"></video>')
            await page.evaluate("""b64 => document.querySelector('textarea').addEventListener('keydown', e => {
                if(e.key !== 'Enter') return;
                const bytes = Uint8Array.from(atob(b64), c => c.charCodeAt(0));
                const avatarWrapper = document.createElement('div');
                avatarWrapper.setAttribute('role','img');
                const avatar = document.createElement('video');
                avatar.src = URL.createObjectURL(new Blob([bytes], {type:'video/mp4'}));
                avatarWrapper.appendChild(avatar); document.body.appendChild(avatarWrapper);
                const v = document.createElement('video'); v.id = 'result-video';
                v.src = URL.createObjectURL(new Blob([bytes], {type:'video/mp4'}));
                document.body.appendChild(v);
            })""", encoded)
            adapter = Muse({'input_selector':'textarea','send_selector':'','video_selector':'video','timeout_seconds':30}, tmp_path)
            adapter.page, adapter.context = page, context
            selected_sources=[]
            original_download = adapter.download_media
            async def track_download(src, target):
                selected_sources.append(src)
                return await original_download(src,target)
            adapter.download_media = track_download
            events=[]
            target = tmp_path / 'downloaded.mp4'
            await adapter.generate('Test prompt', target, lambda u:events.append('submitted'), lambda u:events.append('downloaded'), lambda:False)
            assert target.read_bytes() == sample.read_bytes()
            assert selected_sources == [await page.locator('#result-video').evaluate('(v)=>v.currentSrc||v.src')]
            assert events == ['submitted','downloaded']
            await browser.close()
    asyncio.run(run())
    with pytest.raises(ValueError):
        validate_url('http://127.0.0.1/private')


def test_browser_bridge_tokens_and_scope(client):
    import json
    import zipfile
    import io
    c, app = client
    session = {'cookies': [
        {'name':'muse-session','value':'private-value','domain':'.muse.ai','path':'/'},
        {'name':'other-account','value':'do-not-import','domain':'.facebook.com','path':'/'}],
        'origins':[{'origin':'https://muse.ai','localStorage':[{'name':'test','value':'local-value'}],
                    'sessionStorage':[{'name':'tab-token','value':'tab-value'}]}]}
    assert c.post('/api/browser/connect',headers={'X-Muse-Local':'1'},json={'token':'bad','session':session}).status_code == 403
    challenge = c.get('/api/browser/challenge').json()
    payload = {'token':challenge['token'],'session':session}
    (app.DATA / 'runtime-session.json').write_text('{}')
    response = c.post('/api/browser/connect',headers={'X-Muse-Local':'1'},json=payload)
    assert response.status_code == 200
    assert response.json()['verified'] is False
    assert not (app.DATA / 'runtime-session.json').exists()
    assert 'private-value' not in response.text
    stored = json.loads((app.DATA / 'session.json').read_text())
    assert len(stored['cookies']) == 1
    assert stored['cookies'][0]['domain'] == '.muse.ai'
    assert stored['origins'][0]['sessionStorage'][0]['value'] == 'tab-value'
    assert c.post('/api/browser/connect',headers={'X-Muse-Local':'1'},json=payload).status_code == 403
    archive = zipfile.ZipFile(io.BytesIO(c.get('/api/browser/extension.zip').content))
    manifest = json.loads(archive.read('manifest.json'))
    assert set(manifest['host_permissions']) == {'https://muse.ai/*','https://www.muse.ai/*'}
    assert set(archive.namelist()) == {'manifest.json','background.js','content.js'}


def test_extension_transfers_session_in_headless_browser(tmp_path):
    """Real extension APIs, source tab, trusted click and local HTTP server; no Muse account."""
    import asyncio
    import json
    import os
    import time
    import httpx
    from playwright.async_api import async_playwright
    root = Path(__file__).resolve().parents[1]
    env = {**os.environ, 'MUSE_DATA_DIR':str(tmp_path / 'server-data')}
    # The extension explicitly allows 8765 and 8766. Do not stop or reuse a user's server.
    import socket
    with socket.socket() as sock:
        try:
            sock.bind(('127.0.0.1',8766))
        except OSError:
            pytest.skip('Port 8766 is already in use')
    process = subprocess.Popen([sys.executable,'-m','uvicorn','app:app','--host','127.0.0.1','--port','8766'],
                               cwd=root,env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    try:
        for _ in range(100):
            try:
                if httpx.get('http://127.0.0.1:8766/api/config').status_code == 200:
                    break
            except httpx.TransportError:
                pass
            time.sleep(.05)
        else:
            pytest.fail('Local test server did not start')
        async def run():
            async with async_playwright() as pw:
                extension = root / 'browser-extension'
                context = await pw.chromium.launch_persistent_context(str(tmp_path/'extension-profile'),
                    headless=True, channel='chromium', args=[f'--disable-extensions-except={extension}',f'--load-extension={extension}'])
                try:
                    await context.add_cookies([{'name':'session-test','value':'fake-session-value',
                                               'domain':'.muse.ai','path':'/','secure':True}])
                    source = await context.new_page()
                    await source.route('https://muse.ai/**',lambda route:route.fulfill(content_type='text/html',
                        body='<textarea></textarea><script>localStorage.setItem("fixture-local","local-secret");sessionStorage.setItem("fixture-tab","tab-secret")</script>'))
                    await source.goto('https://muse.ai/')
                    tool = await context.new_page()
                    # Login verification is a separate operation: fixture tests transfer only.
                    await tool.route('**/api/connection/check',lambda route:route.fulfill(content_type='application/json',
                                     body=json.dumps({'ok':False,'message':'fixture: not verified'})))
                    await tool.goto('http://127.0.0.1:8766/')
                    await tool.wait_for_function('document.documentElement.dataset.museConnector === "1"')
                    before = len(context.pages)
                    await tool.get_by_role('button',name='Kết nối trình duyệt đã đăng nhập',exact=True).click()
                    await tool.wait_for_function('document.querySelector("#connection").textContent.includes("Đã nhận phiên nhưng")')
                    assert len(context.pages) == before
                    assert source.url == 'https://muse.ai/'
                    stored=json.loads((tmp_path/'server-data'/'session.json').read_text())
                    assert stored['cookies'][0]['value'] == 'fake-session-value'
                    assert stored['origins'][0]['localStorage'] == [{'name':'fixture-local','value':'local-secret'}]
                    assert stored['origins'][0]['sessionStorage'] == [{'name':'fixture-tab','value':'tab-secret'}]
                    assert 'fake-session-value' not in await tool.content()
                finally:
                    await context.close()
        asyncio.run(run())
    finally:
        process.terminate()
        process.wait(timeout=10)


def test_imported_session_keeps_refreshed_cookies(tmp_path, monkeypatch):
    import asyncio
    import json
    from types import SimpleNamespace
    from playwright.async_api import async_playwright
    import muse

    def stored(value):
        return {'cookies':[{'name':'auth','value':value,'domain':'.muse.ai','path':'/','secure':True}],
                'origins':[{'origin':'https://muse.ai','localStorage':[{'name':'local','value':value}],
                            'sessionStorage':[{'name':'tab','value':value}]}]}

    class Factory:
        async def start(self):
            pw = await async_playwright().start()
            async def launch(*args, **kwargs):
                assert kwargs['headless'] is True
                ctx = await pw.chromium.launch_persistent_context(*args, **kwargs)
                await ctx.route('https://muse.ai/**',lambda route:route.fulfill(content_type='text/html',body='<textarea></textarea>'))
                return ctx
            return SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=launch), stop=pw.stop)
    monkeypatch.setattr(muse,'async_playwright',Factory)
    config={'url':'https://muse.ai/','input_selector':'textarea'}
    session = tmp_path / 'session.json'
    session.write_text(json.dumps(stored('original')))
    async def run():
        async with muse.Muse(config,tmp_path) as m:
            assert await m.page.evaluate('localStorage.getItem("local")') == 'original'
            assert await m.page.evaluate('sessionStorage.getItem("tab")') == 'original'
            await m.context.add_cookies([{'name':'auth','value':'refreshed','domain':'.muse.ai','path':'/','secure':True}])
        async with muse.Muse(config,tmp_path) as m:
            assert next(c for c in await m.context.cookies() if c['name']=='auth')['value'] == 'refreshed'
        session.write_text(json.dumps(stored('new-account')))
        async with muse.Muse(config,tmp_path) as m:
            assert next(c for c in await m.context.cookies() if c['name']=='auth')['value'] == 'new-account'
            assert await m.page.evaluate('localStorage.getItem("local")') == 'new-account'
    asyncio.run(run())


def test_last_frame_matches_decoded_last_video_frame(tmp_path):
    from media import last_frame
    clip=tmp_path/'moving.mp4'
    subprocess.run(['ffmpeg','-y','-v','error','-f','lavfi','-i','testsrc2=size=80x120:rate=15',
                    '-t','0.4','-c:v','libx264','-pix_fmt','yuv420p',str(clip)],check=True)
    target=last_frame(clip,tmp_path/'reference.png')
    video=subprocess.check_output(['ffmpeg','-v','error','-i',str(clip),'-f','rawvideo','-pix_fmt','rgb24','-'])
    image=subprocess.check_output(['ffmpeg','-v','error','-i',str(target),'-f','rawvideo','-pix_fmt','rgb24','-'])
    assert image == video[-80*120*3:]


def test_reference_upload_before_prompt_submission(tmp_path):
    import asyncio
    from playwright.async_api import async_playwright
    from muse import Muse
    from media import last_frame
    clip=tmp_path/'sample.mp4'
    subprocess.run(['ffmpeg','-y','-v','error','-f','lavfi','-i','color=c=green:s=80x120:r=15',
                    '-t','0.4','-c:v','libx264','-pix_fmt','yuv420p',str(clip)],check=True)
    reference=last_frame(clip,tmp_path/'reference.png')
    async def run():
        async with async_playwright() as pw:
            browser=await pw.chromium.launch(headless=True)
            context=await browser.new_context()
            page=await context.new_page()
            markup='''<input type="file"><textarea></textarea><button aria-label="Send" disabled>Send</button>
            <script>
            document.querySelector('input').addEventListener('change',()=>{
              document.querySelector('button').disabled=false;
            });
            document.querySelector('textarea').addEventListener('keydown',event=>{
              if(event.key==='Enter'){window.sent={text:event.target.value,name:document.querySelector('input').files[0]?.name};history.replaceState(null,'','/thread/fixture-result');}
            });
            </script>'''
            await context.route('https://muse.ai/**',lambda route:route.fulfill(content_type='text/html',body=markup))
            await page.goto('https://muse.ai/thread/fixture')
            m=Muse({'input_selector':'textarea','video_selector':'video','timeout_seconds':30},tmp_path)
            m.context,m.page=context,page
            try:
                await m.generate('Continue from attached frame',tmp_path/'unused.mp4',lambda url:None,lambda url:None,
                                 lambda:True,reference=reference)
            except Exception as error:
                from muse import NeedsAttention
                assert isinstance(error,NeedsAttention)
            assert page.url == 'https://muse.ai/thread/fixture-result'
            assert len(context.pages)==1
            assert await page.evaluate('window.sent') == {'text':'Continue from attached frame','name':'reference.png'}
            await browser.close()
    asyncio.run(run())


def test_continuity_gate_stops_before_next_generation(client, tmp_path, monkeypatch):
    import asyncio
    import shutil
    c, app = client
    p=c.post('/api/projects',headers={'X-Muse-Local':'1'},json={
        'title':'Continuity gate','script':'Start\n\nNext\n\nFinal','continuity':True}).json()
    inputs=[]
    for i,color in enumerate(['red','blue']):
        f=tmp_path/f'{color}.mp4'
        subprocess.run(['ffmpeg','-y','-v','error','-f','lavfi','-i',f'color=c={color}:s=80x120:r=15',
                        '-t','0.4','-c:v','libx264','-pix_fmt','yuv420p',str(f)],check=True)
        inputs.append(f)
    app.execute("UPDATE scenes SET status='done',path=?,duration=.4,conversation_url='https://muse.ai/thread/fixture' WHERE id=?",
                (str(inputs[0]),p['scenes'][0]['id']))
    calls=[]
    class FakeMuse:
        def __init__(self,*args):pass
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def check(self):return 'https://muse.ai/thread/fixture'
        async def generate(self,prompt,target,submitted,located,stopping,reference=None):
            assert reference and reference.is_file()
            calls.append(prompt)
            submitted('https://muse.ai/thread/fixture')
            shutil.copy2(inputs[1],target)
    monkeypatch.setattr(app,'Muse',FakeMuse)
    asyncio.run(app.worker(p['id']))
    result=c.get('/api/projects').json()[0]
    assert len(calls)==1
    assert result['status']=='attention'
    assert result['scenes'][1]['status']=='review'
    assert result['scenes'][1]['boundary_score']<.85
    assert result['scenes'][2]['status']=='pending'
    assert c.post(f"/api/projects/{p['id']}/start",headers={'X-Muse-Local':'1'}).status_code==409


def test_opening_frame_seed_is_used_before_downstream_scenes(client, tmp_path, monkeypatch):
    import asyncio
    import shutil
    from types import SimpleNamespace
    from media import last_frame
    c,app=client
    p=create(c)
    sample=tmp_path/'opening.mp4'
    subprocess.run(['ffmpeg','-y','-v','error','-f','lavfi','-i','color=c=green:s=80x120:r=15',
                    '-t','0.4','-c:v','libx264','-pix_fmt','yuv420p',str(sample)],check=True)
    opening=last_frame(sample,app.DATA/p['id']/'opening-frame.png')
    calls=[]
    class EmptyLocator:
        async def count(self):return 0
    class FakeMuse:
        def __init__(self,*args):self.page=SimpleNamespace(get_by_role=lambda *a,**k:EmptyLocator())
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def check(self):return 'https://muse.ai/'
        async def generate(self,prompt,target,submitted,located,stopping,reference=None):
            assert reference==opening
            assert prompt.startswith('OPENING FRAME ANIMATION:')
            calls.append(prompt)
            submitted('https://muse.ai/thread/opening-fixture')
            shutil.copy2(sample,target)
    monkeypatch.setattr(app,'Muse',FakeMuse)
    asyncio.run(app.worker(p['id'],max_new_clips=1))
    result=c.get('/api/projects').json()[0]
    assert len(calls)==1
    assert result['status']=='draft'
    assert result['scenes'][0]['status']=='done'
    assert result['scenes'][0]['boundary_score']>.99
    assert result['scenes'][1]['status']=='pending'
