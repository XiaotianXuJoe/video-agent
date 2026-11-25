"""视频标注：存取 + ffmpeg 精确抽帧 + 时间戳→镜头/帧号反查。
timeline.json 是唯一时间基准（对照视图、filmstrip、标注共用）。"""
import json
import os
import subprocess
import time
from pathlib import Path

from . import store


def _ann_dir(proj_dir: Path) -> Path:
    d = proj_dir / "annotations"
    d.mkdir(exist_ok=True)
    return d


def _load(proj_dir: Path) -> list:
    p = _ann_dir(proj_dir) / "annotations.json"
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return []


def _save(proj_dir: Path, items: list):
    p = _ann_dir(proj_dir) / "annotations.json"
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)


def locate_scene(timeline: dict, t: float):
    """t → (scene, frame_no)。镜与镜交界帧归后一镜。"""
    fps = timeline.get("fps", 30)
    for sc in timeline.get("scenes", []):
        start = sc.get("start", 0)
        dur = sc.get("duration", 0)
        if start <= t < start + dur:
            return sc["id"], int(round(t * fps))
    scenes = timeline.get("scenes", [])
    if scenes:
        return scenes[-1]["id"], int(round(t * fps))
    return None, int(round(t * fps))


def save_annotation(project_id: str, data: dict) -> dict:
    proj_dir = store.project_dir(project_id)
    tl_path = proj_dir / "timeline.json"
    timeline = json.loads(tl_path.read_text(encoding="utf-8")) if tl_path.exists() else {}
    t = float(data.get("t_start", 0))
    scene_id, frame_no = locate_scene(timeline, t)
    ann = {
        "id": f"ann_{int(time.time()*1000)}",
        "type": data.get("type", "frame"),  # frame | segment
        "video_version": data.get("video_version", ""),
        "scene_id": data.get("scene_id") or scene_id,
        "frame_no": frame_no,
        "t_start": t,
        "t_end": float(data.get("t_end", t)),
        "shapes": data.get("shapes", []),
        "snapshot": data.get("snapshot", ""),  # 相对路径（前端 canvas 导出后上传）
        "comment": data.get("comment", ""),
        "created": time.time(),
    }
    items = _load(proj_dir)
    items.append(ann)
    _save(proj_dir, items)
    return ann


def list_annotations(project_id: str) -> list:
    return _load(store.project_dir(project_id))


def extract_frame(video_path: Path, t: float, out_path: Path):
    """ffmpeg 精确 seek 抽帧为 JPEG。"""
    subprocess.run([
        "ffmpeg", "-v", "error", "-ss", f"{t:.3f}", "-i", str(video_path),
        "-frames:v", "1", "-q:v", "3", "-y", str(out_path),
    ], check=True, timeout=60)
