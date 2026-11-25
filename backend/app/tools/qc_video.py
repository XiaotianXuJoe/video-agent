#!/usr/bin/env python3
"""Validate an algorithm video and create start/middle/ending/hold contact sheets."""
import argparse
import json
import math
from pathlib import Path
import re
import subprocess

import numpy as np
from PIL import Image, ImageDraw, ImageFont


def run(command):
    return subprocess.run(command, check=True, capture_output=True, text=True)


def decode_pcm(path, sample_rate=8000, audio_filter=None):
    """把音轨解码成 8k 单声道 s16le 原始 PCM，供逐窗 RMS 计算（不落盘）。"""
    command = ['ffmpeg', '-v', 'error', '-i', str(path), '-map', '0:a:0',
               '-ac', '1', '-ar', str(sample_rate)]
    if audio_filter:
        command += ['-af', audio_filter]
    command += ['-f', 's16le', '-']
    proc = subprocess.run(command, capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError(f'解码音频失败: {proc.stderr.decode(errors="replace")[-300:]}')
    return proc.stdout


def _to_samples(pcm):
    if not pcm:
        return np.zeros(0, dtype=np.float32)
    usable = pcm[:len(pcm) // 2 * 2]
    return np.frombuffer(usable, dtype='<i2').astype(np.float32) / 32768.0


def pcm_rms_db(pcm):
    """整体 RMS（dBFS）；空音频返回 -120。"""
    samples = _to_samples(pcm)
    if samples.size == 0:
        return -120.0
    rms = float(np.sqrt(np.mean(np.square(samples))))
    return round(20 * math.log10(max(rms, 1e-6)), 2)


def window_rms_db(pcm, sample_rate=8000, window_seconds=5.0):
    """逐窗 RMS 列表（dBFS），窗口默认 5 秒——用于判断 BGM 是否覆盖全片。"""
    samples = _to_samples(pcm)
    step = max(1, int(sample_rate * window_seconds))
    out = []
    for start in range(0, samples.size, step):
        chunk = samples[start:start + step]
        if chunk.size == 0:
            break
        rms = float(np.sqrt(np.mean(np.square(chunk))))
        out.append(round(20 * math.log10(max(rms, 1e-6)), 2))
    return out


def bgm_coverage(video, bgm, volume, window_seconds=5.0, margin=12.0):
    """全时段 BGM 存在性：每个窗口的成片 RMS 都不应低于「BGM 衬底 - margin」。

    没有 BGM 的版本在静音窗口会掉到 -120 dB；只要某个窗口塌下去，就说明循环/淡入
    只盖住了前半段（skill 明确要求的失败模式）。
    """
    windows = window_rms_db(decode_pcm(video), 8000, window_seconds)
    reference = pcm_rms_db(decode_pcm(bgm, 8000, audio_filter=f'volume={volume:g}'))
    threshold = round(reference - margin, 2)
    missing = [{'window': i, 'start': round(i * window_seconds, 2), 'rms_db': level}
               for i, level in enumerate(windows) if level < threshold]
    return {'windows': windows, 'reference_rms_db': reference,
            'threshold_db': threshold, 'missing_windows': missing}


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
    parser.add_argument('--bgm', type=Path, help='本次混入的 BGM；给出时做全时段覆盖校验')
    parser.add_argument('--bgm-volume', type=float, default=0.08)
    parser.add_argument('--bgm-note', default='', help='未混入 BGM 时的说明（写进报告）')
    parser.add_argument('--bgm-window', type=float, default=5.0, help='RMS 窗口秒数')
    parser.add_argument('--bgm-margin', type=float, default=12.0, help='允许低于 BGM 衬底的分贝数')
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
    # BGM：有文件就做全时段覆盖校验；没有就只记录「功能已关闭」的说明
    bgm_info = {'expected': bool(args.bgm), 'file': None, 'volume': args.bgm_volume,
                'note': args.bgm_note, 'window_seconds': args.bgm_window,
                'margin_db': args.bgm_margin, 'windows': [],
                'reference_rms_db': None, 'threshold_db': None, 'missing_windows': []}
    bgm_errors = []
    if args.bgm:
        if not args.bgm.exists():
            bgm_info['note'] = f'BGM 文件不存在: {args.bgm}（本次未混入）'
        else:
            bgm_info['file'] = str(args.bgm)
            coverage = bgm_coverage(args.video, args.bgm, args.bgm_volume,
                                    args.bgm_window, args.bgm_margin)
            bgm_info.update(coverage)
            if coverage['missing_windows']:
                bgm_errors.append(
                    f"BGM 未覆盖全片：{len(coverage['missing_windows'])}/{len(coverage['windows'])} "
                    f"个 {args.bgm_window:g}s 窗口低于 {coverage['threshold_db']:.1f} dB（循环失败）")
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
        'true_peak_margin_db': round(-1.0 - float(loudness['input_tp']), 2),
        'long_silence_events': silence_events, 'contact_sheets': contacts, 'bgm': bgm_info,
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
    errors.extend(bgm_errors)
    report['errors'] = errors
    report['passed'] = not errors
    (args.project / 'final_qc.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if errors:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
