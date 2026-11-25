#!/usr/bin/env python3
"""Synthesize timeline scenes with MiniMax and derive audio-driven scene timing."""
import argparse
import binascii
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time

import requests


DEFAULT_VOICE_ID = ''  # 在设置页或 MINIMAX_VOICE_ID 环境变量中配置你的音色 ID


def probe_duration(path):
    result = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
                             '-of', 'default=noprint_wrappers=1:nokey=1', str(path)],
                            check=True, capture_output=True, text=True)
    return float(result.stdout.strip())


def decode(path):
    subprocess.run(['ffmpeg', '-v', 'error', '-i', str(path), '-f', 'null', '-'], check=True)


def persist_with_decode(source, target):
    error = None
    for attempt in range(6):
        try:
            shutil.copyfile(source, target)
            decode(target)
            return
        except (OSError, subprocess.CalledProcessError) as exc:
            error = exc
            time.sleep(0.5 * (attempt + 1))
    raise error


def request_audio(text, args):
    key = os.environ.get('MINIMAX_API_KEY')
    if not key:
        raise RuntimeError('Set MINIMAX_API_KEY in the environment; never write it into the project.')
    payload = {
        'model': args.model, 'text': text, 'stream': False, 'language_boost': 'Chinese',
        'voice_setting': {'voice_id': args.voice_id, 'speed': args.speed, 'vol': 1.0,
                          'pitch': 0, 'emotion': 'fluent'},
        'audio_setting': {'sample_rate': 44100, 'bitrate': 256000,
                          'format': 'mp3', 'channel': 1},
    }
    last_error = None
    for attempt in range(4):
        try:
            response = requests.post(args.base_url.rstrip('/') + '/t2a_v2',
                                     headers={'Authorization': 'Bearer ' + key,
                                              'Content-Type': 'application/json'},
                                     json=payload, timeout=(20, 240))
            response.raise_for_status()
            data = response.json()
            if data.get('base_resp', {}).get('status_code') != 0:
                raise RuntimeError(data.get('base_resp'))
            audio_hex = data.get('data', {}).get('audio', '')
            if not audio_hex:
                raise RuntimeError('MiniMax returned empty audio')
            return binascii.unhexlify(audio_hex), response.headers.get('x-request-id')
        except Exception as exc:
            last_error = exc
            if attempt < 3:
                time.sleep(2 * (attempt + 1))
    raise last_error


def process_audio(raw, target, no_process):
    if no_process:
        filters = 'aresample=48000'
    else:
        filters = ('afftdn=nr=6:nf=-50:track_noise=on:residual_floor=-55,'
                   'highpass=f=65,equalizer=f=800:width_type=q:w=1.2:g=-2,'
                   'loudnorm=I=-18:TP=-1.5:LRA=11,aresample=48000')
    subprocess.run(['ffmpeg', '-y', '-v', 'error', '-i', str(raw), '-af', filters,
                    '-ar', '48000', '-ac', '1', '-c:a', 'pcm_s16le', str(target)], check=True)
    decode(target)


def signature(scene, args):
    text = scene.get('tts') or scene.get('narration') or ''
    value = '|'.join([text, args.voice_id, str(args.speed), args.model,
                      str(args.audio_offset), str(args.end_hold), str(args.no_process)])
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def build_cues(scene):
    captions = scene.get('captions') or [scene.get('narration', '')]
    weights = [max(4, len(str(c).replace(' ', ''))) for c in captions]
    total = sum(weights)
    cursor = scene['audio_offset']
    cues = []
    for caption, weight in zip(captions, weights):
        end = cursor + scene['audio_duration'] * weight / total
        cues.append({'start': round(cursor, 6), 'end': round(end, 6), 'text': str(caption)})
        cursor = end
    cues[-1]['end'] = round(scene['audio_offset'] + scene['audio_duration'], 6)
    return cues


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', type=Path, required=True)
    parser.add_argument('--voice-id', default=os.environ.get('MINIMAX_VOICE_ID', DEFAULT_VOICE_ID))
    parser.add_argument('--base-url', default=os.environ.get('MINIMAX_BASE_URL', 'https://ai.ctaigw.cn/v1'))
    parser.add_argument('--model', default=os.environ.get('MINIMAX_MODEL', 'minimax-speech-2.8-hd'))
    parser.add_argument('--speed', type=float, default=0.925)
    parser.add_argument('--audio-offset', type=float, default=0.55)
    parser.add_argument('--end-hold', type=float, default=1.2)
    parser.add_argument('--no-process', action='store_true')
    parser.add_argument('--force', action='store_true', help='Ignore matching audio cache and synthesize again.')
    args = parser.parse_args()
    if not args.voice_id:
        parser.error('Provide --voice-id or MINIMAX_VOICE_ID.')
    if args.speed <= 0 or args.audio_offset < 0 or args.end_hold < 0:
        parser.error('Speed must be positive; offsets cannot be negative.')
    timeline_path = args.project / 'timeline.json'
    timeline = json.loads(timeline_path.read_text(encoding='utf-8'))
    output_dir = args.project / 'audio_processed'
    output_dir.mkdir(parents=True, exist_ok=True)
    report_path = args.project / 'audio_report.json'
    report = json.loads(report_path.read_text(encoding='utf-8')) if report_path.exists() else {'scenes': []}
    previous = {str(s['scene']): s for s in report.get('scenes', [])}
    backup = args.project / 'timeline.before_audio.json'
    if not backup.exists():
        shutil.copyfile(timeline_path, backup)
    report.update({'voice_id': args.voice_id, 'speed': args.speed, 'model': args.model, 'scenes': []})
    with tempfile.TemporaryDirectory(prefix='algorithm_video_audio_') as temp_dir:
        temp = Path(temp_dir)
        for scene in timeline['scenes']:
            sid = int(scene['id'])
            text = scene.get('tts') or scene.get('narration') or ''
            if not text.strip():
                raise ValueError(f'Scene {sid} has no tts or narration text.')
            sig = signature(scene, args)
            target = output_dir / f'scene_{sid:02d}.wav'
            cached = previous.get(str(sid), {})
            if target.exists() and not args.force and cached.get('signature') == sig:
                meta = dict(cached)
                meta['reused'] = True
            else:
                audio_bytes, request_id = request_audio(text, args)
                raw = temp / f'scene_{sid:02d}.mp3'
                processed = temp / f'scene_{sid:02d}.wav'
                raw.write_bytes(audio_bytes)
                decode(raw)
                process_audio(raw, processed, args.no_process)
                persist_with_decode(processed, target)
                meta = {'request_id': request_id, 'raw_duration': probe_duration(raw),
                        'audio_bytes': len(audio_bytes), 'reused': False}
            duration = probe_duration(target)
            scene['audio_offset'] = args.audio_offset
            scene['audio_duration'] = round(duration, 6)
            scene['duration'] = round(args.audio_offset + duration + args.end_hold, 6)
            scene['audio_path'] = str(target)
            meta.update({'scene': sid, 'signature': sig, 'processed_duration': duration})
            report['scenes'] = [s for s in report['scenes'] if int(s['scene']) != sid]
            report['scenes'].append(meta)
            report['scenes'].sort(key=lambda s: int(s['scene']))
            report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
            print(f'scene {sid:02d}: audio={duration:.3f}s, scene={scene["duration"]:.3f}s, reused={meta["reused"]}', flush=True)
    start = 0.0
    for scene in timeline['scenes']:
        scene['start'] = round(start, 6)
        scene['subtitle_cues'] = build_cues(scene)
        start += scene['duration']
        scene.pop('frames', None)
        scene.pop('start_frame', None)
    timeline.update({'duration': round(start, 6), 'voice_id': args.voice_id, 'speed': args.speed})
    timeline_path.write_text(json.dumps(timeline, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'total={start:.3f}s')


if __name__ == '__main__':
    main()
