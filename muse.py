"""Headless-only adapter. Selectors are configurable until verified against a live account."""
import asyncio
import json
import hashlib
import os
import time
from urllib.parse import urlparse

from playwright.async_api import async_playwright
from media import probe


class NeedsAttention(Exception):
    pass


def validate_url(url):
    parsed = urlparse(url)
    if parsed.scheme != 'https' or parsed.hostname not in {'muse.ai', 'www.muse.ai'} or parsed.username:
        raise ValueError('Chỉ chấp nhận URL https://muse.ai/...')
    return url


def validate_session(value):
    if isinstance(value, list):
        value = {'cookies': value, 'origins': []}
    if not isinstance(value, dict) or not isinstance(value.get('cookies'), list):
        raise ValueError('Cần JSON cookies hoặc Playwright storageState')
    cookies = []
    for original in value['cookies']:
        if not isinstance(original, dict):
            raise ValueError('Cookie không hợp lệ')
        domain = str(original.get('domain', '')).lstrip('.')
        if not any(domain == root or domain.endswith('.' + root) for root in ('muse.ai', 'meta.ai', 'facebook.com', 'meta.com')):
            continue
        cookie = {k: v for k, v in original.items() if k in {'name','value','domain','path','expires','httpOnly','secure','sameSite'}}
        if not all(isinstance(cookie.get(k), str) for k in ('name','value','domain')):
            raise ValueError('Cookie thiếu name/value/domain')
        cookie.setdefault('path', '/')
        if cookie.get('sameSite') not in ('Strict', 'Lax', 'None'):
            cookie['sameSite'] = 'Lax'
        cookies.append(cookie)
    origins = []
    if not isinstance(value.get('origins', []), list):
        raise ValueError('Origins không hợp lệ')
    for original in value.get('origins', []):
        if not isinstance(original, dict):
            raise ValueError('Origin không hợp lệ')
        if original.get('origin') not in ('https://muse.ai', 'https://www.muse.ai'):
            continue
        origin = {'origin': original['origin']}
        for storage in ('localStorage', 'sessionStorage'):
            entries = original.get(storage, [])
            if not isinstance(entries, list) or any(not isinstance(e, dict) or
                not isinstance(e.get('name'), str) or not isinstance(e.get('value'), str) for e in entries):
                raise ValueError('Storage không hợp lệ')
            origin[storage] = [{'name': e['name'], 'value': e['value']} for e in entries]
        if 'indexedDB' in original:
            origin['indexedDB'] = original['indexedDB']
        origins.append(origin)
    if not cookies and not origins:
        raise ValueError('File không chứa phiên Muse hợp lệ')
    return {'cookies': cookies, 'origins': origins}


class Muse:
    def __init__(self, config, data):
        self.config, self.data = config, data

    async def __aenter__(self):
        self.pw = await async_playwright().start()
        try:
            self.context = await self.pw.chromium.launch_persistent_context(
                str(self.data / 'browser-profile'), headless=True, accept_downloads=True,
                viewport={'width': 1280, 'height': 900})
            session = self.data / 'session.json'
            if session.exists():
                content = session.read_bytes()
                state = validate_session(json.loads(content))
                self.source_fingerprint = hashlib.sha256(content).hexdigest()
                saved = self.data / 'runtime-session.json'
                if saved.exists():
                    try:
                        previous = json.loads(saved.read_text())
                        if previous.get('source') == self.source_fingerprint:
                            state = validate_session(previous['state'])
                    except (ValueError, KeyError, TypeError):
                        pass
                self.imported_state = state
                # Session cookies do not reliably survive closing Chromium. Restore the latest
                # scoped snapshot, rather than overwriting refreshed cookies with an old import.
                await self.context.set_storage_state({
                    'cookies': state['cookies'],
                    'origins': [{k: v for k, v in origin.items() if k != 'sessionStorage'} for origin in state['origins']]
                })
                for origin in state.get('origins', []):
                    payload = json.dumps({'origin': origin['origin'], 'entries': origin.get('sessionStorage', [])})
                    await self.context.add_init_script(f'''(() => {{ const s = {payload};
                      if (location.origin === s.origin) for (const x of s.entries) sessionStorage.setItem(x.name, x.value);
                    }})();''')
            self.page = await self.context.new_page()
            await self.page.goto(validate_url(self.config['url']), wait_until='domcontentloaded', timeout=60000)
            await self.page.wait_for_timeout(2000)
            return self
        except BaseException:
            if hasattr(self, 'context'):
                await self.context.close()
            await self.pw.stop()
            raise

    async def __aexit__(self, *args):
        try:
            if hasattr(self, 'source_fingerprint'):
                state = validate_session(await self.context.storage_state(indexed_db=True))
                tabs = {o['origin']: o.get('sessionStorage', []) for o in self.imported_state['origins']}
                for page in self.context.pages:
                    if urlparse(page.url).hostname in {'muse.ai', 'www.muse.ai'}:
                        try:
                            tab = await page.evaluate('''() => ({origin:location.origin,
                                entries:Object.keys(sessionStorage).map(name => ({name,value:sessionStorage.getItem(name)}))})''')
                            tabs[tab['origin']] = tab['entries']
                        except Exception:
                            pass
                for origin in state['origins']:
                    origin['sessionStorage'] = tabs.get(origin['origin'], [])
                temporary = self.data / 'runtime-session.tmp'
                fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, 'w') as stream:
                    json.dump({'source': self.source_fingerprint, 'state': state}, stream)
                temporary.replace(self.data / 'runtime-session.json')
        finally:
            try:
                await self.context.close()
            finally:
                await self.pw.stop()

    async def check(self):
        try:
            await self.page.locator(self.config['input_selector']).first.wait_for(state='visible', timeout=15000)
        except Exception:
            raise NeedsAttention('Chưa thấy ô nhập Muse. Cần nhập phiên đăng nhập hợp lệ hoặc chỉnh selector. Không mở cửa sổ.') from None
        return self.page.url

    async def generate(self, prompt, target, on_submit, on_url, should_stop, reference=None):
        await self.check()
        if reference:
            # Isolate each image continuation: earlier versions in a long thread can cause
            # the agent to extend an old video instead of the newly attached frame.
            await self.page.goto('https://muse.ai/thread/new', wait_until='domcontentloaded')
            await self.check()
            await self.page.wait_for_timeout(1500)
            remove = self.page.locator('button[aria-label="Remove attachment"]')
            while await remove.count():
                await remove.first.click()
            await self.page.locator('input[type="file"]').first.set_input_files(str(reference))
            await self.page.wait_for_timeout(1500)
        selector = self.config['video_selector']
        # Wait for a new video element, not a previously generated clip.
        await self.page.locator(selector).evaluate_all('''xs => xs.forEach(x => {
          x.setAttribute('data-muse-old', '1');
          x.closest('[data-testid="hatch-chat-attachment-presentation-video"]')?.setAttribute('data-muse-old','1');
        })''')
        await self.page.get_by_test_id('hatch-chat-attachment-presentation-video').evaluate_all("xs=>xs.forEach(x=>x.setAttribute('data-muse-old','1'))")
        field = self.page.locator(self.config['input_selector']).first
        await field.fill(prompt)
        if reference:
            await self.page.wait_for_function('''() => {
                const b = document.querySelector('button[aria-label="Send"]');
                return b && !b.disabled && b.getAttribute('aria-disabled') !== 'true';
            }''', timeout=60000)
        # Persist BEFORE submission. A crash here must require reconciliation, never blind retry.
        on_submit(self.page.url)
        if self.config.get('send_selector'):
            await self.page.locator(self.config['send_selector']).first.click()
        else:
            await field.press('Enter')
        if '/thread/new' in self.page.url:
            await self.page.wait_for_url(lambda url: '/thread/' in url and '/thread/new' not in url, timeout=60000)
            on_url(self.page.url)
        deadline = time.monotonic() + self.config['timeout_seconds']
        next_capture = 0
        while time.monotonic() < deadline:
            if should_stop():
                raise NeedsAttention('Đã dừng chờ. Muse có thể vẫn đang tạo clip; đối chiếu trước khi gửi lại.')
            if time.monotonic() > next_capture:
                await self.page.screenshot(path=str(self.data / 'muse-render-progress.png'))
                next_capture = time.monotonic() + 30
            candidates = await self.page.locator(selector).evaluate_all('''xs => xs.filter(x =>
              !x.hasAttribute('data-muse-old') &&
              !x.closest('[data-testid="hatch-chat-attachment-presentation-video"][data-muse-old]') &&
              !x.closest('[role="img"], [class*="status-avatar"]') &&
              x.videoWidth > 0 && x.videoHeight > 0
            ).map(x => x.currentSrc || x.src || x.querySelector('source')?.src).filter(Boolean)''')
            for src in candidates:
                if not src.startswith(('https://', 'blob:')):
                    continue
                if await self.download_media(src, target):
                    (self.data / 'adapter-verified.json').write_text(json.dumps({
                        'input_selector': self.config['input_selector'], 'video_selector': selector
                    }))
                    on_url(self.page.url)
                    return
            await asyncio.sleep(4)
        raise NeedsAttention('Hết thời gian chờ. Kiểm tra clip trong hội thoại Muse, nhập clip nếu đã xong. Tool không tự gửi lại.')

    async def download_media(self, src, target):
        temporary = target.with_suffix('.partial.mp4')
        if src.startswith('blob:'):
            async with self.page.expect_download(timeout=30000) as pending:
                await self.page.evaluate('''src => { const a=document.createElement('a');
                    a.href=src; a.download='clip.mp4'; document.body.appendChild(a); a.click(); a.remove(); }''', src)
            await (await pending.value).save_as(str(temporary))
        elif src.startswith('https://'):
            response = await self.context.request.get(src, timeout=120000)
            if not response.ok:
                return False
            temporary.write_bytes(await response.body())
        else:
            return False
        try:
            await asyncio.to_thread(probe, temporary)
        except Exception:
            temporary.unlink(missing_ok=True)
            return False
        temporary.replace(target)
        return True
