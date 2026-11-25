#!/usr/bin/env python3
"""Render a timeline with project visuals.py, then mux MiniMax audio into MP4."""
import argparse
from functools import lru_cache
import importlib.util
import json
import multiprocessing as mp
from pathlib import Path
import subprocess

from PIL import Image, ImageDraw, ImageFont

FPS = 30
V = None
SEGMENTS = None
FG = (232, 238, 248)
SCREEN = (4, 10, 20)
FONT_PATHS = ['/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc',
              '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf']


@lru_cache(maxsize=16)
def subtitle_font(size):
    for path in FONT_PATHS:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)


def load_visuals(path):
    spec = importlib.util.spec_from_file_location('algorithm_video_visuals', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not hasattr(module, 'render_visual'):
        raise AttributeError('visuals.py must define render_visual(scene, local_t).')
    return module


def init_worker(visuals_path, segments):
    global V, SEGMENTS
    V = load_visuals(visuals_path)
    SEGMENTS = Path(segments)


def probe(path, entry):
    result = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', entry,
                             '-of', 'default=noprint_wrappers=1:nokey=1', str(path)],
                            check=True, capture_output=True, text=True)
    return result.stdout.strip()


def prepare_timeline(path):
    timeline = json.loads(path.read_text(encoding='utf-8'))
    start_frame = 0
    for scene in timeline['scenes']:
        scene['frames'] = int(round(float(scene['duration']) * FPS))
        if scene['frames'] <= 0:
            raise ValueError(f"Scene {scene['id']} has no frames.")
        scene['duration'] = scene['frames'] / FPS
        scene['start_frame'] = start_frame
        scene['start'] = start_frame / FPS
        start_frame += scene['frames']
        if not Path(scene['audio_path']).exists():
            raise FileNotFoundError(scene['audio_path'])
    timeline['frames'] = start_frame
    timeline['duration'] = start_frame / FPS
    path.write_text(json.dumps(timeline, ensure_ascii=False, indent=2), encoding='utf-8')
    return timeline


def wrap_caption(draw, text, font, max_width=1660):
    lines, current = [], ''
    for char in text:
        candidate = current + char
        if current and draw.textlength(candidate, font=font) > max_width:
            split_at = max(current.rfind('；'), current.rfind('，'), current.rfind('：'))
            if split_at >= max(8, len(current) - 14):
                lines.append(current[:split_at + 1])
                current = current[split_at + 1:] + char
            else:
                lines.append(current)
                current = char
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines


def draw_caption(img, text, opacity):
    if not text or opacity <= 0.01:
        return
    d = ImageDraw.Draw(img)
    chosen = None
    for size in (40, 38, 36, 34, 32):
        f = subtitle_font(size)
        lines = wrap_caption(d, text, f, 1760 if size == 32 else 1660)
        if len(lines) <= 2:
            chosen = (f, lines)
            break
    if chosen is None:
        raise ValueError(f'Caption cannot fit in two lines: {text}')
    f, lines = chosen
    color = tuple(int(SCREEN[i] + (FG[i] - SCREEN[i]) * opacity) for i in range(3))
    ys = [930] if len(lines) == 1 else [897, 959]
    for line, y in zip(lines, ys):
        d.text((960, y), line, font=f, fill=color, anchor='mm', stroke_width=3, stroke_fill=SCREEN)


def render_scene(scene):
    scene_id = int(scene['id'])
    output = SEGMENTS / f'scene_{scene_id:02d}.mp4'
    command = ['ffmpeg', '-y', '-hide_banner', '-loglevel', 'error',
               '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-s', '1920x1080', '-r', str(FPS),
               '-i', '-', '-an', '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '18',
               '-threads', '2', '-pix_fmt', 'yuv420p', '-movflags', '+faststart', str(output)]
    proc = subprocess.Popen(command, stdin=subprocess.PIPE)
    cues = scene.get('subtitle_cues', [])
    cue_index = 0
    try:
        for frame_index in range(scene['frames']):
            t = frame_index / FPS
            img = V.render_visual(scene, t).convert('RGB')
            if img.size != (1920, 1080):
                raise ValueError(f'Scene {scene_id} returned {img.size}, expected 1920x1080.')
            while cue_index < len(cues) - 1 and t >= cues[cue_index]['end']:
                cue_index += 1
            caption = ''
            if cues:
                cue = cues[cue_index]
                if cue['start'] <= t < cue['end']:
                    caption = cue['text']
            opacity = min(max(0, t / 0.35), max(0, (scene['duration'] - t) / 0.45), 1)
            draw_caption(img, caption, opacity)
            proc.stdin.write(img.tobytes())
            if frame_index % 300 == 0:
                print(f'scene {scene_id:02d}: {frame_index}/{scene["frames"]}', flush=True)
        proc.stdin.close()
        code = proc.wait()
        if code != 0:
            raise RuntimeError(f'FFmpeg scene {scene_id} exited {code}.')
    except Exception:
        if proc.stdin and not proc.stdin.closed:
            proc.stdin.close()
        proc.kill()
        raise
    count = int(probe(output, 'stream=nb_frames'))
    if count != scene['frames']:
        raise RuntimeError(f'Scene {scene_id}: {count} frames != {scene["frames"]}.')
    print(f'scene {scene_id:02d}: DONE {count} frames', flush=True)
    return str(output)


def mux(timeline, project, output):
    paths = [project / 'segments' / f'scene_{s["id"]:02d}.mp4' for s in timeline['scenes']]
    missing = [str(p) for p in paths if not p.exists()]
    if missing:
        raise FileNotFoundError('Missing segments: ' + ', '.join(missing))
    concat = project / 'concat.txt'
    concat.write_text(''.join(f"file '{p}'\n" for p in paths), encoding='utf-8')
    video = project / 'video_full.mp4'
    subprocess.run(['ffmpeg', '-y', '-hide_banner', '-loglevel', 'error', '-f', 'concat',
                    '-safe', '0', '-i', str(concat), '-c', 'copy', str(video)], check=True)
    inputs, filters = [], []
    for idx, scene in enumerate(timeline['scenes']):
        inputs.extend(['-i', scene['audio_path']])
        delay = round((scene['start'] + scene['audio_offset']) * 1000)
        filters.append(f'[{idx + 1}:a]aresample=48000,adelay={delay}|{delay}[a{idx}]')
    labels = ''.join(f'[a{i}]' for i in range(len(timeline['scenes'])))
    filters.append(f'{labels}amix=inputs={len(timeline["scenes"])}:duration=longest:normalize=0,'
                   f'apad,atrim=0:{timeline["duration"]:.6f}[out]')
    subprocess.run(['ffmpeg', '-y', '-hide_banner', '-loglevel', 'error', '-i', str(video), *inputs,
                    '-filter_complex', ';'.join(filters), '-map', '0:v:0', '-map', '[out]',
                    '-c:v', 'copy', '-c:a', 'aac', '-b:a', '192k', '-ar', '48000',
                    '-t', f'{timeline["duration"]:.6f}', '-movflags', '+faststart',
                    '-metadata', f'title={timeline.get("title", "Algorithm video")}', str(output)], check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--workers', type=int, default=4)
    parser.add_argument('--only-scene', help='Comma-separated scene IDs to rerender before muxing.')
    parser.add_argument('--mux-only', action='store_true')
    parser.add_argument('--overwrite', action='store_true')
    args = parser.parse_args()
    if args.output.exists() and not args.overwrite:
        parser.error('Output exists. Choose a new name or explicitly use --overwrite.')
    if args.mux_only and args.only_scene:
        parser.error('--mux-only cannot be combined with --only-scene.')
    visuals_path = args.project / 'visuals.py'
    if not visuals_path.exists():
        parser.error(f'Missing {visuals_path}.')
    segments = args.project / 'segments'
    segments.mkdir(parents=True, exist_ok=True)
    timeline = prepare_timeline(args.project / 'timeline.json')
    if not args.mux_only:
        selected = timeline['scenes']
        if args.only_scene:
            ids = {int(x) for x in args.only_scene.split(',')}
            selected = [s for s in selected if int(s['id']) in ids]
            if not selected:
                parser.error('No valid scenes selected.')
        with mp.Pool(max(1, min(args.workers, len(selected))), initializer=init_worker,
                     initargs=(str(visuals_path), str(segments))) as pool:
            pool.map(render_scene, selected)
    mux(timeline, args.project, args.output)
    print(f'saved={args.output}')
    print(f'duration={probe(args.output, "format=duration")}, frames={timeline["frames"]}')


if __name__ == '__main__':
    main()
