import hashlib
import json
import shutil
import subprocess
import time
from pathlib import Path
import httpx
from media import last_frame, probe
from postprocess import finish_episode

BASE = 'http://127.0.0.1:8765'
DATA = Path('data')
QUEUE = [
    ('beb2fe3dccca45e9b526368d671bc7fc', 'projects/episode-03-the-city-is-watching'),
    ('22e43c43afa94fcfb44491dea9a17482', 'projects/episode-04-the-other-subjects'),
    ('df54a362c22e4497994224b6ddfca736', 'projects/episode-05-the-false-exit'),
    ('61253ac411224dee83ebfa7b02f9622f', 'projects/episode-06-the-creator'),
]
client = httpx.Client(timeout=15)

def get_project(pid):
    return next(p for p in client.get(BASE+'/api/projects').json() if p['id'] == pid)

def wait_done(pid):
    last_signature = None
    while True:
        p = get_project(pid)
        signature = (p['status'], tuple(s['status'] for s in p['scenes']))
        if signature != last_signature:
            print(p['title'], p['status'], p['message'], 'scenes=',signature[1], flush=True)
            last_signature = signature
        if p['status'] == 'attention':
            raise RuntimeError(f"{p['title']} needs attention: {p['message']}")
        if p['status'] == 'done':
            return p
        time.sleep(15)

def postprocess_export(pid, export_dir, project):
    root = DATA / pid
    output = root / 'output.mp4'
    plan = json.loads((root/'postproduction.json').read_text())
    export = Path(export_dir)
    title_idx = int(project['title'].split()[1])
    title_names = {3:'Episode 03 - The City Is Watching.mp4',4:'Episode 04 - The Other Subjects.mp4',5:'Episode 05 - The False Exit.mp4',6:'Episode 06 - The Creator.mp4'}
    target = export / title_names[title_idx]
    if (export/'render-report.json').exists() and target.exists():
        print('ALREADY_EXPORTED',target,flush=True)
        return target
    post = finish_episode(output, plan, root, DATA)
    metadata = json.loads(subprocess.run(['ffprobe','-v','error','-show_format','-show_streams','-of','json',str(output)],check=True,capture_output=True,text=True).stdout)
    video = next(s for s in metadata['streams'] if s['codec_type']=='video')
    duration = float(metadata['format']['duration'])
    scores = [s.get('boundary_score') for s in project['scenes']]
    if len(project['scenes']) != 6 or any(s['status'] != 'done' for s in project['scenes']):
        raise RuntimeError('Không đủ 6 cảnh đã render thành công')
    if duration < 59.9 or duration > 60.1 or (video['width'],video['height']) != (1080,1920):
        raise RuntimeError(f'Sai thời lượng/tỷ lệ: {duration}s, {video["width"]}x{video["height"]}')
    if any(x is not None and x < .85 for x in scores):
        raise RuntimeError(f'Có điểm nối dưới ngưỡng: {scores}')
    export.mkdir(parents=True,exist_ok=True)
    # Human-readable standard names override the title-derived fallback.
    idx = int(project['title'].split()[1])
    title_names = {3:'Episode 03 - The City Is Watching.mp4',4:'Episode 04 - The Other Subjects.mp4',5:'Episode 05 - The False Exit.mp4',6:'Episode 06 - The Creator.mp4'}
    target = export / title_names[idx]
    shutil.copy2(output,target)
    report={'project_id':pid,'title':project['title'],'duration':duration,'width':video['width'],'height':video['height'],
            'file_size_bytes':target.stat().st_size,'sha256':hashlib.sha256(target.read_bytes()).hexdigest(),
            'boundary_scores':scores,'postproduction':post}
    (export/'render-report.json').write_text(json.dumps(report,indent=2))
    print('EXPORTED',target,report['file_size_bytes'],'bytes; post lines=',len(post['voice_lines']),'screen text=',len(post['screen_text']),flush=True)
    return target

for i,(pid,export_dir) in enumerate(QUEUE):
    if i:
        current = get_project(pid)
        if current['status'] == 'draft':
            previous_pid = QUEUE[i-1][0]
            prev_output = DATA/previous_pid/'output.mp4'
            seed = DATA/pid/'opening-frame.png'
            last_frame(prev_output,seed)
            shutil.copy2(seed,Path(export_dir)/'opening-reference.png')
            for attempt in range(12):
                response=client.post(f'{BASE}/api/projects/{pid}/start',headers={'X-Muse-Local':'1'})
                if response.status_code==200:
                    print('STARTED',get_project(pid)['title'],'from previous episode final frame',flush=True)
                    break
                if response.status_code!=409:
                    raise RuntimeError(f'Cannot start project {pid}: {response.status_code} {response.text[:300]}')
                time.sleep(5)
            else:
                raise RuntimeError('Worker chưa sẵn sàng cho tập tiếp theo')
        else:
            print('RESUMING_MONITOR',current['title'],current['status'],flush=True)
    project=wait_done(pid)
    postprocess_export(pid,export_dir,project)
print('ALL_EPISODES_RENDERED',flush=True)
