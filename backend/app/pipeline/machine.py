"""流水线状态机：12 阶段 + 4 门禁 + 三档分步路由 + 断点续跑 + 事件总线。

纪律（继承 skill）：
- 确定性步骤（校验/关键帧/TTS/渲染/QC）只调工具，不调模型；
- 每条产物记录真实来源；接口失败如实标注；
- 旧版不覆盖（产物注册表自动版本递增）。
"""
import asyncio
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from .. import artifacts, store
from ..llm import complete, ProviderError
from ..procsafe import preexec_limits as _preexec_limits, subprocess_env as _subprocess_env, \
    kill_process_tree as _kill_tree
from . import prompts, textnorm

TOOLS = Path(__file__).resolve().parent.parent / "tools"

# 渲染进度行（C-UX-04）：`scene 03: 1200/3400` 与 `scene 03: DONE 3400 frames`
PROGRESS_FRAME_RE = re.compile(r"scene\s+(\d+)\s*:\s*(\d+)\s*/\s*(\d+)")
PROGRESS_DONE_RE = re.compile(r"scene\s+(\d+)\s*:\s*DONE\s+(\d+)")

STAGE_NAMES = {
    1: "需求澄清", 2: "算法校验", 3: "创意方案", 4: "讲稿分镜",
    5: "事实校对", 6: "视觉代码", 7: "关键帧预览", 8: "配音合成",
    9: "时间轴组装", 10: "渲染合成", 11: "QC 验收", 12: "修改",
}
TIER_STAGES = {"concept": (2, 3), "storyboard": (2, 5), "video": (2, 11)}
GATES = {3: "创意确认", 5: "分镜确认", 7: "关键帧确认", 11: "成片审片"}
STAGE_MODEL_KEY = {3: "concept", 4: "storyboard", 5: "factcheck", 6: "visuals"}


class EventBus:
    """项目级事件总线：运行中的流水线发布事件，SSE 订阅消费。

    D1：每个事件同时落 SQLite（project_events 表，seq 递增），
    断线/刷新/重启后可按游标完整回放；内存历史限量（C-PERF-07）。"""

    def __init__(self):
        self._subs = {}
        self._history = {}

    def publish(self, project_id: str, event: dict):
        # 落表失败不阻断流水线：事件仍以内存方式分发给在线订阅者
        try:
            seq = store.append_project_event(project_id, event["event"], event.get("data"))
            event["seq"] = seq
        except Exception:
            pass
        h = self._history.setdefault(project_id, [])
        h.append(event)
        del h[:-500]  # 内存历史限量；完整历史在 DB
        for q in self._subs.get(project_id, []):
            q.put_nowait(event)

    def subscribe(self, project_id: str) -> asyncio.Queue:
        q = asyncio.Queue()
        self._subs.setdefault(project_id, []).append(q)
        return q

    def unsubscribe(self, project_id: str, q: asyncio.Queue):
        if project_id in self._subs and q in self._subs[project_id]:
            self._subs[project_id].remove(q)

    def history(self, project_id: str, after: int = 0):
        """从 DB 回放事件（含重启前）；after 为断线续传游标。"""
        return [{"event": e["type"], "data": e["payload"], "seq": e["seq"], "ts": e["ts"]}
                for e in store.list_project_events(project_id, after)]


BUS = EventBus()
RUNNING = {}  # project_id -> asyncio.Task


def _state_path(proj_dir: Path) -> Path:
    return proj_dir / "pipeline_state.json"


def load_state(proj_dir: Path) -> dict:
    """读取流水线状态；JSON 损坏时备份坏文件并返回 failed（D3/C-REL-05，
    不再抛 JSONDecodeError 导致项目打不开）。"""
    p = _state_path(proj_dir)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            backup = p.with_suffix(f".corrupt-{int(time.time())}.json")
            try:
                os.replace(p, backup)
            except OSError:
                pass
            return {"stage": 1, "status": "failed", "gate": None, "tier": None,
                    "selected_direction": None, "log": [],
                    "error": f"状态文件损坏已备份为 {backup.name}，可从任意阶段重跑"}
    return {"stage": 1, "status": "created", "gate": None, "tier": None,
            "selected_direction": None, "log": []}


def save_state(proj_dir: Path, state: dict):
    """原子写（tmp + os.replace）：写一半被 kill 也不会留下截断的 JSON。"""
    p = _state_path(proj_dir)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)


def _read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, obj):
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_raw(proj_dir: Path, name: str, text: str):
    """原始响应落盘前统一脱敏（D4/C-SEC-08）：上游错误体可能回显凭据片段。"""
    from ..redact import redact
    d = proj_dir / "raw_responses"
    d.mkdir(exist_ok=True)
    (d / name).write_text(redact(text), encoding="utf-8")


def extract_json(text: str):
    """从模型输出稳健提取 JSON：剥围栏、找首尾括号。"""
    t = text.strip()
    t = re.sub(r"^```(json)?\s*|\s*```$", "", t, flags=re.MULTILINE).strip()
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        pass
    start = t.find("{")
    end = t.rfind("}")
    if start >= 0 and end > start:
        return json.loads(t[start:end + 1])
    raise ValueError(f"模型输出不是有效 JSON: {t[:200]}")


def provider_for(stage_no: int) -> dict:
    key = STAGE_MODEL_KEY.get(stage_no, "clarify")
    settings = store.load_settings()
    pid = settings["stage_models"].get(key)
    if not pid:
        raise ProviderError("config", f"阶段「{STAGE_NAMES[stage_no]}」未配置模型，请到设置页选择供应商")
    p = store.get_provider(pid)
    if not p:
        raise ProviderError("config", f"供应商配置不存在: {pid}")
    if not p.get("api_key"):
        raise ProviderError("auth", f"供应商「{p.get('name')}」未填写 API Key")
    return p


MAX_VISUALS_FIX = 2  # 视觉代码自动带错修复上限轮数


def bgm_config() -> tuple[Path | None, float, str]:
    """解析 BGM 配置，返回 (文件路径, 音量, 说明)。

    BGM 文件缺失时自动关闭该功能并给出说明（不阻断渲染）——skill 要求「不存在时
    自动关闭并在验收说明」，而不是让整条流水线失败。路径只取文件名，防目录穿越。
    """
    cfg = store.load_settings().get("bgm") or {}
    volume = float(cfg.get("volume", 0.08))
    if not 0 < volume <= 1:
        volume = 0.08
    if not cfg.get("enabled", True):
        return None, volume, "设置页已关闭 BGM"
    name = Path(str(cfg.get("path") or "bgm.mp3")).name
    path = store.ASSETS_DIR / name
    if not path.exists():
        return None, volume, f"未找到 BGM 文件 assets/{name}，本次不混入 BGM（功能已自动关闭）"
    return path, volume, f"BGM：assets/{name}，衬底音量 {volume}"


# ---------- 子进程加固（C-SEC-04 / C-SEC-07） ----------

# 子进程加固：_preexec_limits / _subprocess_env 由 app.procsafe 统一提供
# （POSIX resource 限额 + env 白名单；见文件顶部 import）

def _require_code_exec(stage: int):
    """设置页关闭「允许执行生成代码」时，阶段 6/7/10 直接失败并给出开启指引。"""
    settings = store.load_settings()
    if not (settings.get("security") or {}).get("allow_generated_code_exec", True):
        raise ProviderError(
            "config",
            f"阶段「{STAGE_NAMES[stage]}」需要执行生成的代码，但设置页已关闭"
            "「允许执行生成代码」。请到设置页开启后重试。")


def strip_code_fences(raw: str) -> str:
    code = raw.strip()
    if code.startswith("```"):
        code = re.sub(r"^```(python)?\s*", "", code)
        code = re.sub(r"\s*```$", "", code)
    return code


async def check_visuals_code(proj_dir: Path, code: str) -> str | None:
    """视觉代码三级自检：编译 → 子进程冒烟渲染（含导入 + 每镜 3 时间点真实渲染）。
    通过返回 None；失败返回可读错误描述（供自动修复提示词与报错使用）。
    这里会执行生成代码，因此同样受「允许执行生成代码」开关约束（含修改流的自检）。"""
    _require_code_exec(6)
    try:
        compile(code, "visuals.py", "exec")
    except SyntaxError as e:
        return f"语法错误: {e}"
    (proj_dir / "visuals.py").write_text(code, encoding="utf-8")
    proc = await asyncio.create_subprocess_exec(
        sys.executable, str(TOOLS / "visuals_smoke.py"), "--project", str(proj_dir),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, cwd=proj_dir,
        env=_subprocess_env(), preexec_fn=_preexec_limits())
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=300)
    except asyncio.TimeoutError:
        _kill_tree(proc)
        return "冒烟渲染超时（300s）"
    except asyncio.CancelledError:
        _kill_tree(proc)
        raise
    if proc.returncode == 0:
        return None
    tail = (out.decode(errors="replace").strip().splitlines() or [""])[-1]
    try:
        detail = json.loads(tail)
        return (f"冒烟渲染失败（{detail.get('phase')}，镜头 {detail.get('scene', '-')} "
                f"{detail.get('pos', '')}）: {str(detail.get('error', ''))[-400:]}")
    except json.JSONDecodeError:
        return f"冒烟渲染失败: {(err.decode(errors='replace') or tail)[-400:]}"


async def visuals_with_autofix(proj_dir: Path, provider: dict, code: str,
                               emit=None) -> tuple[str, int]:
    """自检 + 自动带错修复（至多 MAX_VISUALS_FIX 轮），返回 (最终代码, 修复轮数)。
    修复耗尽仍失败则抛 RuntimeError；最后一次失败代码保留在 visuals.py 供人工排查。"""
    fix_rounds = 0
    while True:
        err = await check_visuals_code(proj_dir, code)
        if err is None:
            return code, fix_rounds
        if fix_rounds >= MAX_VISUALS_FIX:
            raise RuntimeError(
                f"visuals.py 自检未通过且自动修复 {MAX_VISUALS_FIX} 轮后仍失败: {err}"
                "（最后一次失败的代码已保留在 visuals.py，可查看后手动修复或重跑本阶段）")
        fix_rounds += 1
        if emit:
            emit(f"冒烟自检未通过，自动带错修复（第 {fix_rounds}/{MAX_VISUALS_FIX} 轮）：{err[:120]}")
        raw = await complete(provider, [{"role": "user", "content": prompts.VISUALS_FIX_PROMPT.format(
            code=code, error=err)}], max_tokens=32768, reasoning_effort="low")
        _write_raw(proj_dir, f"visuals_fix{fix_rounds}_{int(time.time())}.txt", raw)
        code = strip_code_fences(raw)


class Pipeline:
    def __init__(self, project_id: str):
        self.pid = project_id
        self.dir = store.project_dir(project_id)
        self.state = load_state(self.dir)

    def emit(self, event: dict):
        event["ts"] = time.time()
        BUS.publish(self.pid, event)
        self._persist_note(event)

    def _persist_note(self, event: dict):
        """把关键事件写成会话消息——只由执行者写一次（D3/C-REL-09），
        多标签/多订阅者不再重复落盘；重进会话可见完整执行记录。"""
        kind = event.get("event")
        d = event.get("data") or {}
        note = None
        if kind == "stage_event" and d.get("status") == "completed":
            note = f"✔ 阶段「{d.get('name')}」完成（{d.get('elapsed', '?')}s）"
        elif kind == "gate":
            note = f"⏸️ 流水线暂停：需要人工确认「{d.get('name')}」。"
        elif kind == "pipeline_done":
            note = "✅ 流水线执行完毕。"
        elif kind == "error":
            note = (f"❌ 执行失败（{d.get('kind')}）："
                    f"{str(d.get('message', ''))[:600]}")
        elif kind == "interrupted":
            note = "⏹️ 已手动中断，可从流水线面板任意节点继续。"
        if note:
            try:
                store.add_project_note(self.pid, note)
            except Exception:
                pass  # 落盘失败不影响流水线主流程

    def emit_stage(self, stage: int, status: str, **kw):
        self.emit({"event": "stage_event", "data": {
            "stage": stage, "name": STAGE_NAMES[stage], "status": status, **kw}})

    def save(self):
        save_state(self.dir, self.state)
        store.update_project(self.pid, state=self.state["status"],
                             stage=self.state["stage"])

    # ---------- 入口 ----------

    async def run_tier(self, tier: str):
        """按档位运行：concept/storyboard/video。门禁处暂停。"""
        start, end = TIER_STAGES[tier]
        self.state["tier"] = tier
        cur = max(self.state.get("stage", 1), start)
        await self._run_range(cur, end)

    async def resume(self, selected_direction=None):
        """门禁确认后续跑：按当前档位剩余范围，或升级档位。
        若此前状态是 failed，则重跑失败阶段本身而不是跳过它。"""
        if selected_direction is not None:
            self.state["selected_direction"] = selected_direction
        was_failed = self.state.get("status") == "failed"
        was_interrupted = bool(self.state.pop("interrupted", None))
        self.state.pop("error", None)
        self.state["gate"] = None
        self.state["status"] = "running"
        self.save()
        tier = self.state.get("tier") or "video"
        _, end = TIER_STAGES[tier]
        # 失败或中断续跑：重跑当前阶段（它没完成）；正常门禁确认：从下一阶段继续
        start = self.state["stage"] if (was_failed or was_interrupted) else self.state["stage"] + 1
        await self._run_range(start, end)

    async def upgrade_tier(self, tier: str):
        """逐档续跑：如创意档 → 分镜档 → 成片档。"""
        self.state["tier"] = tier
        self.state["gate"] = None
        self.state["status"] = "running"
        self.state.pop("error", None)
        self.save()
        _, end = TIER_STAGES[tier]
        await self._run_range(self.state["stage"] + 1, end)

    def _rollback_stage(self, prev_stage: int, failed_stage: int):
        """手动单点执行失败：stage 回写执行前位置，失败尝试单独记录（D3/C-REL-06），
        不丢「原来停在哪个门禁/阶段」的信息。"""
        self.state["stage"] = prev_stage
        self.state["last_failed_stage"] = failed_stage

    async def run_one_stage(self, stage: int):
        """手动单点执行：重跑指定阶段后停下。门禁阶段执行完进入待确认，其余进入 paused。"""
        if not (2 <= stage <= 11):
            raise ValueError(f"阶段必须在 2-11 之间，收到 {stage}")
        prev_stage = self.state.get("stage", 1)
        self.state["stage"] = stage
        self.state["status"] = "running"
        self.state["gate"] = None
        self.state.pop("interrupted", None)
        self.save()
        self.emit_stage(stage, "started")
        t0 = time.time()
        try:
            await self._run_stage(stage)
        except asyncio.CancelledError:
            await self._pause_interrupted()
            raise
        except ProviderError as e:
            self._rollback_stage(prev_stage, stage)
            await self._fail(e.kind, str(e))
            return
        except Exception as e:
            self._rollback_stage(prev_stage, stage)
            await self._fail("stage_error", f"{type(e).__name__}: {e}")
            return
        self.emit_stage(stage, "completed", elapsed=round(time.time() - t0, 1))
        self.state.pop("error", None)  # 成功后清除陈旧报错，避免面板误导
        if stage in GATES:
            self.state["status"] = "awaiting_gate"
            self.state["gate"] = {"stage": stage, "name": GATES[stage]}
            self.save()
            self.emit({"event": "gate", "data": self.state["gate"]})
        else:
            self.state["status"] = "paused"
            self.save()
            self.emit({"event": "pipeline_done",
                       "data": {"tier": self.state.get("tier"), "manual": True}})

    async def _pause_interrupted(self):
        """任务被取消（手动中断/进程退出前）：落到 paused，标记中断以便续跑时重跑当前阶段。"""
        self.state["status"] = "paused"
        self.state["interrupted"] = True
        self.state["error"] = "已手动中断，可从任意节点继续"
        self.save()
        self.emit({"event": "interrupted", "data": {"stage": self.state.get("stage")}})

    async def _run_range(self, start: int, end: int):
        try:
            for s in range(start, end + 1):
                self.state["stage"] = s
                self.state["status"] = "running"
                self.save()
                self.emit_stage(s, "started")
                t0 = time.time()
                await self._run_stage(s)
                self.emit_stage(s, "completed", elapsed=round(time.time() - t0, 1))
                self.state.pop("error", None)
                if s in GATES:
                    self.state["status"] = "awaiting_gate"
                    self.state["gate"] = {"stage": s, "name": GATES[s]}
                    self.save()
                    self.emit({"event": "gate", "data": self.state["gate"]})
                    return
            self.state["status"] = "tier_done"
            self.save()
            self.emit({"event": "pipeline_done", "data": {"tier": self.state.get("tier")}})
        except asyncio.CancelledError:
            await self._pause_interrupted()
            raise
        except ProviderError as e:
            await self._fail(e.kind, str(e))
        except Exception as e:
            await self._fail("stage_error", f"{type(e).__name__}: {e}")

    async def _fail(self, kind: str, msg: str):
        self.state["status"] = "failed"
        self.state["error"] = msg
        self.save()
        self.emit({"event": "error", "data": {"kind": kind, "message": msg,
                                              "stage": self.state.get("stage")}})

    async def _run_stage(self, s: int):
        handler = getattr(self, f"_stage_{s}", None)
        if handler is None:
            raise RuntimeError(f"阶段 {s} 未实现")
        try:
            await handler()
        except json.JSONDecodeError:
            # LLM 输出偶发残缺 JSON 具有随机性，自动重试一次再判失败
            self.emit_stage(s, "progress", detail="模型输出 JSON 解析失败，自动重试一次")
            await handler()

    def _register(self, logical, atype, title, rel, source, extra=None):
        art = artifacts.register(self.dir, logical, atype, title, rel, source, extra)
        # 对外事件里的音色 ID 等敏感 extra 打码；注册表保留完整值供服务端使用
        self.emit({"event": "artifact", "data": artifacts.masked_artifact(art)})
        return art

    # ---------- 阶段实现 ----------

    async def _stage_2(self):
        """算法校验：运行用户代码并记录事实。"""
        brief = _read_json(self.dir / "brief.json")
        code = brief.get("code", "")
        result = {"code_provided": bool(code), "language": brief.get("language", "python"),
                  "run": None, "note": ""}
        if code:
            code_file = self.dir / "user_code.py"
            code_file.write_text(code, encoding="utf-8")
            proc = await asyncio.create_subprocess_exec(
                sys.executable, str(code_file),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                cwd=self.dir, env=_subprocess_env(), preexec_fn=_preexec_limits())
            try:
                out, err = await asyncio.wait_for(proc.communicate(), timeout=30)
                result["run"] = {"exit": proc.returncode,
                                 "stdout": out.decode(errors="replace")[-2000:],
                                 "stderr": err.decode(errors="replace")[-2000:]}
            except asyncio.TimeoutError:
                _kill_tree(proc)
                result["run"] = {"exit": -1, "stderr": "运行超时(30s)"}
            except asyncio.CancelledError:
                _kill_tree(proc)
                raise
        else:
            result["note"] = "未提供代码（科普类主题可跳过校验）"
        _write_json(self.dir / "verified_facts.json", result)
        self._register("verified_facts", "json", "算法校验结果",
                       "verified_facts.json", "tool:sandbox")

    async def _stage_3(self):
        """创意方案：LLM 产出 2-3 个方向。"""
        from ..ingest_service import refs_block_for_prompt
        brief = _read_json(self.dir / "brief.json")
        facts = _read_json(self.dir / "verified_facts.json")
        p = provider_for(3)
        raw = await complete(p, [{"role": "user", "content": prompts.CONCEPT_PROMPT.format(
            topic=brief.get("topic", ""), audience=brief.get("audience", "初学者"),
            facts=json.dumps(facts, ensure_ascii=False))
            + refs_block_for_prompt(self.pid)}])
        _write_raw(self.dir, f"concept_{int(time.time())}.txt", raw)
        concept = extract_json(raw)
        concept["_source"] = f"{p['name']}:{p['model']}"
        _write_json(self.dir / "concept.json", concept)
        self._register("concept", "doc", "创意方案", "concept.json",
                       concept["_source"], {"directions": len(concept.get("directions", []))})

    async def _stage_4(self):
        """讲稿分镜：LLM 产出 timeline 草稿。"""
        brief = _read_json(self.dir / "brief.json")
        facts = _read_json(self.dir / "verified_facts.json")
        concept = _read_json(self.dir / "concept.json")
        sel = self.state.get("selected_direction")
        if isinstance(sel, int) and concept.get("directions"):
            chosen = concept["directions"][min(sel, len(concept["directions"]) - 1)]
        else:
            chosen = (concept.get("directions") or [{}])[0]
        from ..ingest_service import refs_block_for_prompt
        p = provider_for(4)
        rules = prompts.content_rules_block(store.load_settings().get("content_rules"))
        raw = await complete(p, [{"role": "user", "content": prompts.STORYBOARD_PROMPT.format(
            topic=brief.get("topic", ""), audience=brief.get("audience", "初学者"),
            facts=json.dumps(facts, ensure_ascii=False),
            concept=json.dumps(chosen, ensure_ascii=False), rules=rules)
            + refs_block_for_prompt(self.pid)}], max_tokens=16384)
        _write_raw(self.dir, f"storyboard_{int(time.time())}.txt", raw)
        sb = extract_json(raw)
        if not sb.get("scenes"):
            raise RuntimeError("分镜生成结果为空或格式不符（原始响应已保存到 raw_responses/）")
        sb["_source"] = f"{p['name']}:{p['model']}"
        _write_json(self.dir / "storyboard_draft.json", sb)
        self._register("storyboard_draft", "doc", "分镜稿（草稿）",
                       "storyboard_draft.json", sb["_source"],
                       {"scenes": len(sb.get("scenes", []))})

    async def _stage_5(self):
        """事实校对：交叉模型核对，有问题则修正后定稿 timeline.json。"""
        facts = _read_json(self.dir / "verified_facts.json")
        sb = _read_json(self.dir / "storyboard_draft.json")
        from ..ingest_service import refs_block_for_prompt
        refs_block = refs_block_for_prompt(self.pid)
        p = provider_for(5)
        rules = prompts.factcheck_rules_block(store.load_settings().get("content_rules"))
        raw = await complete(p, [{"role": "user", "content": prompts.FACTCHECK_PROMPT.format(
            rules=rules,
            facts=json.dumps(facts, ensure_ascii=False)
            + (f"\n【参考资料可用事实】{refs_block}" if refs_block else ""),
            storyboard=json.dumps(sb, ensure_ascii=False))}], max_tokens=16384)
        check = extract_json(raw)
        check["_checker"] = f"{p['name']}:{p['model']}"
        _write_json(self.dir / "factcheck.json", check)
        final = check.get("corrected") or {k: v for k, v in sb.items() if not k.startswith("_")}
        if not final.get("scenes"):
            raise RuntimeError("校对后的分镜缺少 scenes（原始响应已保存）")
        timeline = {
            "title": final.get("title", "讲解视频"),
            "width": 1920, "height": 1080, "fps": 30,
            "scenes": [{
                "id": sc["id"], "title": sc.get("title", f"镜头{sc['id']}"),
                "purpose": sc.get("purpose", ""), "visual": sc.get("visual", ""),
                "narration": sc.get("narration", ""), "tts": sc.get("tts", sc.get("narration", "")),
                "captions": sc.get("captions", []),
            } for sc in final.get("scenes", [])],
        }
        _write_json(self.dir / "timeline.json", timeline)
        norm = self._normalize_text()
        self._register("storyboard", "doc", "分镜稿（定稿）", "timeline.json",
                       f"{sb.get('_source','?')} + 校对:{check['_checker']}",
                       {"scenes": len(timeline["scenes"]),
                        "issues_fixed": len(check.get("issues", [])),
                        "textnorm_changes": norm["captions"] + norm["tts"]})

    async def _stage_6(self):
        """视觉代码：LLM 生成 visuals.py → 编译/导入/冒烟渲染三级自检，失败自动带错修复。"""
        _require_code_exec(6)
        timeline = _read_json(self.dir / "timeline.json")
        p = provider_for(6)
        # 视觉代码用低推理强度：实测 kimi-k3 默认推理会独占 32k token 预算导致正文为空
        raw = await complete(p, [{"role": "user", "content": prompts.VISUALS_PROMPT.format(
            storyboard=json.dumps(timeline, ensure_ascii=False))}], max_tokens=32768,
            reasoning_effort="low")
        _write_raw(self.dir, f"visuals_{int(time.time())}.txt", raw)
        code = strip_code_fences(raw)
        code, fix_rounds = await visuals_with_autofix(
            self.dir, p, code,
            emit=lambda msg: self.emit_stage(6, "progress", detail=msg))
        source = f"{p['name']}:{p['model']}" + (f" + 自动修复{fix_rounds}轮" if fix_rounds else "")
        self._register("visuals", "code", "视觉代码 visuals.py", "visuals.py",
                       source, {"smoke": "passed", "fix_rounds": fix_rounds})

    async def _stage_7(self):
        """关键帧预览：每镜开始/中段/结尾帧。"""
        _require_code_exec(7)
        self._estimate_durations()
        proc = await asyncio.create_subprocess_exec(
            sys.executable, str(TOOLS / "keyframe_render.py"), "--project", str(self.dir),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env=_subprocess_env(), preexec_fn=_preexec_limits())
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=300)
        except asyncio.CancelledError:
            _kill_tree(proc)
            raise
        if proc.returncode != 0:
            raise RuntimeError(f"关键帧渲染失败: {err.decode(errors='replace')[-800:]}")
        frames = json.loads(out.decode())["frames"]
        if not frames:
            raise RuntimeError("关键帧渲染产物为空")
        self._register("keyframes", "gallery", "关键帧预览", "keyframes",
                       "tool:keyframe_render", {"frames": frames})

    def _normalize_text(self) -> dict:
        """B1-3 / B1-4：字幕阿拉伯化 + TTS 多音字同音替换（规则表后处理，不依赖模型自觉）。

        幂等：阶段 5 定稿后套一次，阶段 8 合成前再套一次（覆盖修改流与手工编辑）。
        改动逐条写入 textnorm.json，供审计与「改了什么读法」的追问。"""
        tl = _read_json(self.dir / "timeline.json")
        report = {"captions": [], "tts": []}
        for sc in tl.get("scenes", []):
            captions, changes = textnorm.to_arabic_many(sc.get("captions") or [])
            sc["captions"] = captions
            if changes:
                report["captions"].append({"scene": sc.get("id"), "changes": changes})
        scenes, tts_changes = textnorm.for_tts_scenes(tl.get("scenes") or [])
        tl["scenes"] = scenes
        report["tts"] = tts_changes
        path = self.dir / "textnorm.json"
        if report["captions"] or report["tts"]:
            _write_json(path, report)
        total = len(report["captions"]) + len(report["tts"])
        if total:
            _write_json(self.dir / "timeline.json", tl)
            self.emit_stage(self.state["stage"], "progress",
                            detail=f"文本规范化：{len(report['captions'])} 条字幕数字、"
                                   f"{len(report['tts'])} 条配音多音字已处理")
        return report

    def _estimate_durations(self):
        """TTS 前为关键帧预览估算每镜时长（默认 5.6 字/秒，对齐 skill 实测 0.925 倍速）。"""
        tl = _read_json(self.dir / "timeline.json")
        cps = float((store.load_settings().get("pipeline") or {}).get("tts_chars_per_second") or 5.6)
        if cps <= 0:
            cps = 5.6
        for sc in tl["scenes"]:
            if "duration" not in sc:
                n = len(sc.get("tts") or sc.get("narration") or "")
                sc["audio_offset"] = 0.55
                sc["audio_duration"] = max(3.0, n / cps)
                sc["end_hold"] = 1.2
                sc["duration"] = round(sc["audio_offset"] + sc["audio_duration"] + 1.2, 2)
        _write_json(self.dir / "timeline.json", tl)

    async def _stage_8(self):
        """配音：MiniMax TTS（工具，不调 LLM）。合成前再套一次文本规范化（B1-3/B1-4）。"""
        self._normalize_text()
        voice = store.load_settings()["voice"]
        key = voice.get("minimax_api_key") or os.environ.get("MINIMAX_API_KEY", "")
        if not key:
            raise ProviderError("auth", "未配置 MiniMax API Key（设置页 → 配音）")
        # env 白名单：TTS 子进程只额外拿到 MINIMAX_API_KEY，拿不到其他供应商的 key
        env = _subprocess_env({"MINIMAX_API_KEY": key})
        cmd = [sys.executable, str(TOOLS / "minimax_tts.py"), "--project", str(self.dir),
               "--voice-id", voice.get("voice_id", ""),
               "--speed", str(voice.get("speed", 0.925)),
               "--model", voice.get("model", "minimax-speech-2.8-hd")]
        await self._run_tool(cmd, env, "配音合成")
        self._register("audio", "audio", "逐镜配音", "audio_processed", "tool:minimax_tts",
                       {"voice_id": voice.get("voice_id"), "speed": voice.get("speed")})

    async def _stage_9(self):
        """时间轴组装：校验 TTS 产物并重算开始时间。
        必须确认每镜音频文件真实存在——关键帧阶段的估算时长不等于 TTS 已完成。"""
        tl = _read_json(self.dir / "timeline.json")
        for sc in tl["scenes"]:
            ap = sc.get("audio_path")
            if not ap or not (self.dir / ap).exists():
                raise RuntimeError(f"镜头 {sc.get('id')} 缺少配音文件（TTS 未完成）")
            if "audio_duration" not in sc:
                raise RuntimeError(f"镜头 {sc['id']} 缺少 audio_duration（TTS 未完成？）")
        t = 0.0
        fps = tl.get("fps", 30)
        for sc in tl["scenes"]:
            sc["start"] = round(t, 3)
            frames = int(round(sc["duration"] * fps))
            sc["frames"] = frames
            t += frames / fps
        tl["total_duration"] = round(t, 3)
        _write_json(self.dir / "timeline.json", tl)
        self._register("timeline", "json", "时间轴", "timeline.json", "tool:timeline",
                       {"total_duration": tl["total_duration"], "scenes": len(tl["scenes"])})

    async def _stage_10(self):
        """渲染合成（长任务，VPS 双核 → workers=2）。"""
        _require_code_exec(10)
        out_name = f"成片_v{int(time.time())}.mp4"
        out_path = self.dir / out_name
        bgm_path, bgm_volume, bgm_note = bgm_config()
        cmd = [sys.executable, str(TOOLS / "render_video.py"), "--project", str(self.dir),
               "--output", str(out_path), "--workers", "2"]
        if bgm_path:
            cmd += ["--bgm", str(bgm_path), "--bgm-volume", f"{bgm_volume:.4f}"]
        else:
            self.emit_stage(10, "progress", detail=bgm_note)
        _write_json(self.dir / "bgm.json", {
            "path": bgm_path.name if bgm_path else None,
            "volume": bgm_volume, "note": bgm_note})
        # 渲染子进程会导入并执行 visuals.py：env 里不传任何 API key
        await self._run_tool(cmd, _subprocess_env(), "渲染合成", timeout=7200)
        self._register("video", "video", "视频成片", out_name, "tool:render_video",
                       {"size": out_path.stat().st_size,
                        "bgm": bgm_path.name if bgm_path else None})

    async def _stage_11(self):
        """QC 验收。"""
        vids = [a for a in artifacts.list_artifacts(self.dir)
                if a["type"] == "video" and a.get("current")]
        if not vids:
            raise RuntimeError("QC 找不到当前视频产物")
        video = self.dir / vids[-1]["path"]
        bgm_path, bgm_volume, bgm_note = bgm_config()
        cmd = [sys.executable, str(TOOLS / "qc_video.py"), "--project", str(self.dir),
               "--video", str(video)]
        if bgm_path:
            cmd += ["--bgm", str(bgm_path), "--bgm-volume", f"{bgm_volume:.4f}"]
        else:
            cmd += ["--bgm-note", bgm_note]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env=_subprocess_env(), preexec_fn=_preexec_limits())
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=3600)
        except asyncio.CancelledError:
            _kill_tree(proc)
            raise
        report = {"exit": proc.returncode, "stdout": out.decode(errors="replace")[-4000:],
                  "stderr": err.decode(errors="replace")[-2000:]}
        if (self.dir / "final_qc.json").exists():
            report["detail"] = _read_json(self.dir / "final_qc.json")
        self._register("qc", "doc", "QC 验收报告", "final_qc.json", "tool:qc_video",
                       {"exit": proc.returncode})
        if proc.returncode != 0:
            raise RuntimeError(f"QC 未通过（详见 QC 报告产物）: {report['stderr'][-300:]}")

    async def _run_tool(self, cmd, env, name, timeout=1800):
        """执行工具子进程，并把 stdout 的进度行转成 stage_event(progress) 推给 SSE。
        render_video.py 每 300 帧打印一次 `scene 03: 1200/3400`，收尾打印 `scene 03: DONE 3400 frames`；
        minimax_tts.py 打印 `scene 03: audio=...`（不匹配进度正则，忽略）。"""
        self.emit_stage(self.state["stage"], "progress", detail=f"{name} 运行中…")
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env=env, cwd=self.dir, preexec_fn=_preexec_limits())
        total_frames = self._timeline_total_frames()
        scene_frames: dict[int, int] = {}
        stderr_tail: list[str] = []
        stdout_tail: list[str] = []

        def on_stdout(line: str):
            if not line.strip():
                return
            stdout_tail.append(line)
            del stdout_tail[:-20]
            m = PROGRESS_FRAME_RE.search(line) or PROGRESS_DONE_RE.search(line)
            if not m:
                return
            scene_no, frames = int(m.group(1)), int(m.group(2))
            scene_frames[scene_no] = frames
            done = sum(scene_frames.values())
            if total_frames > 0:
                percent = max(0, min(99, int(done * 100 / total_frames)))
                self.emit_stage(self.state["stage"], "progress",
                                detail=f"{name} {percent}%（{done}/{total_frames} 帧）",
                                percent=percent, frames=done, total_frames=total_frames)
            else:
                self.emit_stage(self.state["stage"], "progress",
                                detail=f"{name} 镜头 {scene_no}：{frames} 帧",
                                frames=frames)

        def on_stderr(line: str):
            if line.strip():
                stderr_tail.append(line)
                del stderr_tail[:-20]

        async def pump(stream, handler):
            while True:
                raw = await stream.readline()
                if not raw:
                    break
                handler(raw.decode(errors="replace").rstrip())

        try:
            await asyncio.wait_for(asyncio.gather(
                pump(proc.stdout, on_stdout),
                pump(proc.stderr, on_stderr),
                proc.wait(),
            ), timeout=timeout)
        except asyncio.TimeoutError:
            _kill_tree(proc)
            raise RuntimeError(f"{name}超时")
        except asyncio.CancelledError:
            # 取消任务必须连锅端掉子进程组（D3/C-REL-07）：
            # 渲染/QC/ffmpeg 不会在取消后继续吃 CPU
            _kill_tree(proc)
            raise
        if proc.returncode != 0:
            err = "\n".join(stderr_tail) or "\n".join(stdout_tail)
            raise RuntimeError(f"{name}失败: {err[-800:]}")
        return "\n".join(stdout_tail)

    def _timeline_total_frames(self) -> int:
        """渲染进度分母：优先用 timeline.json 里已算好的总帧数，否则按 fps*duration 估算。"""
        p = self.dir / "timeline.json"
        if not p.exists():
            return 0
        try:
            tl = _read_json(p)
        except (json.JSONDecodeError, OSError):
            return 0
        total = tl.get("frames")
        if isinstance(total, (int, float)) and total > 0:
            return int(total)
        fps = float(tl.get("fps") or 30)
        return sum(int(round(float(sc.get("duration") or 0) * fps))
                   for sc in tl.get("scenes", []))


def start_run(project_id: str, coro) -> asyncio.Task:
    old = RUNNING.get(project_id)
    if old and not old.done():
        old.cancel()
    task = asyncio.create_task(coro)
    RUNNING[project_id] = task
    return task


def is_running(project_id: str) -> bool:
    t = RUNNING.get(project_id)
    return bool(t and not t.done())


def stop(project_id: str) -> bool:
    """中断正在运行的流水线；任务取消后由 _pause_interrupted 落盘为 paused。"""
    t = RUNNING.get(project_id)
    if t and not t.done():
        t.cancel()
        return True
    return False


def reap_if_stale(project_id: str) -> dict | None:
    """状态文件停在 running 但无存活任务（进程重启/崩溃残留）→ 标记 failed 并返回状态。"""
    st = load_state(store.project_dir(project_id))
    if st.get("status") == "running" and not is_running(project_id):
        st["status"] = "failed"
        st["error"] = "进程重启或异常导致流水线中断，可用「重试」从当前阶段继续"
        save_state(store.project_dir(project_id), st)
        store.update_project(project_id, state="failed")
    return st
