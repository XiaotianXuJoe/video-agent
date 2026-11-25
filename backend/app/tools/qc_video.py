#!/usr/bin/env python3
"""Validate an algorithm video and create start/middle/ending/hold contact sheets."""
import argparse
import json
from pathlib import Path
import re
import subprocess

from PIL import Image, ImageDraw, ImageFont


def run(command):
    return subprocess.run(command, check=True, capture_output=True, text=True)


def grab_frame(video, timestamp, path):
    """用 ffmpeg 精确抽帧（输入定位 -ss 自 ffmpeg 2.1 起为精确 seek，兼顾速度）。"""
    subprocess.run(['ffmpeg', '-y', '-v', 'error', '-ss', f'{max(0, timestamp):.3f}',
                    '-i', str(video), '-frames:v', '1', '-q:v', '2', str(path)],
                   check=True, capture_output=True, text=True)
    if not path.exists() or path.stat().st_size == 0:
        raise RuntimeError(f'Empty frame at {timestamp:.3f}s.')


def contact_sheets(video, timeline, project):
    frames_dir = project / 'final_qc_frames'
    frames_dir.mkdir(exist_ok=True)
    captures = {key: [] for key in ('start', 'middle', 'ending', 'hold')}
    for scene in timeline['scenes']:
        base = scene['start']
        audio_start = base + scene['audio_offset']
        audio_duration = scene['audio_duration']
        audio_end = audio_start + audio_duration
        scene_end = base + scene['duration']
        times = {
            'start': audio_start + min(0.3, audio_duration * 0.1),
            'middle': audio_start + audio_duration * 0.52,
            'ending': audio_end - min(0.3, audio_duration * 0.1),
            'hold': min(scene_end - 0.05, audio_end + min(0.55, (scene_end - audio_end) * 0.5)),
        }
        for phase, timestamp in times.items():
            path = frames_dir / f'scene_{int(scene["id"]):02d}_{phase}.jpg'
            grab_frame(video, timestamp, path)
            captures[phase].append((int(scene['id']), timestamp, path))
    font_paths = ['/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc',
                  '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf']
    font = None
    for path in font_paths:
        try:
            font = ImageFont.truetype(path, 23)
            break
        except OSError:
            continue
    if font is None:
        font = ImageFont.load_default(size=23)
    paths = []
    for phase, items in captures.items():
        tw, th, label_h = 768, 432, 40
        rows = (len(items) + 1) // 2
        sheet = Image.new('RGB', (tw * 2, (th + label_h) * rows), (5, 12, 22))
        d = ImageDraw.Draw(sheet)
        for idx, (scene_id, timestamp, path) in enumerate(items):
            im = Image.open(path).convert('RGB').resize((tw, th), Image.Resampling.LANCZOS)
            x, y = (idx % 2) * tw, (idx // 2) * (th + label_h)
            sheet.paste(im, (x, y))
            d.text((x + 14, y + th + 5), f'Scene {scene_id:02d} | {timestamp:.2f}s | {phase}',
                   font=font, fill=(230, 238, 248))
        output = project / f'contact_{phase}.jpg'
        sheet.save(output, quality=92)
        paths.append(str(output))
    return paths


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', type=Path, required=True)
    parser.add_argument('--video', type=Path, required=True)
    parser.add_argument('--tolerance', type=float, default=0.08)
    parser.add_argument('--allow-long-silence', action='store_true')
    parser.add_argument('--skip-contact-sheets', action='store_true')
    args = parser.parse_args()
    timeline = json.loads((args.project / 'timeline.json').read_text(encoding='utf-8'))
    probe = json.loads(run(['ffprobe', '-v', 'error', '-count_frames', '-show_streams',
                            '-show_format', '-of', 'json', str(args.video)]).stdout)
    video_stream = next(s for s in probe['streams'] if s['codec_type'] == 'video')
    audio_stream = next(s for s in probe['streams'] if s['codec_type'] == 'audio')
    decoded = subprocess.run(['ffmpeg', '-v', 'error', '-i', str(args.video), '-f', 'null', '-'],
                             capture_output=True, text=True)
    loud = subprocess.run(['ffmpeg', '-hide_banner', '-nostats', '-i', str(args.video),
                           '-map', '0:a:0', '-af', 'loudnorm=print_format=json', '-f', 'null', '-'],
                          capture_output=True, text=True, check=True)
    matches = re.findall(r'\{\s*"input_i".*?\}', loud.stderr, re.S)
    if not matches:
        raise RuntimeError('Could not parse loudness statistics.')
    loudness = json.loads(matches[-1])
    silence = subprocess.run(['ffmpeg', '-hide_banner', '-nostats', '-i', str(args.video),
                              '-map', '0:a:0', '-af', 'silencedetect=noise=-45dB:d=2.5',
                              '-f', 'null', '-'], capture_output=True, text=True, check=True)
    silence_events = re.findall(r'silence_start:.*', silence.stderr)
    frame_count = int(video_stream.get('nb_read_frames') or video_stream.get('nb_frames'))
    video_duration = float(video_stream.get('duration') or probe['format']['duration'])
    audio_duration = float(audio_stream.get('duration') or probe['format']['duration'])
    contacts = [] if args.skip_contact_sheets else contact_sheets(args.video, timeline, args.project)
    report = {
        'file': str(args.video), 'bytes': args.video.stat().st_size,
        'expected_duration': timeline['duration'], 'duration_format': float(probe['format']['duration']),
        'duration_video': video_duration, 'duration_audio': audio_duration,
        'expected_frames': timeline['frames'], 'frames': frame_count,
        'width': video_stream['width'], 'height': video_stream['height'],
        'fps': video_stream['r_frame_rate'], 'video_codec': video_stream['codec_name'],
        'audio_codec': audio_stream['codec_name'], 'audio_sample_rate': audio_stream['sample_rate'],
        'full_decode_returncode': decoded.returncode, 'full_decode_errors': decoded.stderr,
        'input_i': float(loudness['input_i']), 'input_tp': float(loudness['input_tp']),
        'long_silence_events': silence_events, 'contact_sheets': contacts,
    }
    (args.project / 'final_qc.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    errors = []
    if frame_count != timeline['frames']:
        errors.append(f'frames {frame_count} != {timeline["frames"]}')
    if (video_stream['width'], video_stream['height'], video_stream['r_frame_rate']) != (1920, 1080, '30/1'):
        errors.append('expected 1920x1080 at 30fps')
    if abs(video_duration - timeline['duration']) > args.tolerance:
        errors.append('video duration mismatch')
    if abs(audio_duration - timeline['duration']) > args.tolerance:
        errors.append('audio duration mismatch')
    if decoded.returncode != 0 or decoded.stderr:
        errors.append('full decode failed')
    if float(loudness['input_tp']) > -1.0:
        errors.append('true peak exceeds -1 dBTP')
    if silence_events and not args.allow_long_silence:
        errors.append('unexpected silence >= 2.5s')
    report['errors'] = errors
    report['passed'] = not errors
    (args.project / 'final_qc.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if errors:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
