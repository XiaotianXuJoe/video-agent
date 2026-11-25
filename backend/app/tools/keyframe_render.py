#!/usr/bin/env python3
"""渲染每镜开始/中段/结尾关键帧到 keyframes/ 目录。
用法: python3 keyframe_render.py --project /path/to/project
"""
import argparse
import importlib.util
import json
import sys
from pathlib import Path


def load_visuals(proj: Path):
    spec = importlib.util.spec_from_file_location("visuals", proj / "visuals.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--project', type=Path, required=True)
    ap.add_argument('--only-scene', default=None)
    args = ap.parse_args()
    proj = args.project
    timeline = json.loads((proj / 'timeline.json').read_text(encoding='utf-8'))
    mod = load_visuals(proj)
    out = proj / 'keyframes'
    out.mkdir(exist_ok=True)
    only = set(int(x) for x in args.only_scene.split(',')) if args.only_scene else None
    made = []
    for scene in timeline['scenes']:
        sid = scene['id']
        if only and sid not in only:
            continue
        dur = scene.get('duration') or (scene.get('audio_offset', 0.55)
                                        + scene.get('audio_duration', 5.0)
                                        + scene.get('end_hold', 1.2))
        scene = dict(scene)
        scene.setdefault('audio_offset', 0.55)
        scene.setdefault('audio_duration', max(1.0, dur - 1.75))
        scene.setdefault('duration', dur)
        for pos, t in [('start', 0.05), ('mid', dur / 2), ('end', max(0.1, dur - 0.1))]:
            img = mod.render_visual(scene, t)
            p = out / f'scene{sid:02d}_{pos}.png'
            img.save(p)
            made.append(str(p.name))
    print(json.dumps({'frames': made}, ensure_ascii=False))


if __name__ == '__main__':
    main()
