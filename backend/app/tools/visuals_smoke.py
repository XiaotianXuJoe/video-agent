#!/usr/bin/env python3
"""视觉代码冒烟渲染：对每镜在 start/mid/end 三个时间点调用 render_visual，
验证每次调用都返回合法的 1920x1080 PIL 图像。不保存任何文件，专供阶段 6
生成后自检（在关键帧/整片渲染之前提前暴露坐标越界、KeyError 等运行时错误）。
用法: python3 visuals_smoke.py --project /path/to/project
退出码 0=通过，1=失败（失败细节以 JSON 打在 stdout 最后一行）。"""
import argparse
import importlib.util
import json
import sys
import traceback
from pathlib import Path


def fail(phase, **kw):
    print(json.dumps({"ok": False, "phase": phase,
                      "error": traceback.format_exc()[-1500:], **kw},
                     ensure_ascii=False))
    sys.exit(1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--project', type=Path, required=True)
    args = ap.parse_args()
    proj = args.project
    timeline = json.loads((proj / 'timeline.json').read_text(encoding='utf-8'))
    try:
        spec = importlib.util.spec_from_file_location('visuals_smoke', proj / 'visuals.py')
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        if not callable(getattr(mod, 'render_visual', None)):
            raise AttributeError('visuals.py 未定义可调用的 render_visual')
    except Exception:
        fail('import')
    from PIL import Image
    W, H = timeline.get('width', 1920), timeline.get('height', 1080)
    checked = 0
    for scene in timeline['scenes']:
        dur = scene.get('duration') or (scene.get('audio_offset', 0.55)
                                        + scene.get('audio_duration', 5.0)
                                        + scene.get('end_hold', 1.2))
        sc = dict(scene)
        sc.setdefault('audio_offset', 0.55)
        sc.setdefault('audio_duration', max(1.0, dur - 1.75))
        sc.setdefault('duration', dur)
        for pos, t in [('start', 0.05), ('mid', dur / 2), ('end', max(0.1, dur - 0.1))]:
            try:
                img = mod.render_visual(sc, t)
                if not isinstance(img, Image.Image):
                    raise TypeError(f'render_visual 返回 {type(img).__name__}，不是 PIL.Image')
                if img.size != (W, H):
                    raise ValueError(f'画面尺寸 {img.size}，应为 {(W, H)}')
                img.convert('RGB')
            except Exception:
                fail('render', scene=sc.get('id'), pos=pos, t=round(t, 3))
            checked += 1
    print(json.dumps({'ok': True, 'frames_checked': checked,
                      'scenes': len(timeline['scenes'])}, ensure_ascii=False))


if __name__ == '__main__':
    main()
