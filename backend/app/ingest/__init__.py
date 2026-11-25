"""资料摄取（M6）：公开网页与飞书文档抓取 → LLM 结构化摘要。"""
from .web import fetch_web, IngestError  # noqa: F401
from .feishu import is_feishu_url, fetch_feishu  # noqa: F401
