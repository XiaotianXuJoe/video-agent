"""飞书文档摄取：app_id/app_secret → tenant_access_token → docx raw_content。

支持 /docx/、/docs/、/wiki/ 三种链接形态；wiki 先解析 node 取 obj_token。
鉴权与权限失败如实抛出，绝不记为「文档为空」。
"""
import re

import httpx

from .web import IngestError, MAX_CHARS_PER_CHUNK, MAX_TOTAL_CHARS

_FEISHU_HOSTS = ("feishu.cn", "larksuite.com")
API = "https://open.feishu.cn/open-apis"


def is_feishu_url(url: str) -> bool:
    return any(h in url for h in _FEISHU_HOSTS)


def _parse_doc_ref(url: str) -> dict:
    m = re.search(r"/docx/([A-Za-z0-9]+)", url)
    if m:
        return {"kind": "docx", "token": m.group(1)}
    m = re.search(r"/docs/([A-Za-z0-9]+)", url)
    if m:
        return {"kind": "docs", "token": m.group(1)}
    m = re.search(r"/wiki/([A-Za-z0-9]+)", url)
    if m:
        return {"kind": "wiki", "token": m.group(1)}
    raise IngestError("config", "无法从链接解析飞书文档标识（支持 /docx/、/docs/、/wiki/）")


async def _tenant_token(app_id: str, app_secret: str) -> str:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(f"{API}/auth/v3/tenant_access_token/internal",
                                 json={"app_id": app_id, "app_secret": app_secret})
    data = resp.json()
    if data.get("code") != 0:
        raise IngestError("auth", f"飞书应用鉴权失败：{data.get('msg', '未知错误')}"
                                  "（请检查设置页 App ID / App Secret）")
    return data["tenant_access_token"]


async def _get(client, path, token, err_prefix):
    resp = await client.get(f"{API}{path}",
                            headers={"Authorization": f"Bearer {token}"})
    data = resp.json()
    if data.get("code") != 0:
        code = data.get("code")
        kind = "auth" if code in (99991663, 99991664, 99991671) else "http"
        raise IngestError(kind, f"{err_prefix}：{data.get('msg', '未知错误')}"
                                f"（code={code}，请确认应用已添加文档权限且文档对该应用可见）")
    return data


async def fetch_feishu(url: str, app_id: str, app_secret: str) -> dict:
    if not app_id or not app_secret:
        raise IngestError("config", "未配置飞书应用凭据（设置页 → 飞书集成）")
    ref = _parse_doc_ref(url)
    token = await _tenant_token(app_id, app_secret)
    async with httpx.AsyncClient(timeout=30) as client:
        if ref["kind"] == "wiki":
            node = await _get(client, f"/wiki/v2/spaces/get_node?token={ref['token']}",
                              token, "解析知识库节点失败")
            obj = node["data"]["node"]
            ref = {"kind": obj.get("obj_type", "docx"), "token": obj["obj_token"]}
        if ref["kind"] == "docs":
            # 旧版 docs：走 docx 接口通常兼容；不行再提示
            data = await _get(client, f"/docx/v1/documents/{ref['token']}/raw_content",
                              token, "读取飞书文档失败")
        else:
            data = await _get(client, f"/docx/v1/documents/{ref['token']}/raw_content",
                              token, "读取飞书文档失败")
    content = (data.get("data") or {}).get("content", "")
    if not content.strip():
        raise IngestError("extract", "飞书文档返回了空内容（文档可能无正文或权限仅到标题）")
    # raw_content 首行通常是标题
    lines = [ln for ln in content.splitlines() if ln.strip()]
    title = lines[0][:200] if lines else url
    text = content.strip()[:MAX_TOTAL_CHARS]
    chunks = [text[i:i + MAX_CHARS_PER_CHUNK]
              for i in range(0, len(text), MAX_CHARS_PER_CHUNK)]
    return {"title": title, "text": text, "chunks": chunks}
