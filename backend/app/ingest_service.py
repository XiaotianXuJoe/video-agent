"""资料摄取共用流程：抓取 → LLM 结构化摘要 → 入库。供 REST 端点与对话流共用。"""
import json

from . import store
from .ingest import fetch_web, is_feishu_url, fetch_feishu, IngestError  # noqa: F401
from .llm import complete
from .pipeline.machine import extract_json
from .pipeline.prompts import INGEST_SUMMARY, INGEST_MERGE


async def ingest_url(url: str) -> dict:
    """返回入库后的 reference dict（含 provider 标注）。失败抛 IngestError/ValueError。

    飞书链接双通道：配了应用凭据优先走 OpenAPI（完整正文）；
    无凭据或 API 失败时回退公开页抓取（互联网可见文档的 SSR 正文，长文可能只有前部）。
    """
    fetched = None
    via = "web"
    api_err = None
    if is_feishu_url(url):
        fs = store.load_settings().get("feishu", {})
        if fs.get("app_id") and fs.get("app_secret"):
            try:
                fetched = await fetch_feishu(url, fs["app_id"], fs["app_secret"])
                via = "feishu"
            except IngestError as e:
                api_err = e  # 保留 API 错误，公开页也失败时抛出它（信息量更大）
        if fetched is None:
            try:
                fetched = await fetch_web(url)
                via = "feishu_public"
            except IngestError:
                if api_err:
                    raise api_err
                raise
    else:
        fetched = await fetch_web(url)

    provider = store.provider_for_key("ingest")
    if not provider:
        raise IngestError("config", "未配置任何大模型供应商，无法生成资料摘要")

    partials = []
    for chunk in fetched["chunks"]:
        text = await complete(provider,
                              [{"role": "user", "content":
                                INGEST_SUMMARY.format(source_url=url, content=chunk)}],
                              max_tokens=2048, temperature=0.3)
        partials.append(extract_json(text))
    if len(partials) == 1:
        data = partials[0]
    else:
        merged_text = await complete(
            provider,
            [{"role": "user", "content": INGEST_MERGE.format(
                n=len(partials),
                summaries=json.dumps(partials, ensure_ascii=False)[:20000])}],
            max_tokens=2048, temperature=0.3)
        data = extract_json(merged_text)

    rid = store.create_reference(
        title=data.get("title") or fetched["title"],
        source_url=url, ingested_via=via,
        summary=data.get("summary", ""),
        key_points=data.get("key_points", []),
        usable_facts=data.get("usable_facts", []),
        suggested_angles=data.get("suggested_angles", []),
        raw_chars=len(fetched["text"]))
    ref = store.get_reference(rid)
    ref["provider"] = provider.get("name", "")
    return ref


def refs_block_for_prompt(pid: str) -> str:
    """生成注入阶段 3/4 提示词的参考资料文本块；无资料时返回空串。"""
    from .pipeline.prompts import REFERENCES_BLOCK
    refs = store.project_references(pid)
    if not refs:
        return ""
    lines = []
    for r in refs:
        lines.append(f"- 《{r['title']}》（{r['source_url']}）：{r['summary']}")
        for f_ in r["usable_facts"][:8]:
            lines.append(f"  · {f_}")
    return REFERENCES_BLOCK.format(refs="\n".join(lines))
