#!/usr/bin/env python3
"""用失败项目（5bca 拓扑排序）的真实 stage-6 提示词复现 cc 空内容问题，
并验证 thinking=adaptive + output_config.effort=low 是否解决。
直连网关流式请求，统计 text/thinking 字符与 stop_reason。"""
import asyncio
import json
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from app.pipeline import prompts  # noqa: E402

KEY_FILE = Path("/tmp/.cc_key")


async def run(label, extra_payload, max_tokens=32768):
    key = KEY_FILE.read_text().strip()
    timeline = json.loads(Path("/tmp/5bca_timeline.json").read_text())
    prompt = prompts.VISUALS_PROMPT.format(storyboard=json.dumps(timeline, ensure_ascii=False))
    payload = {"model": "claude-opus-5-5", "max_tokens": max_tokens, "temperature": 0.7,
               "stream": True, "messages": [{"role": "user", "content": prompt}]}
    payload.update(extra_payload)
    text_chars = think_chars = 0
    stop = None
    events = {}
    import time
    t0 = time.time()
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(600, connect=30)) as client:
            async with client.stream("POST", "https://api.vera-hub.ai/v1/messages",
                                     json=payload,
                                     headers={"x-api-key": key,
                                              "anthropic-version": "2023-06-01",
                                              "content-type": "application/json"}) as resp:
                print(f"[{label}] HTTP {resp.status_code}")
                if resp.status_code != 200:
                    body = await resp.aread()
                    print(f"[{label}] ERROR: {body.decode(errors='replace')[:300]}")
                    return
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    try:
                        obj = json.loads(line[5:].strip())
                    except json.JSONDecodeError:
                        continue
                    t = obj.get("type")
                    events[t] = events.get(t, 0) + 1
                    if t == "content_block_delta":
                        d = obj.get("delta", {})
                        text_chars += len(d.get("text") or "")
                        think_chars += len(d.get("thinking") or "")
                    elif t == "message_delta":
                        stop = obj.get("delta", {}).get("stop_reason") or stop
                        u = obj.get("usage") or {}
                        if u:
                            print(f"[{label}] usage: {u}")
    except Exception as e:
        print(f"[{label}] 流中断: {type(e).__name__}: {e}")
    el = time.time() - t0
    print(f"[{label}] stop={stop} | text={text_chars} chars | thinking={think_chars} chars | {el:.0f}s")
    print(f"[{label}] events: {events}")


async def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "low"
    if mode == "low":
        await run("adaptive+low", {"thinking": {"type": "adaptive"},
                                   "output_config": {"effort": "low"}})
    elif mode == "plain":
        await run("plain(默认思考)", {})
    elif mode == "medium":
        await run("adaptive+medium", {"thinking": {"type": "adaptive"},
                                      "output_config": {"effort": "medium"}})
    elif mode == "low16k":
        await run("adaptive+low+16k", {"thinking": {"type": "adaptive"},
                                       "output_config": {"effort": "low"}}, max_tokens=16384)


asyncio.run(main())
