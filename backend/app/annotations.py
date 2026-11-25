"""视频标注：存取 + ffmpeg 精确抽帧 + 时间戳→镜头/帧号反查。
timeline.json 是唯一时间基准（对照视图、filmstrip、标注共用）。

快照存储（C-UX-09）：前端上传的 canvas data URL 落盘为 annotations/<ann_id>.jpg，
JSON 只记相对路径 snapshot_path；旧数据里的 base64（snapshot 字段）原样保留，
读取时仍可正常展示。"""
import base64
import binascii
import json
import os
import subprocess
import time
from pathlib import Path

from . import store
from .procsafe import preexec_limits, subprocess_env

# 单张快照上限：canvas 导出的 JPEG 通常 ~200KB，8MB 足够并挡住异常大包
SNAPSHOT_MAX_BYTES = 8 * 1024 * 1024


def _ann_dir(proj_dir: Path) -> Path:
    d = proj_dir / "annotations"
    d.mkdir(exist_ok=True)
    return d


def _save_snapshot(proj_dir: Path, ann_id: str, snapshot: str) -> tuple[str, str]:
    """把快照落盘为文件，返回 (相对路径, 需内联的旧式 base64)。
    - data URL → 解码写 annotations/<ann_id>.jpg|png，JSON 只存路径；
    - 已是相对路径（重存/调用方直接传路径）→ 原样复用；
    - 解码失败或超限 → 不落盘，返回空（避免把错误内容写进项目目录）。"""
    if not snapshot:
        return "", ""
    if not snapshot.startswith("data:"):
        return snapshot, ""
    header, sep, payload = snapshot.partition(",")
    if not sep:
        return "", ""
    mime = header[5:].split(";")[0].strip().lower()
    ext = "png" if "png" in mime else "jpg"
    try:
        raw = base64.b64decode(payload, validate=False)
    except (binascii.Error, ValueError):
        return "", ""
    if not raw or len(raw) > SNAPSHOT_MAX_BYTES:
        return "", ""
    name = f"{ann_id}.{ext}"
    (_ann_dir(proj_dir) / name).write_bytes(raw)
    return f"annotations/{name}", ""


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
    ann_id = f"ann_{int(time.time()*1000)}"
    snapshot_path, legacy_snapshot = _save_snapshot(
        proj_dir, ann_id, data.get("snapshot", "") or "")
    ann = {
        "id": ann_id,
        "type": data.get("type", "frame"),  # frame | segment
        "video_version": data.get("video_version", ""),
        "scene_id": data.get("scene_id") or scene_id,
        "frame_no": frame_no,
        "t_start": t,
        "t_end": float(data.get("t_end", t)),
        "shapes": data.get("shapes", []),
        # 新格式：快照是文件（annotations/ann_xxx.jpg），JSON 不再内联 base64
        "snapshot_path": snapshot_path,
        # 兼容字段：仅旧数据/无法落盘时才有 base64 内容
        "snapshot": legacy_snapshot,
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
    ], check=True, timeout=60, env=subprocess_env(), preexec_fn=preexec_limits())
