#!/usr/bin/env python3
"""Render a timeline with project visuals.py, then mux MiniMax audio (+ optional BGM) into MP4."""
import argparse
from functools import lru_cache
import importlib.util
import json
import math
import multiprocessing as mp
from pathlib import Path
import shutil
import subprocess
import tempfile

from PIL import Image, ImageDraw, ImageFont

FPS = 30
V = None
SEGMENTS = None
FG = (232, 238, 248)
SCREEN = (4, 10, 20)
FONT_PATHS = ['/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc',
              'C:/Windows/Fonts/msyh.ttc',
              'C:/Windows/Fonts/NotoSansSC-VF.ttf',
              '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf']

# ---------- 字幕排版（skill B1-6） ----------
# 避头点：这些标点不能出现在行首，必须并回上一行（允许悬挂溢出数像素）
CAPTION_NO_LINE_START = '、。！？》】，；：）…·」』”’%'
CAPTION_BREAK_PUNCT = '；，：、。！？'
# 字号自适应：先保 2 行（40→24），仍放不下最多 4 行，再放不下才报错
CAPTION_SIZES = (40, 38, 36, 34, 32, 30, 28, 26, 24)
CAPTION_MIN_SIZE = 24
CAPTION_COMFORT_LINES = 2
CAPTION_MAX_LINES = 4
CAPTION_CENTER_Y = 930


def _has_cjk_glyphs(font):
    """字体必须能渲染中文字形，否则排版测量与画面都会出豆腐块。"""
    try:
        return font.getmask('汉').getbbox() is not None
    except Exception:
        return False


@lru_cache(maxsize=16)
def subtitle_font(size):
    for path in FONT_PATHS:
        try:
            font = ImageFont.truetype(path, size)
        except OSError:
            continue
        if _has_cjk_glyphs(font):
            return font
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


# ---------- 字幕断行与避头点 ----------

def avoid_leading_punctuation(lines):
    """行首标点并回上一行；并回后仍为空的行直接丢弃。"""
    out = []
    for line in lines:
        while out and line and line[0] in CAPTION_NO_LINE_START:
            out[-1] += line[0]
            line = line[1:]
        if line:
            out.append(line)
    return out


def wrap_caption(draw, text, font, max_width=1660):
    """按实际像素宽度断行，优先在标点处断开，再做避头点处理。"""
    lines, current = [], ''
    for char in text:
        if current and draw.textlength(current + char, font=font) > max_width:
            split_at = max([current.rfind(p) for p in CAPTION_BREAK_PUNCT] + [-1])
            if split_at >= max(6, len(current) - 16):
                lines.append(current[:split_at + 1])
                current = current[split_at + 1:] + char
            else:
                lines.append(current)
                current = char
        else:
            current += char
    if current:
        lines.append(current)
    return avoid_leading_punctuation(lines)


def caption_line_ys(size, count):
    """按实际行数重新垂直居中：整块以 CAPTION_CENTER_Y 为中心。"""
    line_height = max(1, round(size * 1.55))
    total = (count - 1) * line_height
    return [CAPTION_CENTER_Y - total // 2 + i * line_height for i in range(count)]


def caption_layout(draw, text):
    """返回 (font, lines, ys)。优先 2 行内的大字号，放不下再缩到 24px、最多 4 行。"""
    sizes = CAPTION_SIZES
    for size in sizes:
        font = subtitle_font(size)
        # 小字号允许更宽的版心，避免无谓缩行
        lines = wrap_caption(draw, text, font, 1760 if size < 34 else 1660)
        if len(lines) <= CAPTION_COMFORT_LINES:
            return font, lines, caption_line_ys(size, len(lines))
    for size in sizes:
        font = subtitle_font(size)
        lines = wrap_caption(draw, text, font, 1760 if size < 34 else 1660)
        if len(lines) <= CAPTION_MAX_LINES:
            return font, lines, caption_line_ys(size, len(lines))
    raise ValueError(
        f'字幕缩到 {CAPTION_MIN_SIZE}px 仍超过 {CAPTION_MAX_LINES} 行，请拆分这条字幕：{text[:60]}')


def draw_caption(img, text, opacity):
    if not text or opacity <= 0.01:
        return
    d = ImageDraw.Draw(img)
    font, lines, ys = caption_layout(d, text)
    color = tuple(int(SCREEN[i] + (FG[i] - SCREEN[i]) * opacity) for i in range(3))
    for line, y in zip(lines, ys):
        d.text((960, y), line, font=font, fill=color, anchor='mm', stroke_width=3, stroke_fill=SCREEN)


# ---------- 渲染 ----------

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
            try:
                draw_caption(img, caption, opacity)
            except ValueError as exc:
                raise ValueError(f'Scene {scene_id} 字幕无法排版: {exc}') from exc
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


# ---------- BGM 混音（skill B1-8） ----------

def bgm_plan(duration: float, bgm_duration: float):
    """返回 (循环次数, 交叉淡化时长)。

    mp3 头里的 duration 不可靠，bgm_duration 必须来自实际解码后的 PCM。
    acrossfade 会让总长缩短 (loops-1)*fade，因此循环次数按「每段有效长度」倒推，
    保证交叉淡化后仍覆盖全片（只在开头有 BGM 即视为循环失败）。
    """
    if bgm_duration <= 0:
        raise ValueError('BGM 解码时长必须大于 0')
    fade = min(4.0, bgm_duration / 3)
    if bgm_duration <= 2 * fade:  # 极短 BGM：淡化再收一半，避免吃掉整段
        fade = bgm_duration / 4
    stride = max(0.1, bgm_duration - fade)
    loops = max(1, int(math.ceil((duration + fade) / stride)))
    return loops, fade


def decode_mono_wav(source: Path, target: Path) -> float:
    """把 BGM 解码成 48k 单声道 WAV 并返回真实时长（不信容器头）。"""
    subprocess.run(['ffmpeg', '-y', '-v', 'error', '-i', str(source),
                    '-ac', '1', '-ar', '48000', '-c:a', 'pcm_s16le', str(target)], check=True)
    return float(probe(target, 'format=duration'))


def mux(timeline, project, output, bgm=None, bgm_volume=0.08):
    """拼接逐镜画面、混入人声（以及可选 BGM），先写临时目录再拷回输出路径。"""
    paths = [project / 'segments' / f'scene_{s["id"]:02d}.mp4' for s in timeline['scenes']]
    missing = [str(p) for p in paths if not p.exists()]
    if missing:
        raise FileNotFoundError('Missing segments: ' + ', '.join(missing))
    work = Path(tempfile.mkdtemp(prefix='algorithm_video_mux_'))
    try:
        concat = work / 'concat.txt'
        concat.write_text(''.join(f"file '{p}'\n" for p in paths), encoding='utf-8')
        video = work / 'video_full.mp4'
        subprocess.run(['ffmpeg', '-y', '-hide_banner', '-loglevel', 'error', '-f', 'concat',
                        '-safe', '0', '-i', str(concat), '-c', 'copy', str(video)], check=True)
        scenes = timeline['scenes']
        duration = float(timeline['duration'])
        inputs, filters = [], []
        for idx, scene in enumerate(scenes):
            inputs.extend(['-i', scene['audio_path']])
            delay = round((scene['start'] + scene['audio_offset']) * 1000)
            filters.append(f'[{idx + 1}:a]aresample=48000,adelay={delay}|{delay}[a{idx}]')
        labels = ''.join(f'[a{i}]' for i in range(len(scenes)))
        filters.append(f'{labels}amix=inputs={len(scenes)}:duration=longest:normalize=0,'
                       f'apad,atrim=0:{duration:.6f}[voice]')
        final_input = '[voice]'
        loops = 0
        if bgm is not None:
            bed_wav = work / 'bgm.wav'
            bgm_duration = decode_mono_wav(Path(bgm), bed_wav)
            loops, fade = bgm_plan(duration, bgm_duration)
            first = 1 + len(scenes)
            for k in range(loops):
                # 每段都用独立 -i 输入：复用同一输入标签喂 acrossfade 会被 ffmpeg 静默丢弃
                inputs.extend(['-i', str(bed_wav)])
                filters.append(f'[{first + k}:a]aresample=48000,volume={bgm_volume:g},'
                               f'atrim=0:{bgm_duration:.6f},asetpts=N/SR/TB[bed{k}]')
            if loops == 1:
                bed = '[bed0]'
            else:
                prev = '[bed0]'
                for k in range(1, loops):
                    out = f'[x{k}]'
                    filters.append(f'{prev}[bed{k}]acrossfade=d={fade:.3f}:c1=tri:c2=tri{out}')
                    prev = out
                bed = prev
            fade_out_start = max(0.0, duration - 2.5)
            filters.append(f'{bed}atrim=0:{duration:.6f},'
                           f'afade=t=in:st=0:d={min(1.5, duration / 4):.3f},'
                           f'afade=t=out:st={fade_out_start:.3f}:d=2.5[bgm]')
            # 人声在前（duration=first 以人声长度为准）；normalize=0 才不会把人声压低 6dB
            filters.append('[voice][bgm]amix=inputs=2:duration=first:normalize=0,'
                           'alimiter=limit=0.85:level=false[out]')
            final_input = '[out]'
        tmp_out = work / 'final.mp4'
        command = ['ffmpeg', '-y', '-hide_banner', '-loglevel', 'error',
                   '-i', str(video), *inputs, '-filter_complex', ';'.join(filters),
                   '-map', '0:v:0', '-map', final_input, '-c:v', 'copy',
                   '-c:a', 'aac', '-b:a', '192k', '-ar', '48000',
                   '-t', f'{duration:.6f}', '-movflags', '+faststart',
                   '-metadata', f'title={timeline.get("title", "Algorithm video")}', str(tmp_out)]
        subprocess.run(command, check=True)
        shutil.copyfile(tmp_out, output)
        if bgm is not None:
            print(f'bgm={Path(bgm).name} loops={loops} volume={bgm_volume}', flush=True)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--workers', type=int, default=4)
    parser.add_argument('--only-scene', help='Comma-separated scene IDs to rerender before muxing.')
    parser.add_argument('--mux-only', action='store_true')
    parser.add_argument('--overwrite', action='store_true')
    parser.add_argument('--bgm', type=Path, help='Optional background music (loops to cover the whole video).')
    parser.add_argument('--bgm-volume', type=float, default=0.08,
                        help='BGM bed volume (default 0.08, low enough not to mask the voice).')
    args = parser.parse_args()
    if args.output.exists() and not args.overwrite:
        parser.error('Output exists. Choose a new name or explicitly use --overwrite.')
    if args.mux_only and args.only_scene:
        parser.error('--mux-only cannot be combined with --only-scene.')
    if args.bgm is not None and not args.bgm.exists():
        parser.error(f'BGM not found: {args.bgm}')
    if not 0 < args.bgm_volume <= 1:
        parser.error('--bgm-volume must be in (0, 1].')
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
    mux(timeline, args.project, args.output, bgm=args.bgm, bgm_volume=args.bgm_volume)
    print(f'saved={args.output}')
    print(f'duration={probe(args.output, "format=duration")}, frames={timeline["frames"]}')


if __name__ == '__main__':
    main()
