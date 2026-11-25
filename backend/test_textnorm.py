#!/usr/bin/env python3
"""B1-3 多音字 / B1-4 数字阿拉伯化 的规则表回归（纯本地，不依赖外部服务）。

覆盖 SKILL.md:44-48 与 minimax-api.md:80-88 列出的全部例子，外加白名单与幂等性。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from app.pipeline import textnorm  # noqa: E402

results = []


def check(name, got, want):
    ok = got == want
    results.append((name, ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: got={got!r} want={want!r}")


# ---------- A. 字幕端：阿拉伯数字 ----------

def case_arabic():
    cases = [
        # SKILL.md:47 的原例（字幕侧）
        ("两人时", "2人时"),
        ("零加三模二，得一", "0+3模2，得1"),
        ("零加三 模 二，得一", "0+3 模 2，得1"),
        # 0基／1基
        ("零基／一基", "0基／1基"),
        # 序号 / 位值 / 量词
        ("第一步", "第1步"),
        ("十二个节点", "12个节点"),
        ("二十三步", "23步"),
        ("一百零五", "105"),
        ("两千零一十五", "2015"),
        ("三万个元素", "30000个元素"),
        ("十", "10"),
        # 已阿拉伯化的文本保持幂等
        ("f(1)是0。2人时，0+3 模 2，得1", "f(1)是0。2人时，0+3 模 2，得1"),
        ("O(N) 与 O(n log n)", "O(N) 与 O(n log n)"),
        # 全角数字归一
        ("１２３", "123"),
    ]
    for src, want in cases:
        check(f"阿拉伯化 {src}", textnorm.to_arabic(src), want)


def case_whitelist():
    cases = [
        "统一处理", "一致", "一般情况", "一起排序", "一下", "一点",
        "十分重要", "千万不要", "唯一解", "两难", "二手数据",
    ]
    for src in cases:
        check(f"白名单不误改 {src}", textnorm.to_arabic(src), src)
    # 幂等：连套两次结果不变
    src = "两人时，零加三模二，得一"
    once = textnorm.to_arabic(src)
    check("阿拉伯化幂等", textnorm.to_arabic(once), once)


def case_caption_batch():
    out, changes = textnorm.to_arabic_many(["两人时", "f(1)是0", "统一"])
    check("批量转换结果", out, ["2人时", "f(1)是0", "统一"])
    check("批量改动条目只记真改动", len(changes), 1)
    check("批量改动条目内容", changes[0], {"from": "两人时", "to": "2人时"})


# ---------- B. 配音端：英文 / 符号 / 括号 ----------

def case_tts_symbols():
    cases = [
        ("O(N)", "O N"),
        ("复杂度 O(N log N)", "复杂度 O N log N"),
        ("TIMEOUT", "time out"),
        ("OR", "or"),
        ("NOT (NOT CPU)", "NOT，括号，NOT CPU"),
        ("voice_id", "voice id"),
        ("EQ", "E Q"),
        ("CPU", "CPU"),
    ]
    for src, want in cases:
        check(f"TTS 读法 {src}", textnorm.for_tts(src), want)


# ---------- C. 配音端：多音字 ----------

def case_polyphones():
    cases = [
        ("排成 5 行", "排成 5 航"),
        ("排成5行", "排成5航"),
        ("排成五行", "排成五航"),
        ("第 3 行第 2 列", "第 3 航第 2 列"),
        ("重复执行", "崇复执行"),
        ("重新计算", "崇新计算"),
        ("增长一倍", "增掌一倍"),
        ("长度", "长度"),
        ("进行排序", "进行排序"),
        ("银行", "银行"),
        # 幂等：已替换过的文本再套一次不变
        ("排成 5 航", "排成 5 航"),
        ("崇复执行", "崇复执行"),
    ]
    for src, want in cases:
        check(f"多音字 {src}", textnorm.for_tts(src), want)
    # 改动条目要能落盘审计
    _, changes = textnorm.for_tts_with_changes("排成 5 行")
    check("改动条目非空", bool(changes), True)
    check("改动条目记规则名", changes[0].get("rule"), "行(háng) 行列读法")
    check("改动条目记原文", changes[0].get("from"), " 行")
    check("改动条目记改写", changes[0].get("to"), " 航")


def case_scenes():
    scenes = [
        {"id": 1, "tts": "第一行代码"},
        {"id": 2, "tts": "没有多音字"},
        {"id": 3, "narration": "排成 5 行"},
    ]
    out, report = textnorm.for_tts_scenes(scenes)
    check("逐镜改写第1镜（TTS 不做阿拉伯化）", out[0]["tts"], "第一航代码")
    check("逐镜改写第3镜（回退 narration）", out[2]["tts"], "排成 5 航"),
    check("逐镜未改动的镜不记报告", [r["scene"] for r in report], [1, 3])
    check("逐镜保留原文", out[0]["tts_original"], "第一行代码")
    check("原列表未被就地修改", scenes[0]["tts"], "第一行代码")


if __name__ == "__main__":
    case_arabic()
    case_whitelist()
    case_caption_batch()
    case_tts_symbols()
    case_polyphones()
    case_scenes()
    failed = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    if failed:
        print("FAILED:", *failed, sep="\n- ")
        raise SystemExit(1)
