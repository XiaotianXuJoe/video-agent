"""视觉代码静态检查（skill B2-1 禁镜号 / B2-2·B1-7 标题统一规范）。

两类结论：

- ``errors``：画面里绘制了镜号/分镜序号（「镜1」「06/08」「分镜2」）——skill 明确禁止，
  出现即判定失败，交给阶段 6 的自动带错修复；
- ``warnings``：顶部标题（y<110 的绘制）字号或坐标不一致 / 不是 36px——不阻断生成，
  但会在冒烟输出与日志里提示，避免每个镜头各写一套标题。

纯静态 AST 分析，不执行被检查的代码，因此可以安全地跑在任何生成的 visuals.py 上。
"""
import ast
import re

FORBIDDEN_RE = re.compile(r"镜\s*\d+|分镜\s*\d*")
TITLE_BAND_Y = 110      # y < 110 视为顶部标题带
TITLE_SIZE = 36         # skill 规定的标题字号
TEXT_CALLS = {"text", "multiline_text", "draw_text"}


def _call_name(node) -> str:
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return ""


def _text_calls(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _call_name(node) in TEXT_CALLS:
            yield node


def _literal_strings(node):
    """收集节点里的字面字符串；f-string 只取其中的常量片段（如 f"镜{n}" 的「镜」）。"""
    out = []
    for sub in ast.walk(node):
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            out.append(sub.value)
    return out


def _const_pair(node):
    """取 ((x, y)) 这类常量坐标；取不到返回 None。"""
    if isinstance(node, (ast.Tuple, ast.List)) and len(node.elts) == 2:
        values = []
        for elt in node.elts:
            if isinstance(elt, ast.Constant) and isinstance(elt.value, (int, float)):
                values.append(float(elt.value))
            else:
                return None
        return tuple(values)
    return None


def _font_sizes(tree):
    """模块级字体变量名 → 字号（如 TITLE_FONT = ImageFont.truetype(path, 36)）。"""
    sizes = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            targets = [node.target.id]
        else:
            continue
        if not targets or not isinstance(node.value, ast.Call):
            continue
        for sub in ast.walk(node.value):
            if isinstance(sub, ast.Constant) and isinstance(sub.value, (int, float)):
                sizes[targets[0]] = int(sub.value)
                break
    return sizes


def _call_font_size(call, sizes):
    """从 text(...) 调用的 font 关键字里解析字号；解析不到返回 None。"""
    for kw in call.keywords:
        if kw.arg != "font":
            continue
        value = kw.value
        if isinstance(value, ast.Name):
            return sizes.get(value.id)
        if isinstance(value, ast.Call):
            for sub in ast.walk(value):
                if isinstance(sub, ast.Constant) and isinstance(sub.value, (int, float)):
                    return int(sub.value)
    return None


def lint_visuals_source(source: str) -> dict:
    """返回 {"errors": [...], "warnings": [...], "title_draws": [...]}。"""
    result = {"errors": [], "warnings": [], "title_draws": []}
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        result["errors"].append(f"语法错误，无法静态检查: {exc}")
        return result

    # 1) 镜号 / 分镜序号：任何文本绘制调用都不许出现；模块级字符串常量同样不许
    for call in _text_calls(tree):
        positional = list(call.args[1:])  # 第一个位置参数是坐标
        for arg in positional:
            for text in _literal_strings(arg):
                if FORBIDDEN_RE.search(text):
                    result["errors"].append(
                        f"画面中出现镜号/分镜序号：{text!r}（skill 规定画面任何位置不出现分镜序号）")
    for node in tree.body:
        if isinstance(node, ast.Assign):
            value = node.value
        elif isinstance(node, ast.AnnAssign):
            value = node.value
        else:
            continue
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            if FORBIDDEN_RE.search(value.value):
                result["errors"].append(
                    f"模块级文本常量含镜号/分镜序号：{value.value!r}（不要预置这类标题）")

    # 2) 顶部标题一致性：y<110 的文本绘制应使用同一坐标、同一 36px 字号
    sizes = _font_sizes(tree)
    coords, fonts = [], []
    for call in _text_calls(tree):
        if not call.args:
            continue
        pos = _const_pair(call.args[0])
        if not pos or pos[1] >= TITLE_BAND_Y:
            continue
        size = _call_font_size(call, sizes)
        coords.append(pos)
        fonts.append(size)
        result["title_draws"].append({"pos": pos, "font_size": size})
    if coords:
        if len(set(coords)) > 1:
            result["warnings"].append(
                f"顶部标题坐标不一致：{sorted(set(coords))}（应统一到同一坐标，如 (110, 58)）")
        known = [s for s in fonts if s is not None]
        if len(set(known)) > 1:
            result["warnings"].append(
                f"顶部标题字号不一致：{sorted(set(known))}（应统一为 {TITLE_SIZE}px 粗体）")
        elif known and set(known) != {TITLE_SIZE}:
            result["warnings"].append(
                f"顶部标题字号为 {sorted(set(known))}，skill 规定统一 {TITLE_SIZE}px")
    return result


def summary(lint: dict) -> str:
    parts = list(lint["errors"]) + [f"（告警）{w}" for w in lint["warnings"]]
    return "；".join(parts)


if __name__ == "__main__":
    import json
    import sys
    from pathlib import Path

    if len(sys.argv) < 2:
        raise SystemExit("用法: python3 visuals_lint.py path/to/visuals.py")
    print(json.dumps(lint_visuals_source(Path(sys.argv[1]).read_text(encoding="utf-8")),
                     ensure_ascii=False, indent=2))
