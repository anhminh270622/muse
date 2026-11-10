"""Optional deterministic episode finishing: real memory inserts and local system voice.

The Muse clips stay untouched. A project may supply data/<id>/postproduction.json.
No credentials or network services are required for this stage.
"""
import json
import math
import re
import shutil
import subprocess
from pathlib import Path

from media import command, probe


def number(value, name, lower=0):
    result = float(value)
    if not math.isfinite(result) or result < lower:
        raise ValueError(f'{name} không hợp lệ')
    return result


def metadata(path):
    return json.loads(command(['ffprobe', '-v', 'error', '-show_format', '-show_streams', '-of', 'json', str(path)]))


def system_voice(text, directory, name='system-voice', voice='rms'):
    if not isinstance(text, str) or not text.strip() or len(text) > 240:
        raise ValueError('Lời hệ thống phải dài 1–240 ký tự')
    if voice not in {'rms', 'slt'}:
        raise ValueError('Giọng flite không được hỗ trợ')
    directory = Path(directory)
    script = directory / f'{name}.txt'
    script.write_text(text)
    raw = directory / f'{name}-raw.wav'
    # Text is read from a file, never interpolated as a lavfi expression.
    escaped = str(script.resolve()).replace('\\', '\\\\').replace(':', '\\:').replace("'", "'\\''")
    command(['ffmpeg', '-y', '-v', 'error', '-f', 'lavfi', '-i', f"flite=voice={voice}:textfile='{escaped}'", str(raw)])
    info = metadata(raw)
    sample_rate = int(next(s for s in info['streams'] if s['codec_type'] == 'audio')['sample_rate'])
    target = directory / f'{name}.wav'
    command(['ffmpeg', '-y', '-v', 'error', '-i', str(raw), '-af',
             f'asetrate={sample_rate}*0.88,aresample=48000,atempo=1.1363636,'
             'highpass=f=100,lowpass=f=3600,aecho=0.8:0.65:90|180:0.14|0.07,'
             'loudnorm=I=-18:TP=-2:LRA=5', '-ar', '48000', '-ac', '2', str(target)])
    return target, float(metadata(target)['format']['duration'])


def finish_episode(output, plan, directory, data_root):
    output, directory, data_root = Path(output), Path(directory), Path(data_root)
    duration, audio = probe(output)
    if not audio:
        raise ValueError('Bản dựng nền cần có audio trước khi hậu kỳ')
    info = metadata(output)
    video = next(s for s in info['streams'] if s['codec_type'] == 'video')
    width, height = video['width'], video['height']
    flashes = plan.get('memory_flashes', [])
    if not isinstance(flashes, list) or len(flashes) > 12:
        raise ValueError('Tối đa 12 chớp hồi ức')
    prepared = []
    for flash in flashes:
        source_id = flash.get('source_project_id', '')
        if not re.fullmatch(r'[a-f0-9]{32}', source_id):
            raise ValueError('Project nguồn hồi ức không hợp lệ')
        scene = int(flash['source_scene'])
        if not 1 <= scene <= 30:
            raise ValueError('Cảnh nguồn hồi ức không hợp lệ')
        source = data_root / source_id / f'scene-{scene:03}.mp4'
        source_duration, _ = probe(source)
        at = number(flash['at'], 'Thời điểm hồi ức')
        length = number(flash['duration'], 'Độ dài hồi ức', 0.03)
        source_at = number(flash['source_at'], 'Thời điểm video nguồn')
        if length > .5 or at+length > duration or source_at+length > source_duration:
            raise ValueError('Hồi ức vượt ngoài thời lượng video')
        prepared.append((source, at, length, source_at))
    voice_lines = list(plan.get('voice_lines', []))
    legacy_voice = plan.get('system_voice')
    if legacy_voice:
        if legacy_voice.get('display_text') is not None:
            raise ValueError('Không chèn thông báo hệ thống bằng chữ trong chế độ này')
        voice_lines.append({'at': legacy_voice['at'], 'spoken': legacy_voice['spoken'], 'voice': 'rms'})
    if len(voice_lines) > 20:
        raise ValueError('Tối đa 20 câu thoại cho một tập')
    rendered_voices = []
    for i, line in enumerate(voice_lines):
        voice_at = number(line.get('at'), 'Thời điểm thoại')
        voice_file, voice_length = system_voice(line.get('spoken'), directory,
                                                 name=f'voice-{i+1:02}', voice=line.get('voice', 'rms'))
        if voice_at+voice_length > duration:
            raise ValueError('Lời thoại vượt ngoài video')
        rendered_voices.append((line, voice_file, voice_length, voice_at))
    silence = plan.get('silence')
    if silence:
        silent_from = number(silence['from'], 'Bắt đầu im lặng')
        silent_until = number(silence['until'], 'Kết thúc im lặng')
        if not silent_from < silent_until <= duration:
            raise ValueError('Khoảng im lặng không hợp lệ')
        if any(at < silent_until and at+length > silent_from for _, _, length, at in rendered_voices):
            raise ValueError('Khoảng im lặng đang cắt lời hệ thống')
    base = directory / 'output-base.mp4'
    shutil.copy2(output, base)
    args = ['ffmpeg', '-y', '-v', 'error', '-i', str(base)]
    next_input = 1
    voice_inputs = []
    for _, voice_file, _, _ in rendered_voices:
        voice_inputs.append(next_input)
        args += ['-i', str(voice_file)]
        next_input += 1
    flash_inputs = []
    for source, at, length, source_at in prepared:
        args += ['-ss', str(source_at), '-t', str(length), '-i', str(source)]
        flash_inputs.append(next_input)
        next_input += 1
    glitch_input = None
    if prepared:
        args += ['-f', 'lavfi', '-i', 'anoisesrc=color=white:amplitude=0.10:r=48000:d=0.06']
        glitch_input = next_input
    filters = ['[0:v]setpts=PTS-STARTPTS[v0]']
    current = 'v0'
    for i, ((_, at, length, _), index) in enumerate(zip(prepared, flash_inputs), 1):
        count = max(1, round(length*30))
        filters += [f'[{index}:v]scale={width}:{height}:force_original_aspect_ratio=increase,'
                    f'crop={width}:{height},setsar=1,fps=30,trim=end_frame={count},'
                    f'eq=brightness=0.04:contrast=1.08:saturation=0.7,setpts=PTS-STARTPTS+{at}/TB[f{i}]',
                    f'[{current}][f{i}]overlay=eof_action=pass:repeatlast=0:'
                    f"enable='gte(t,{at})*lt(t,{at+count/30})'[v{i}]"]
        current = f'v{i}'
    screen_text = plan.get('screen_text', [])
    if not isinstance(screen_text, list) or len(screen_text) > 20:
        raise ValueError('Tối đa 20 chi tiết chữ trên màn hình')
    font = Path('/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf')
    for i, item in enumerate(screen_text, 1):
        text = item.get('text', '')
        if not isinstance(text, str) or not text.strip() or len(text) > 100:
            raise ValueError('Chữ màn hình cần dài 1–100 ký tự')
        at = number(item.get('at'), 'Thời điểm chữ')
        length = number(item.get('duration', 3), 'Thời lượng chữ', 0.1)
        if at+length > duration:
            raise ValueError('Chữ màn hình vượt ngoài video')
        x = str(item.get('x', '(w-text_w)/2'))
        y = str(item.get('y', 330))
        color = str(item.get('color', '0x8df7ff'))
        if not re.fullmatch(r'(?:\(w-text_w\)/2|\d{1,4})', x) or not re.fullmatch(r'\d{1,4}', y):
            raise ValueError('Vị trí chữ không hợp lệ')
        if not re.fullmatch(r'(?:white|yellow|0x[0-9A-Fa-f]{6})', color):
            raise ValueError('Màu chữ không hợp lệ')
        text_path = directory / f'screen-text-{i:02}.txt'
        text_path.write_text(text)
        escaped_path = str(text_path.resolve()).replace('\\', '\\\\').replace(':', '\\:').replace("'", "'\\''")
        font_part = f"fontfile='{font.as_posix()}':" if font.is_file() else ''
        filters.append(f"[{current}]drawtext={font_part}textfile='{escaped_path}':fontcolor={color}:"
                       f"fontsize=48:borderw=3:bordercolor=black@0.85:x={x}:y={y}:"
                       f"enable='gte(t,{at})*lt(t,{at+length})'[txt{i}]")
        current = f'txt{i}'
    base_audio = f'[0:a]atrim=duration={duration},asetpts=PTS-STARTPTS,aresample=48000'
    if rendered_voices:
        duck = '+'.join(f'between(t,{at},{at+length})' for _, _, length, at in rendered_voices)
        base_audio += f",volume='if({duck},0.16,1)':eval=frame"
    if silence:
        base_audio += f",volume='if(between(t,{silent_from},{silent_until}),0,1)':eval=frame"
    filters.append(base_audio+'[ambience]')
    mix = ['ambience']
    for i, ((_, _, _, at), voice_input) in enumerate(zip(rendered_voices, voice_inputs), 1):
        delay = round(at*1000)
        filters.append(f'[{voice_input}:a]aresample=48000,asetpts=PTS-STARTPTS,adelay={delay}|{delay}[voice{i}]')
        mix.append(f'voice{i}')
    if prepared:
        filters.append(f'[{glitch_input}:a]highpass=f=600,lowpass=f=4500,'
                       f'afade=t=out:st=0.01:d=0.05,asplit={len(prepared)}'+''.join(f'[g{i}]' for i in range(len(prepared))))
        for i, (_, at, _, _) in enumerate(prepared):
            delay = round(at*1000)
            filters.append(f'[g{i}]adelay={delay}|{delay}[glitch{i}]')
            mix.append(f'glitch{i}')
    filters.append(''.join(f'[{name}]' for name in mix)+f'amix=inputs={len(mix)}:duration=first:normalize=0,'
                   'alimiter=limit=0.95:level=0:latency=1[mix]')
    target = directory / 'output-post.partial.mp4'
    args += ['-filter_complex', ';'.join(filters), '-map', f'[{current}]', '-map', '[mix]',
             '-c:v', 'libx264', '-threads', '2', '-preset', 'fast', '-crf', '20', '-pix_fmt', 'yuv420p',
             '-c:a', 'aac', '-ar', '48000', '-ac', '2', '-t', str(duration), '-movflags', '+faststart', str(target)]
    command(args)
    finished_duration, _ = probe(target)
    if abs(finished_duration-duration) > .01:
        raise RuntimeError('Hậu kỳ làm thay đổi thời lượng video')
    target.replace(output)
    report = {'memory_flash_count':len(prepared), 'memory_sources':flashes,
              'voice_lines':[{'at':at,'spoken':line['spoken'],'voice':line.get('voice','rms'),'duration':length}
                             for line, _, length, at in rendered_voices],
              'screen_text':screen_text, 'silence':silence, 'subtitles':False,
              'duration':finished_duration, 'voice_engine':'local libflite rms/slt, no API key'}
    (directory/'postproduction-report.json').write_text(json.dumps(report,indent=2))
    return report
