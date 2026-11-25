"""公开网页摄取：httpx 抓取 + trafilatura 正文提取，>30k 字符切块。"""
import httpx
import trafilatura

from ..llm import ProviderError

MAX_CHARS_PER_CHUNK = 30000
MAX_TOTAL_CHARS = 120000
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


class IngestError(Exception):
    def __init__(self, kind: str, detail: str):
        self.kind = kind  # network / timeout / http / extract / config / auth
        self.detail = detail
        super().__init__(f"[{kind}] {detail}")


async def fetch_web(url: str) -> dict:
    """返回 {title, text, chunks}；失败抛 IngestError，绝不谎称空内容。"""
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(40, connect=15),
                                     follow_redirects=True,
                                     headers={"User-Agent": _UA}) as client:
            resp = await client.get(url)
    except (httpx.ConnectError, httpx.ConnectTimeout) as e:
        raise IngestError("network", f"无法连接目标站点：{e}")
    except httpx.ReadTimeout as e:
        raise IngestError("timeout", f"读取超时：{e}")
    if resp.status_code != 200:
        kind = "auth" if resp.status_code in (401, 403) else "http"
        raise IngestError(kind, f"HTTP {resp.status_code}（该页面可能需要登录或拒绝抓取）")

    text = trafilatura.extract(resp.text, url=url,
                               include_comments=False, include_tables=True)
    if not text or not text.strip():
        raise IngestError("extract", "正文提取失败：页面可能是纯脚本渲染或需要登录")
    meta = trafilatura.extract_metadata(resp.text)
    title = (meta.title if meta and meta.title else url)[:200]
    text = text.strip()[:MAX_TOTAL_CHARS]
    chunks = [text[i:i + MAX_CHARS_PER_CHUNK]
              for i in range(0, len(text), MAX_CHARS_PER_CHUNK)]
    return {"title": title, "text": text, "chunks": chunks}
