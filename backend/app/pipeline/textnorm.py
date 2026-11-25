"""中文数字 / 多音字规范化（skill: B1-3 多音字、B1-4 数字阿拉伯化）。

两套规则，分别服务屏幕与配音：

- ``to_arabic``：字幕与屏幕短文字。数值、数量、序号、编号、数学表达统一成阿拉伯
  数字与规范符号，例如 ``f(1)是0。2人时，0+3 模 2，得1``、``0基／1基``。
- ``for_tts``：配音朗读文本。英文缩写、符号、括号与多音字改成模型能读对的写法，
  屏幕字幕保持正确汉字，例如 ``排成 5 行`` → ``排成 5 航``。

两条硬规则：

1. 只改真正表达数字的内容——白名单里的普通词（统一 / 一致 / 一下 / 十分…）原样保留；
2. 幂等——同一段文本重复套用结果不变。阶段 8 会再套一次 ``for_tts``，用来覆盖
   修改流里用户手工改过的 TTS 文本。

规则表是起点不是终点：不同 TTS 模型仍需试听，未覆盖的多音字应补进规则表。
"""
import re

# ---------- 基础表 ----------

DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
          "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
SMALL_UNITS = {"十": 10, "百": 100, "千": 1000}
BIG_UNITS = {"万": 10 ** 4, "亿": 10 ** 8}
NUMERAL_CHARS = "零〇一二三四五六七八九十百千万亿两"

# 白名单：含数字汉字但并不是数字的普通词，整体不参与转换
PROTECTED_WORDS = [
    "千万不要", "千万别", "万无一失", "一模一样", "五花八门", "七嘴八舌",
    "乱七八糟", "四面八方", "三番五次", "接二连三", "说一不二",
    "统一", "一致", "一般", "一样", "一切", "一旦", "一定", "一共", "一直",
    "一起", "一边", "一下", "一点", "一些", "一眼", "一会", "一阵", "一并",
    "一贯", "一番", "一心", "一概", "一律", "一块", "一头", "一手", "一身",
    "一味", "一连", "一旁", "万一", "唯一", "两全", "两难", "二手", "十分",
    "百分百", "一下下", "一点点",
]

_MATH_OPS = {"除以": "/", "乘以": "×", "加": "+", "减": "-", "乘": "×"}
_MATH_RE = re.compile(rf"(?<=[{NUMERAL_CHARS}])\s*(除以|乘以|加|减|乘)\s*(?=[{NUMERAL_CHARS}])")
_NUMERAL_RE = re.compile(f"[{NUMERAL_CHARS}]+")
_PLACEHOLDER_RE = re.compile("\x00(\\d+)\x00")
_FULLWIDTH_DIGITS = str.maketrans("０１２３４５６７８９", "0123456789")

# 配音端：英文 / 符号 / 括号读法（顺序敏感：O(N) 先于通用括号规则）
_TTS_SYMBOL_RULES = [
    ("O(N) 读法", re.compile(r"\bO\s*\(([^()]*)\)"), lambda m: f"O {m.group(1)}"),
    ("括号读法", re.compile(r"\s*[（(]\s*([^（()）]*?)\s*[)）]"), lambda m: f"，括号，{m.group(1)}"),
    ("TIMEOUT 读法", re.compile(r"\bTIMEOUT\b"), lambda m: "time out"),
    ("OR 读法", re.compile(r"\bOR\b"), lambda m: "or"),
    ("EQ 读法", re.compile(r"\bEQ\b"), lambda m: "E Q"),
    ("标识符下划线", re.compile(r"(?<=[A-Za-z0-9])_(?=[A-Za-z0-9])"), lambda m: " "),
]

# 配音端：多音字同音替换（只在 TTS 文本换字，屏幕字幕保持正确汉字）
_TTS_POLYPHONE_RULES = [
    # 行：数字后面的「行」表示行列，读 háng；默认常被读成 xíng
    ("行(háng) 行列读法", re.compile(rf"(?<=[0-9{NUMERAL_CHARS}])\s*行"),
     lambda m: m.group(0)[:-1] + "航"),
    # 重：重复 / 重新 / 重置 读 chóng
    ("重(chóng) 读法", re.compile(r"重(?=复|新|叠|启|试|置|构|来)"), lambda m: "崇"),
    # 长：增长 / 生长 / 成长 读 zhǎng
    ("长(zhǎng) 读法", re.compile(r"(?<=增|生|成)长"), lambda m: "掌"),
]


# ---------- 数字解析 ----------

def parse_numeral(token: str):
    """把一串中文数字解析成 int；不是规范的数值写法时返回 None。"""
    if not token:
        return None
    if len(token) == 1:
        if token in DIGITS:
            return DIGITS[token]
        if token in SMALL_UNITS:
            return SMALL_UNITS[token]
        return None
    # 纯个位数字连写（「一二三」「两三」）通常是「一二线城市」这类泛指，不转
    if all(ch in DIGITS for ch in token):
        return None
    total = section = number = 0
    for ch in token:
        if ch in DIGITS:
            number = DIGITS[ch]
        elif ch in SMALL_UNITS:
            unit = SMALL_UNITS[ch]
            if number == 0 and unit == 10:  # 「十二」「十五」
                number = 1
            section += number * unit
            number = 0
        elif ch in BIG_UNITS:
            section = (section + number) * BIG_UNITS[ch]
            total += section
            section = number = 0
        else:
            return None
    return total + section + number


def _numeral_repl(match):
    value = parse_numeral(match.group(0))
    return str(value) if value is not None else match.group(0)


def _mask_protected(text: str):
    saved = []
    for word in sorted(PROTECTED_WORDS, key=len, reverse=True):
        while word in text:
            marker = f"\x00{len(saved)}\x00"
            text = text.replace(word, marker, 1)
            saved.append(word)
    return text, saved


def _restore(text: str, saved) -> str:
    def repl(match):
        idx = int(match.group(1))
        return saved[idx] if 0 <= idx < len(saved) else match.group(0)
    return _PLACEHOLDER_RE.sub(repl, text)


# ---------- 字幕端：阿拉伯数字 ----------

def to_arabic(text: str) -> str:
    """字幕/屏幕短文字：中文数字与数学表达转阿拉伯数字与规范符号。

    ``两人时，零加三模二，得一`` → ``2人时，0+3模2，得1``
    """
    if not text:
        return text
    text = text.translate(_FULLWIDTH_DIGITS)
    masked, saved = _mask_protected(text)
    masked = _MATH_RE.sub(lambda m: _MATH_OPS[m.group(1)], masked)
    masked = _NUMERAL_RE.sub(_numeral_repl, masked)
    return _restore(masked, saved)


def to_arabic_many(items) -> tuple[list, list]:
    """批量转换字幕，返回 (新列表, 改动条目)。"""
    out, changes = [], []
    for item in items or []:
        new = to_arabic(str(item))
        if new != item:
            changes.append({"from": str(item), "to": new})
        out.append(new)
    return out, changes


# ---------- 配音端：读法与多音字 ----------

def _apply_rule(text: str, pattern, repl):
    changes = []

    def _sub(match):
        new = repl(match)
        if new != match.group(0):
            changes.append({"from": match.group(0), "to": new})
        return new

    return pattern.sub(_sub, text), changes


def for_tts_with_changes(text: str) -> tuple[str, list]:
    """配音朗读文本规范化，同时返回逐条改动（供落盘审计）。"""
    if not text:
        return text, []
    changes = []
    for rule, pattern, repl in _TTS_SYMBOL_RULES + _TTS_POLYPHONE_RULES:
        text, rule_changes = _apply_rule(text, pattern, repl)
        for item in rule_changes:
            changes.append({"rule": rule, **item})
    return text, changes


def for_tts(text: str) -> str:
    return for_tts_with_changes(text)[0]


def for_tts_scenes(scenes) -> tuple[list, list]:
    """逐镜生成 TTS 文本规范化后的副本；返回 (新 scenes, 报告条目)。"""
    out, report = [], []
    for scene in scenes or []:
        scene = dict(scene)
        text = scene.get("tts") or scene.get("narration") or ""
        new, changes = for_tts_with_changes(text)
        if changes:
            report.append({"scene": scene.get("id"), "changes": changes})
            scene["tts"] = new
            scene["tts_original"] = text
        out.append(scene)
    return out, report
