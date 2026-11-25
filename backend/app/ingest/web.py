"""公开网页摄取：httpx 抓取 + trafilatura 正文提取，>30k 字符切块。

SSRF 防护（C-SEC-05）：抓取前与每一跳重定向都解析目标 IP，拒绝内网/环回/链路本地/
云元数据等非公网地址；关闭 httpx 自动重定向，最多跟随 3 跳。
"""
import ipaddress
import socket
from urllib.parse import urljoin, urlparse

import httpx
import trafilatura

from ..llm import ProviderError

MAX_CHARS_PER_CHUNK = 30000
MAX_TOTAL_CHARS = 120000
MAX_REDIRECTS = 3
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


class IngestError(Exception):
    def __init__(self, kind: str, detail: str):
        self.kind = kind  # network / timeout / http / extract / config / auth
        self.detail = detail
        super().__init__(f"[{kind}] {detail}")


def _ip_is_blocked(ip) -> bool:
    """非全球可达地址一律拒绝（私有/环回/链路本地/保留/组播/未指定/云元数据 169.254.169.254）。"""
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return not ip.is_global


def assert_public_http_url(url: str) -> None:
    """校验 scheme 与解析后的每个目标 IP；不合法抛 IngestError(kind='config')。"""
    try:
        parsed = urlparse(url)
    except ValueError as e:
        raise IngestError("config", f"URL 非法：{e}")
    if parsed.scheme not in ("http", "https"):
        raise IngestError("config", "仅支持 http/https 链接")
    host = parsed.hostname
    if not host:
        raise IngestError("config", "URL 缺少主机名")
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        raise IngestError("config", "URL 端口非法")
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise IngestError("network", f"域名解析失败：{e}")
    if not infos:
        raise IngestError("network", f"域名无法解析：{host}")
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            raise IngestError("config", f"无法识别的目标地址：{info[4][0]}")
        if _ip_is_blocked(ip):
            raise IngestError("config",
                              f"拒绝访问内网或保留地址：{host} → {ip}（SSRF 防护）")


async def fetch_web(url: str) -> dict:
    """返回 {title, text, chunks}；失败抛 IngestError，绝不谎称空内容。"""
    assert_public_http_url(url)
    current = url
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(40, connect=15),
                                     follow_redirects=False,
                                     headers={"User-Agent": _UA}) as client:
            for hop in range(MAX_REDIRECTS + 1):
                resp = await client.get(current)
                if not (300 <= resp.status_code < 400):
                    break
                loc = resp.headers.get("location")
                if not loc:
                    raise IngestError("http", f"HTTP {resp.status_code} 但缺少 Location 头")
                if hop >= MAX_REDIRECTS:
                    raise IngestError("http", f"重定向超过 {MAX_REDIRECTS} 次，已放弃")
                current = urljoin(current, loc)
                # 重定向目标重新解析校验：防止 3xx 跳到 127.0.0.1 / 169.254.169.254
                assert_public_http_url(current)
    except (httpx.ConnectError, httpx.ConnectTimeout) as e:
        raise IngestError("network", f"无法连接目标站点：{e}")
    except httpx.ReadTimeout as e:
        raise IngestError("timeout", f"读取超时：{e}")
    if resp.status_code != 200:
        kind = "auth" if resp.status_code in (401, 403) else "http"
        raise IngestError(kind, f"HTTP {resp.status_code}（该页面可能需要登录或拒绝抓取）")

    text = trafilatura.extract(resp.text, url=current,
                               include_comments=False, include_tables=True)
    if not text or not text.strip():
        raise IngestError("extract", "正文提取失败：页面可能是纯脚本渲染或需要登录")
    meta = trafilatura.extract_metadata(resp.text)
    title = (meta.title if meta and meta.title else current)[:200]
    text = text.strip()[:MAX_TOTAL_CHARS]
    chunks = [text[i:i + MAX_CHARS_PER_CHUNK]
              for i in range(0, len(text), MAX_CHARS_PER_CHUNK)]
    return {"title": title, "text": text, "chunks": chunks}
