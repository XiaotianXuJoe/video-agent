"""落盘前脱敏（D4 / C-SEC-08）：API key、Bearer 凭据、长 hex 串统一掩码。

所有写入聊天记录（store.add_message）与原始响应文件（raw_responses/*.txt）
的文本都应先经这里处理——上游错误体可能回显请求头/凭据片段，一旦明文
落盘就永久留在 DB 与项目目录里。
"""
import re

_PATTERNS = [
    # sk- / sk_live_ / api key 形态
    (re.compile(r"\b(sk[-_][A-Za-z0-9]{6,})"), "***REDACTED***"),
    # Bearer <token>
    (re.compile(r"(?i)(bearer)\s+[A-Za-z0-9._~+/=-]{8,}"), r"\1 ***REDACTED***"),
    # Authorization: xxx
    (re.compile(r"(?i)(authorization[\"'\s:=]+)[A-Za-z0-9._~+/=-]{8,}"), r"\1***REDACTED***"),
    # 40+ 位连续 hex（常见密钥形态）
    (re.compile(r"(?<![0-9a-fA-F])[0-9a-fA-F]{40,}(?![0-9a-fA-F])"), "***REDACTED***"),
]


def redact(text: str) -> str:
    """返回脱敏后的文本（保留可读性，只掩码凭据模式）。"""
    if not text:
        return text
    out = text
    for pat, repl in _PATTERNS:
        out = pat.sub(repl, out)
    return out
