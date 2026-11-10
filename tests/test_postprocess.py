import json
import math
import subprocess
import sys
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from postprocess import finish_episode, metadata


def test_postproduction_inserts_real_flash_voice_and_silence_without_time_drift(tmp_path):
    source_id='a'*32
    source=tmp_path/source_id
    source.mkdir()
    scene=source/'scene-001.mp4'
    subprocess.run(['ffmpeg','-y','-v','error','-f','lavfi','-i','color=c=blue:s=90x160:r=30',
                    '-t','1','-c:v','libx264','-pix_fmt','yuv420p',str(scene)],check=True)
    directory=tmp_path/('b'*32)
    directory.mkdir()
    output=directory/'output.mp4'
    subprocess.run(['ffmpeg','-y','-v','error','-f','lavfi','-i','color=c=red:s=90x160:r=30',
                    '-f','lavfi','-i','sine=frequency=440:sample_rate=48000','-t','6',
                    '-c:v','libx264','-pix_fmt','yuv420p','-c:a','aac',str(output)],check=True)
    plan={'memory_flashes':[{'source_project_id':source_id,'source_scene':1,'source_at':.1,'at':1.0,'duration':4/30}],
          'system_voice':{'at':1.8,'spoken':'Subject zero four seven has retained memory.','display_text':None},
          'silence':{'from':5.8,'until':6.0}}
    report=finish_episode(output,plan,directory,tmp_path)
    assert report['memory_flash_count']==1
    assert report['on_screen_system_text'] is False
    assert report['subtitles'] is False
    assert (directory/'system-voice.txt').read_text()=='Subject zero four seven has retained memory.'
    assert 2<report['system_voice_duration']<4
    assert abs(float(metadata(output)['format']['duration'])-6)<.001
    def color(t):
        raw=subprocess.check_output(['ffmpeg','-v','error','-ss',str(t),'-i',str(output),'-frames:v','1',
                                     '-f','rawvideo','-pix_fmt','rgb24','-'])
        return tuple(raw[:3])
    assert color(.8)[0]>200 and color(.8)[2]<30
    assert color(1.05)[2]>150 and color(1.05)[2]-color(1.05)[0]>80
    assert color(1.3)[0]>200 and color(1.3)[2]<30
    quiet=subprocess.check_output(['ffmpeg','-v','error','-ss','5.9','-t','0.08','-i',str(output),
                                   '-vn','-ar','48000','-ac','1','-f','s16le','-'])
    import array
    samples=array.array('h',quiet)
    rms=math.sqrt(sum(x*x for x in samples)/len(samples))/32768
    assert rms<.001
