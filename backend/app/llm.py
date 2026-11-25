"""供应商适配层：OpenAI 兼容端点 + Anthropic Messages 端点，统一流式输出。

调用边界（继承 skill 纪律）：
- 鉴权/权限/超时错误如实抛出并标注，绝不记为「无返回内容」。
- 每条产物记录实际生成它的 provider/model，不伪装来源。
"""
import json
from typing import AsyncGenerator, Optional

import httpx


class ProviderError(Exception):
    def __init__(self, kind: str, detail: str, provider: str = ""):
        self.kind = kind  # auth / permission / timeout / http / network / empty
        self.detail = detail
        self.provider = provider
        super().__init__(f"[{kind}] {provider}: {detail}")


def _err_from_response(resp: httpx.Response, name: str) -> ProviderError:
    body = resp.text[:500]
    if resp.status_code in (401, 403):
        kind = "auth"
    elif resp.status_code == 429:
        kind = "rate_limit"
    elif resp.status_code >= 500:
        kind = "http"
    else:
        kind = "http"
    return ProviderError(kind, f"HTTP {resp.status_code}: {body}", name)


async def stream_chat(provider: dict, messages: list,
                      max_tokens: int = 8192,
                      temperature: float = 0.7,
                      system: Optional[str] = None,
                      reasoning_effort: Optional[str] = None,
                      _meta: Optional[dict] = None) -> AsyncGenerator[str, None]:
    """统一入口：按 provider['type'] 分发，逐段 yield 文本 delta。
    reasoning_effort 仅对 OpenAI 兼容端点生效；_meta 回传 finish_reason/推理字符数。"""
    ptype = provider.get("type", "openai")
    if ptype == "anthropic":
        async for d in _stream_anthropic(provider, messages, max_tokens, temperature, system,
                                         reasoning_effort=reasoning_effort, meta=_meta):
            yield d
    else:
        async for d in _stream_openai(provider, messages, max_tokens, temperature, system,
                                      reasoning_effort=reasoning_effort, meta=_meta):
            yield d


async def complete(provider: dict, messages: list, max_tokens: int = 8192,
                   temperature: float = 0.7, system: Optional[str] = None,
                   reasoning_effort: Optional[str] = None) -> str:
    meta: dict = {}
    parts = []
    async for d in stream_chat(provider, messages, max_tokens, temperature, system,
                               reasoning_effort=reasoning_effort, _meta=meta):
        parts.append(d)
    text = "".join(parts)
    # 思考型模型把 max_tokens 全部耗在推理上（finish=length/max_tokens 且正文为空）：
    # 自动以低推理强度重试一次（OpenAI: reasoning_effort=low；Anthropic: thinking disabled）
    if not text.strip() and meta.get("finish") in ("length", "max_tokens") \
            and meta.get("reasoning") and reasoning_effort != "low":
        parts = []
        async for d in stream_chat(provider, messages, max_tokens, temperature, system,
                                   reasoning_effort="low", _meta=meta):
            parts.append(d)
        text = "".join(parts)
    if not text.strip():
        if meta.get("reasoning"):
            extra = (f"（stop={meta.get('finish', '?')}，模型思考消耗约 {meta['reasoning']} 字符"
                     "后预算耗尽，正文未产出）")
        elif meta.get("finish"):
            extra = f"（stop={meta['finish']}，正文未产出）"
        else:
            extra = "（HTTP 200 不代表有产出）"
        raise ProviderError("empty", f"模型返回了空内容{extra}", provider.get("name", ""))
    return text


async def test_provider(p: dict) -> dict:
    """连通性测试：最小请求验证鉴权与网络，返回 ok/latency/error 分类。

    思考型模型（如 kimi-k3）先输出 reasoning_content 再输出正文，
    小 max_tokens 会被推理耗尽造成「空内容」误报；收到推理流同样证明链路可用。
    """
    import time as _t
    t0 = _t.time()
    saw_reasoning: list = []
    meta: dict = {}
    msgs = [{"role": "user", "content": "ping"}]
    try:
        if p.get("type") == "anthropic":
            gen = _stream_anthropic(p, msgs, 512, 0, None, meta=meta)
        else:
            gen = _stream_openai(p, msgs, 512, 0, None, saw_reasoning=saw_reasoning)
        parts = [d async for d in gen]
        text = "".join(parts)
        lat = int((_t.time() - t0) * 1000)
        if text.strip():
            return {"ok": True, "latency_ms": lat, "model_echo": text.strip()[:80]}
        if saw_reasoning or meta.get("reasoning"):
            return {"ok": True, "latency_ms": lat,
                    "model_echo": "（思考型模型：已收到推理流，正文未在测试额度内产出）"}
        raise ProviderError("empty", "模型返回了空内容（HTTP 200 不代表有产出）",
                            p.get("name", ""))
    except ProviderError as e:
        return {"ok": False, "latency_ms": int((_t.time() - t0) * 1000),
                "error": {"kind": e.kind, "detail": e.detail[:300]}}


async def _stream_openai(p, messages, max_tokens, temperature, system,
                         saw_reasoning: Optional[list] = None,
                         reasoning_effort: Optional[str] = None,
                         meta: Optional[dict] = None):
    name = p.get("name", "openai-compat")
    base = p["base_url"].rstrip("/")
    key = p.get("api_key", "")
    msgs = ([{"role": "system", "content": system}] if system else []) + messages
    payload = {"model": p["model"], "messages": msgs, "stream": True,
               "max_tokens": max_tokens, "temperature": temperature}
    if reasoning_effort:
        payload["reasoning_effort"] = reasoning_effort
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(300, connect=30)) as client:
            async with client.stream("POST", f"{base}/chat/completions",
                                     json=payload, headers=headers) as resp:
                if resp.status_code != 200:
                    await resp.aread()
                    raise _err_from_response(resp, name)
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        return
                    try:
                        obj = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    for ch in obj.get("choices", []):
                        delta = ch.get("delta", {})
                        piece = delta.get("content")
                        if piece:
                            yield piece
                        elif saw_reasoning is not None and delta.get("reasoning_content"):
                            saw_reasoning.append(True)
                        if meta is not None:
                            meta["reasoning"] = meta.get("reasoning", 0) + len(delta.get("reasoning_content") or "")
                            if ch.get("finish_reason"):
                                meta["finish"] = ch["finish_reason"]
    except (httpx.ConnectError, httpx.ConnectTimeout) as e:
        raise ProviderError("network", str(e), name)
    except httpx.ReadTimeout as e:
        raise ProviderError("timeout", str(e), name)


async def _stream_anthropic(p, messages, max_tokens, temperature, system,
                            reasoning_effort: Optional[str] = None,
                            meta: Optional[dict] = None):
    name = p.get("name", "anthropic")
    base = p["base_url"].rstrip("/")
    key = p.get("api_key", "")
    # 新型 claude（如 claude-opus-5-5）只支持 adaptive 思考：thinking 只能省略或
    # {"type":"adaptive"} + output_config.effort（实测 disabled/enabled 均被网关拒绝，
    # 省略时默认思考，复杂任务会烧光 max_tokens 导致正文为空）。
    # reasoning_effort=low/medium/high → adaptive + 对应 effort；网关 400 拒绝则省略参数重试。
    thinking_ladder = [None]
    if reasoning_effort in ("low", "medium", "high"):
        thinking_ladder = [
            ({"type": "adaptive"}, {"effort": reasoning_effort}),
            None,
        ]
    last_err: Optional[ProviderError] = None
    for candidate in thinking_ladder:
        payload = {"model": p["model"], "messages": messages, "max_tokens": max_tokens,
                   "temperature": temperature, "stream": True}
        if system:
            payload["system"] = system
        if candidate:
            thinking, output_config = candidate
            payload["thinking"] = thinking
            payload["output_config"] = output_config
        headers = {"x-api-key": key, "anthropic-version": "2023-06-01",
                   "content-type": "application/json"}
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(300, connect=30)) as client:
                async with client.stream("POST", f"{base}/v1/messages",
                                         json=payload, headers=headers) as resp:
                    if resp.status_code != 200:
                        await resp.aread()
                        # 网关/模型不支持该 thinking 形态（400 且报错提及 thinking）→ 试下一档
                        if (resp.status_code == 400 and candidate is not None
                                and "thinking" in resp.text.lower()):
                            last_err = _err_from_response(resp, name)
                            continue
                        raise _err_from_response(resp, name)
                    async for line in resp.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        try:
                            obj = json.loads(line[5:].strip())
                        except json.JSONDecodeError:
                            continue
                        otype = obj.get("type")
                        if otype == "content_block_delta":
                            delta = obj.get("delta", {})
                            piece = delta.get("text")
                            if piece:
                                yield piece
                            elif meta is not None:
                                think = delta.get("thinking") or ""
                                meta["reasoning"] = meta.get("reasoning", 0) + len(think)
                        elif otype == "message_delta" and meta is not None:
                            stop = obj.get("delta", {}).get("stop_reason")
                            if stop:
                                meta["finish"] = stop
                    return
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            raise ProviderError("network", str(e), name)
        except httpx.ReadTimeout as e:
            raise ProviderError("timeout", str(e), name)
    if last_err is not None:
        raise last_err
