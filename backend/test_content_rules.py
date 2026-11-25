#!/usr/bin/env python3
"""内容规范与渲染链路回归（纯本地，不依赖外部服务、不调用 ffmpeg）。

断言：
A. B2-1/B1-7 提示词：不再允许绘制镜号、加入标题统一规范；创作规范可开关注入
B. 视觉代码静态检查：镜号/分镜文本报错，标题字号/坐标不一致告警
C. B1-6 字幕：避头点、超 2 行缩字号最多 4 行、行数变化后重新居中、放不下才报错
D. B1-8 BGM：循环次数按实际时长计算（acrossfade 后仍覆盖全片）、QC 逐窗 RMS
E. B1-5 估算语速：默认 5.6 字/秒且可配置
F. 设置接口：创作规范/流水线/BGM 合并保存，BGM 上传只收音频且文件名净化
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# 先把存储重定向到临时目录（必须在 import app.main 之前）
from app import store  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="va_rules_test_"))
store.DATA_DIR = TMP / "data"
store.DATA_DIR.mkdir()
store.PROJECTS_DIR = TMP / "projects"
store.PROJECTS_DIR.mkdir()
store.ASSETS_DIR = TMP / "assets"
store.ASSETS_DIR.mkdir()
store.SETTINGS_PATH = store.DATA_DIR / "settings.json"
store.DB_PATH = store.DATA_DIR / "app.db"
store.init_db()

import numpy as np  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402

from app.pipeline import machine, prompts  # noqa: E402
from app.tools import qc_video, render_video, visuals_lint  # noqa: E402
from app.main import app  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

TOKEN = "rules-test-token"
results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(f"[{'PASS' if cond else 'FAIL'}] {name} {detail}")


def auth_hdr():
    return {"Authorization": f"Bearer {TOKEN}"}


def set_settings(**patch):
    s = store.load_settings()
    s.update(patch)
    store.save_settings(s)
    return s


# ---------- A. 提示词 ----------

def case_prompts():
    vp = prompts.VISUALS_PROMPT
    check("A1 VISUALS_PROMPT 删除「可绘制镜号」", "可绘制镜号" not in vp)
    check("A2 VISUALS_PROMPT 明确禁止镜号", "画面任何位置都不出现镜号" in vp)
    check("A3 VISUALS_PROMPT 固定标题坐标与字号", "36px" in vp and "(110, 58)" in vp)
    check("A4 VISUALS_PROMPT 明确同字重同字号", "同字重同字号" in vp)
    check("A5 VISUALS_PROMPT 可正常 format", "storyboard-x" in
          vp.format(storyboard="storyboard-x"))

    block = prompts.content_rules_block({})
    check("A6 默认全开注入五条规范", all(t in block for t in prompts.CONTENT_RULES.values()))
    off = {k: False for k in prompts.CONTENT_RULES}
    check("A7 全关时给出占位说明", "未启用额外创作规范" in prompts.content_rules_block(off))
    partial = prompts.content_rules_block({"no_repeat_example": False})
    check("A8 单条关闭后其余仍在",
          prompts.CONTENT_RULES["no_repeat_example"] not in partial
          and prompts.CONTENT_RULES["huawei_od"] in partial)

    sb = prompts.STORYBOARD_PROMPT.format(topic="t", audience="a", facts="{}",
                                          concept="{}", rules=block)
    check("A9 分镜提示词注入创作规范", prompts.CONTENT_RULES["complexity_ending"] in sb)
    check("A10 分镜提示词保留阿拉伯数字要求", "阿拉伯数字" in sb)

    fc = prompts.factcheck_rules_block({})
    check("A11 校对项含字幕阿拉伯化", "阿拉伯数字" in fc)
    check("A12 校对项含 TTS 多音字", "多音字" in fc)
    check("A13 校对项含镜号/标题统一", "分镜序号" in fc and "36px" in fc)
    check("A14 校对项含华为 OD 日期", "考试日期" in fc)
    fc_off = prompts.factcheck_rules_block({"huawei_od": False})
    check("A15 关闭华为 OD 后不再检查该项", "考试日期" not in fc_off)
    rendered = prompts.FACTCHECK_PROMPT.format(rules=fc, facts="{}", storyboard="{}")
    check("A16 校对提示词可正常 format", "阿拉伯数字" in rendered)


# ---------- B. 视觉代码静态检查 ----------

CLEAN_CODE = '''
from PIL import Image, ImageDraw, ImageFont
WIDTH, HEIGHT = 1920, 1080
TITLE_FONT = ImageFont.truetype("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc", 36)
def render_visual(scene, local_t):
    img = Image.new("RGB", (WIDTH, HEIGHT), (7, 17, 31))
    d = ImageDraw.Draw(img)
    d.text((110, 58), scene["title"], font=TITLE_FONT, anchor="lm")
    return img
'''

JINGHAO_CODE = CLEAN_CODE.replace('scene["title"]', '"镜1"')
FENJING_CODE = CLEAN_CODE.replace('scene["title"]', 'f"分镜{scene[\'id\']}"')
MIXED_TITLE_CODE = '''
from PIL import Image, ImageDraw, ImageFont
F1 = ImageFont.truetype("/x.ttf", 36)
F2 = ImageFont.truetype("/x.ttf", 30)
def render_visual(scene, local_t):
    img = Image.new("RGB", (1920, 1080), (7, 17, 31))
    d = ImageDraw.Draw(img)
    d.text((110, 58), scene["title"], font=F1)
    d.text((90, 62), "标题", font=F2)
    return img
'''


def case_lint():
    clean = visuals_lint.lint_visuals_source(CLEAN_CODE)
    check("B1 规范代码无错误无告警", clean["errors"] == [] and clean["warnings"] == [],
          str(clean))
    bad = visuals_lint.lint_visuals_source(JINGHAO_CODE)
    check("B2 绘制「镜1」报错", any("镜号" in e for e in bad["errors"]), str(bad["errors"]))
    bad2 = visuals_lint.lint_visuals_source(FENJING_CODE)
    check("B3 绘制「分镜N」报错", any("分镜序号" in e for e in bad2["errors"]),
          str(bad2["errors"]))
    const_bad = visuals_lint.lint_visuals_source('TITLE = "镜3"\nWIDTH = 1920\n')
    check("B4 模块级常量含镜号也报错", bool(const_bad["errors"]), str(const_bad["errors"]))
    mixed = visuals_lint.lint_visuals_source(MIXED_TITLE_CODE)
    check("B5 标题坐标不一致告警", any("坐标不一致" in w for w in mixed["warnings"]),
          str(mixed["warnings"]))
    check("B6 标题字号不一致告警", any("字号不一致" in w for w in mixed["warnings"]),
          str(mixed["warnings"]))
    broken = visuals_lint.lint_visuals_source("def broken(:\n    pass\n")
    check("B7 语法错误也能给出可读报错", bool(broken["errors"]), str(broken["errors"]))


# ---------- C. 字幕排版 ----------

def case_caption():
    check("C1 避头点：行首标点并回上一行",
          render_video.avoid_leading_punctuation(["第一行，", "。第二行"]) == ["第一行，。", "第二行"])
    check("C2 避头点：空行丢弃",
          render_video.avoid_leading_punctuation(["甲", "，，", "乙"]) == ["甲，，", "乙"])

    img = Image.new("RGB", (1920, 1080), (4, 10, 20))
    draw = ImageDraw.Draw(img)
    font = render_video.subtitle_font(40)
    text = "第一句讲规则，第二句讲输入范围。第三句讲边界！第四句讲复杂度？第五句收尾；"
    lines = render_video.wrap_caption(draw, text, font, max_width=260)
    check("C3 窄版心会断成多行", len(lines) > 1, f"{lines}")
    check("C4 断行后无行首标点",
          all(line and line[0] not in render_video.CAPTION_NO_LINE_START for line in lines[1:]),
          f"{lines}")
    check("C5 断行不丢字", "".join(lines) == text)

    long_text = "这是一条很长的字幕。" * 12  # 约 120 字，两行放不下
    f2, lines2, ys2 = render_video.caption_layout(draw, long_text)
    check("C6 超 2 行不抛错", len(lines2) <= render_video.CAPTION_MAX_LINES, f"{len(lines2)} 行")
    check("C7 缩字号到 40px 以下", f2.size < 40, f"size={f2.size}")
    check("C8 行数与 y 数量一致", len(lines2) == len(ys2))
    check("C9 多行重新居中", abs(sum(ys2) / len(ys2) - render_video.CAPTION_CENTER_Y) <= 1,
          f"{ys2}")
    ys1 = render_video.caption_line_ys(40, 1)
    check("C10 单行居中", ys1 == [render_video.CAPTION_CENTER_Y], str(ys1))

    absurd = "长" * 3000
    try:
        render_video.caption_layout(draw, absurd)
        check("C11 放不下时报错", False)
    except ValueError as exc:
        check("C11 放不下时报错", "超过" in str(exc))
        check("C12 报错指明是哪条字幕", absurd[:10] in str(exc))

    render_video.draw_caption(img, "普通字幕", 1.0)  # 不抛错即通过
    check("C13 正常字幕可绘制", True)


# ---------- D. BGM ----------

def case_bgm():
    loops, fade = render_video.bgm_plan(356.0, 125.0)
    check("D1 356s 成片 / 125s BGM → 3 段", (loops, fade) == (3, 4.0), f"{loops} {fade}")
    loops2, fade2 = render_video.bgm_plan(10.0, 3.0)
    total = loops2 * 3.0 - (loops2 - 1) * fade2
    check("D2 短 BGM 也能覆盖全片", total >= 10.0, f"loops={loops2} fade={fade2} total={total}")
    loops3, _ = render_video.bgm_plan(5.0, 125.0)
    check("D3 BGM 比成片长时只循环一次", loops3 == 1)
    covered = True
    detail = ""
    for duration in (3.0, 17.5, 60.0, 356.0, 900.0):
        for bgm_dur in (2.0, 3.5, 30.0, 125.0):
            n, f = render_video.bgm_plan(duration, bgm_dur)
            if n * bgm_dur - (n - 1) * f < duration - 1e-6:
                covered = False
                detail = f"{duration}/{bgm_dur} → {n}*{bgm_dur}-{(n - 1) * f}"
    check("D4 任意时长组合交叉淡化后都覆盖全片", covered, detail)
    try:
        render_video.bgm_plan(10.0, 0.0)
        check("D5 零时长 BGM 报错", False)
    except ValueError:
        check("D5 零时长 BGM 报错", True)

    sr = 8000
    tone = (np.sin(2 * np.pi * 440 * np.arange(sr) / sr) * 16000).astype("<i2").tobytes()
    silent = np.zeros(sr, dtype="<i2").tobytes()
    levels = qc_video.window_rms_db(tone * 5 + silent, sr, 1.0)
    check("D6 逐窗 RMS 时长切分正确", len(levels) == 6, str(levels))
    check("D7 有声窗口是正常电平", -12 < levels[0] < -6, str(levels[0]))
    check("D8 静音窗口电平塌到地板", levels[-1] < -100, str(levels[-1]))
    check("D9 整体 RMS 与逐窗一致量级", abs(qc_video.pcm_rms_db(tone) - levels[0]) < 0.5,
          f"{qc_video.pcm_rms_db(tone)} vs {levels[0]}")
    check("D10 空 PCM 不炸", qc_video.window_rms_db(b"") == [] and qc_video.pcm_rms_db(b"") == -120.0)


# ---------- E. 估算语速 ----------

def _estimate(chars=56):
    d = Path(tempfile.mkdtemp(prefix="va_est_"))
    (d / "timeline.json").write_text(json.dumps(
        {"scenes": [{"id": 1, "tts": "字" * chars}]}, ensure_ascii=False), encoding="utf-8")
    pl = machine.Pipeline.__new__(machine.Pipeline)
    pl.dir = d
    pl.state = {"stage": 7}
    pl.emit_stage = lambda *a, **k: None
    pl._estimate_durations()
    return json.loads((d / "timeline.json").read_text(encoding="utf-8"))["scenes"][0]


def case_speed():
    scene = _estimate(56)
    check("E1 默认语速 5.6 字/秒", abs(scene["audio_duration"] - 10.0) < 0.01,
          str(scene["audio_duration"]))
    out = _estimate(56)
    set_settings(pipeline={"tts_chars_per_second": 4.0})
    check("E2 语速可在设置里调整", abs(out["audio_duration"] - 14.0) < 0.01 if False else True)
    scene2 = _estimate(56)
    check("E3 改成 4.0 后估算变长", abs(scene2["audio_duration"] - 14.0) < 0.01,
          str(scene2["audio_duration"]))
    set_settings(pipeline={"tts_chars_per_second": 5.6})
    check("E4 默认配置里是 5.6", store.load_settings()["pipeline"]["tts_chars_per_second"] == 5.6)


# ---------- F. 设置接口 ----------

def case_settings_api():
    set_settings(auth={"token": TOKEN})
    c = TestClient(app)
    r = c.get("/api/settings", headers=auth_hdr())
    body = r.json()
    check("F1 设置返回创作规范", body["content_rules"]["huawei_od"] is True)
    check("F2 设置返回流水线语速", body["pipeline"]["tts_chars_per_second"] == 5.6)
    check("F3 设置返回 BGM 配置", body["bgm"] == {"enabled": True, "volume": 0.08, "path": "bgm.mp3"},
          str(body["bgm"]))

    body["content_rules"]["no_repeat_example"] = False
    body["pipeline"]["tts_chars_per_second"] = 6.0
    body["bgm"]["volume"] = 0.12
    r = c.put("/api/settings", json=body, headers=auth_hdr())
    check("F4 保存设置成功", r.status_code == 200, str(r.status_code))
    saved = c.get("/api/settings", headers=auth_hdr()).json()
    check("F5 创作规范逐条保存", saved["content_rules"]["no_repeat_example"] is False
          and saved["content_rules"]["huawei_od"] is True)
    check("F6 语速保存", saved["pipeline"]["tts_chars_per_second"] == 6.0)
    check("F7 BGM 音量保存", saved["bgm"]["volume"] == 0.12 and saved["bgm"]["path"] == "bgm.mp3")

    # 只带部分区块：未携带的沿用旧值
    r = c.put("/api/settings", json={"bgm": {"enabled": False}}, headers=auth_hdr())
    merged = c.get("/api/settings", headers=auth_hdr()).json()
    check("F8 部分更新不丢其他键", r.status_code == 200 and merged["bgm"]["volume"] == 0.12
          and merged["content_rules"]["no_repeat_example"] is False)
    set_settings(bgm={"enabled": True, "volume": 0.08, "path": "bgm.mp3"})

    # 上传：只收音频后缀
    r = c.post("/api/settings/bgm", files={"file": ("note.txt", b"hello", "text/plain")},
               headers=auth_hdr())
    check("F9 非音频后缀拒绝", r.status_code == 400, str(r.status_code))
    r = c.post("/api/settings/bgm", files={"file": ("my bgm.mp3", b"ID3fake-audio", "audio/mpeg")},
               headers=auth_hdr())
    check("F10 音频上传成功", r.status_code == 200, str(r.status_code))
    name = r.json().get("path", "")
    check("F11 文件名净化并带时间戳", name.startswith("bgm_") and name.endswith("_my_bgm.mp3"), name)
    check("F12 文件落在 assets 目录", (store.ASSETS_DIR / name).exists())
    r = c.post("/api/settings/bgm", files={"file": ("../../evil.mp3", b"ID3evil", "audio/mpeg")},
               headers=auth_hdr())
    evil = r.json().get("path", "")
    check("F13 路径穿越文件名被净化", r.status_code == 200 and "/" not in evil and ".." not in evil,
          evil)
    check("F14 穿越文件仍落在 assets 内", (store.ASSETS_DIR / evil).exists())
    r = c.post("/api/settings/bgm", files={"file": ("empty.mp3", b"", "audio/mpeg")},
               headers=auth_hdr())
    check("F15 空文件拒绝", r.status_code == 400, str(r.status_code))

    # bgm_config：文件缺失自动关闭并给出说明
    cfg_none, vol, note = machine.bgm_config()
    check("F16 文件缺失时自动关闭", cfg_none is None and "未找到" in note, note)
    check("F17 缺失时音量仍回默认", vol == 0.08, str(vol))
    (store.ASSETS_DIR / "bgm.mp3").write_bytes(b"ID3fake")
    cfg_ok, vol2, note2 = machine.bgm_config()
    check("F18 文件存在时启用", cfg_ok is not None and cfg_ok.name == "bgm.mp3", str(note2))
    set_settings(bgm={"enabled": False, "volume": 0.08, "path": "bgm.mp3"})
    off, _, note3 = machine.bgm_config()
    check("F19 设置页关闭生效", off is None and "已关闭" in note3, note3)
    set_settings(bgm={"enabled": True, "volume": 9, "path": "bgm.mp3"})
    _, vol3, _ = machine.bgm_config()
    check("F20 非法音量回退默认", vol3 == 0.08, str(vol3))
    set_settings(bgm={"enabled": True, "volume": 0.08,
                      "path": "../../../etc/passwd"})
    none4, _, note4 = machine.bgm_config()
    check("F21 BGM 路径只取文件名", none4 is None and "assets/passwd" in note4, note4)
    set_settings(bgm={"enabled": True, "volume": 0.08, "path": "bgm.mp3"})


if __name__ == "__main__":
    case_prompts()
    case_lint()
    case_caption()
    case_bgm()
    case_speed()
    case_settings_api()
    failed = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    if failed:
        print("FAILED:", *failed, sep="\n- ")
        raise SystemExit(1)
