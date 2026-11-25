"""产物注册表：类型/版本/路径/来源模型。产物即文件，注册表即索引。
旧版不覆盖：同一 logical_name 重复注册自动递增版本号。"""
import json
import os
import time
from pathlib import Path


def _registry_path(proj_dir: Path) -> Path:
    return proj_dir / "artifacts.json"


def _load(proj_dir: Path) -> list:
    p = _registry_path(proj_dir)
    if p.exists():
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    return []


def _save(proj_dir: Path, items: list):
    tmp = _registry_path(proj_dir).with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)
    os.replace(tmp, _registry_path(proj_dir))


def register(proj_dir: Path, logical_name: str, atype: str, title: str,
             rel_path: str, source: str, extra: dict = None) -> dict:
    """atype: doc|code|gallery|audio|video|json；source 如实记录生成者（如 'opus:claude-opus-5-5' / 'tool:render_video'）。"""
    items = _load(proj_dir)
    versions = [i["version"] for i in items if i["logical_name"] == logical_name]
    version = max(versions, default=0) + 1
    # 旧版本标记为非当前
    for i in items:
        if i["logical_name"] == logical_name:
            i["current"] = False
    art = {
        "id": f"{logical_name}_v{version}",
        "logical_name": logical_name,
        "type": atype,
        "title": title,
        "path": rel_path,
        "version": version,
        "current": True,
        "source": source,
        "created": time.time(),
        "extra": extra or {},
    }
    items.append(art)
    _save(proj_dir, items)
    return art


def list_artifacts(proj_dir: Path) -> list:
    return _load(proj_dir)


def get_artifact(proj_dir: Path, art_id: str) -> dict:
    for i in _load(proj_dir):
        if i["id"] == art_id:
            return i
    return None


def resolve_path(proj_dir: Path, rel_path: str) -> Path:
    """防目录穿越：只允许项目目录内的文件。
    用 resolve() + relative_to() 判包含关系，绝不用字符串前缀（'abc' 会被 'abc2' 前缀骗过）。"""
    base = Path(proj_dir).resolve()
    p = (base / rel_path).resolve()
    try:
        p.relative_to(base)
    except ValueError:
        raise ValueError("path escape")
    return p


def mask_extra(extra: dict | None) -> dict:
    """对外回显的产物 metadata：音色 ID 只留后 4 位（完整值仅服务端使用）。"""
    out = dict(extra or {})
    vid = out.get("voice_id")
    if vid:
        s = str(vid)
        out["voice_id"] = "***" + s[-4:] if len(s) >= 4 else "***"
    return out


def masked_artifact(art: dict) -> dict:
    """产物对象的对外副本（不改注册表原文）。"""
    return {**art, "extra": mask_extra(art.get("extra"))}


def list_artifacts_masked(proj_dir: Path) -> list:
    return [masked_artifact(a) for a in _load(proj_dir)]
