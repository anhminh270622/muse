import json
import subprocess
import re
from pathlib import Path


def command(args):
    result = subprocess.run(args, capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(f'{args[0]} thất bại: {result.stderr[-1600:]}')
    return result.stdout


def probe(path):
    result = json.loads(command(['ffprobe', '-v', 'error', '-show_format', '-show_streams', '-of', 'json', str(path)]))
    if not any(s['codec_type'] == 'video' for s in result['streams']):
        raise ValueError('File không chứa video')
    duration = float(result['format'].get('duration', 0))
    if duration <= 0:
        raise ValueError('Video không có thời lượng hợp lệ')
    return duration, any(s['codec_type'] == 'audio' for s in result['streams'])


def last_frame(clip, target):
    """Decode the tail and reverse it to get the actual final displayed frame."""
    probe(clip)
    command(['ffmpeg', '-y', '-v', 'error', '-sseof', '-1', '-i', str(clip),
             '-map', '0:v:0', '-vf', 'reverse', '-frames:v', '1', str(target)])
    if not Path(target).is_file():
        raise RuntimeError('Không lấy được khung hình cuối của cảnh trước')
    return Path(target)


def boundary_similarity(reference, clip):
    result = subprocess.run(['ffmpeg', '-v', 'info', '-i', str(reference), '-i', str(clip),
        '-filter_complex', '[0:v]scale=360:640,setsar=1,format=yuv420p[a];'
        '[1:v]select=eq(n\\,0),scale=360:640,setsar=1,format=yuv420p[b];[a][b]ssim',
        '-frames:v', '1', '-f', 'null', '-'], capture_output=True, text=True)
    scores = re.findall(r'All:([0-9.]+)', result.stderr)
    if result.returncode or not scores:
        raise RuntimeError('Không kiểm tra được khung hình nối cảnh')
    return float(scores[-1])


def bridge_first_frames(reference, clip, output, frame_count=6):
    """Blend the first 0.2s of a visually aligned clip from the exact previous frame."""
    if not 1 <= frame_count <= 12:
        raise ValueError('Khung nối phải từ 1 đến 12 frame')
    info = json.loads(command(['ffprobe', '-v', 'error', '-select_streams', 'v:0',
                               '-show_entries', 'stream=width,height', '-of', 'json', str(clip)]))
    stream = info['streams'][0]
    width, height = stream['width'], stream['height']
    duration, _ = probe(clip)
    end = frame_count - 1
    graph = (
        f'[0:v]scale={width}:{height},setsar=1,fps=30,trim=end_frame={frame_count},setpts=PTS-STARTPTS[ref];'
        f'[1:v]scale={width}:{height},setsar=1,fps=30,split=2[src0][src1];'
        f'[src0]trim=end_frame={frame_count},setpts=PTS-STARTPTS[src];'
        f"[ref][src]blend=all_expr='A*(1-N/{end})+B*(N/{end})'[bridge];"
        f'[src1]trim=start_frame={frame_count},setpts=PTS-STARTPTS[rest];'
        '[bridge][rest]concat=n=2:v=1:a=0[outv]'
    )
    target = Path(output)
    temporary = target.with_suffix('.partial.mp4')
    command(['ffmpeg', '-y', '-v', 'error', '-loop', '1', '-framerate', '30', '-i', str(reference),
             '-i', str(clip), '-filter_complex', graph, '-map', '[outv]', '-map', '1:a?',
             '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '20', '-pix_fmt', 'yuv420p',
             '-c:a', 'copy', '-t', str(duration), '-movflags', '+faststart', str(temporary)])
    probe(temporary)
    temporary.replace(target)
    return target


def merge(clips, directory, width=1080, height=1920):
    directory = Path(directory)
    normalized = []
    total_duration = 0.0
    for i, clip in enumerate(clips):
        duration, audio = probe(clip)
        total_duration += duration
        target = directory / f'normalized-{i:03}.mp4'
        args = ['ffmpeg', '-y', '-v', 'error', '-i', str(clip)]
        if not audio:
            args += ['-f', 'lavfi', '-i', 'anullsrc=r=48000:cl=stereo']
        args += ['-map', '0:v:0', '-map', '0:a:0' if audio else '1:a:0',
                 '-vf', f'scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30',
                 '-af', 'aresample=48000,apad', '-t', str(duration), '-c:v', 'libx264', '-preset', 'fast',
                 '-crf', '20', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-ar', '48000', '-ac', '2', str(target)]
        command(args)
        normalized.append(target)
    listing = directory / 'concat.txt'
    listing.write_text(''.join(f"file '{p.name}'\n" for p in normalized))
    temporary = directory / 'output.partial.mp4'
    command(['ffmpeg', '-y', '-v', 'error', '-copyts', '-f', 'concat', '-safe', '0', '-i', str(listing),
             '-c:v', 'copy', '-af', 'asetpts=PTS-STARTPTS', '-c:a', 'aac',
             '-t', str(total_duration), '-movflags', '+faststart', str(temporary)])
    probe(temporary)
    output = directory / 'output.mp4'
    temporary.replace(output)
    for path in normalized:
        path.unlink(missing_ok=True)
    return output
