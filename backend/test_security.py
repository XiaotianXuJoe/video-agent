#!/usr/bin/env python3
"""安全回归（纯本地 mock，不依赖外部服务）：
A. 路径穿越：pid 白名单 + resolve_path（`..`、兄弟目录前缀碰撞）+ HTTP 层
B. 鉴权：未配置 401 / 三通道（Bearer、cookie、query）/ 登录 cookie 属性 / 限频 / token 不外泄
C. 密钥外发封堵：test_provider 只用已保存 id、忽略请求体 base_url；test_minimax 不接受请求体
D. 执行加固：env 白名单、平台 resource 限额、生成代码执行开关
E. SSRF：内网/环回/链路本地/元数据地址拒绝
F. 音色 ID 只回显后 4 位
"""
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app import store  # noqa: E402

# 先把存储重定向到临时目录，避免污染 backend/data 与 backend/projects
TMP = Path(tempfile.mkdtemp(prefix="va_sec_test_"))
store.DATA_DIR = TMP / "data"
store.DATA_DIR.mkdir()
store.PROJECTS_DIR = TMP / "projects"
store.PROJECTS_DIR.mkdir()
store.SETTINGS_PATH = store.DATA_DIR / "settings.json"
store.DB_PATH = store.DATA_DIR / "app.db"
store.init_db()

from fastapi import HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import artifacts, auth  # noqa: E402
from app.ingest import IngestError  # noqa: E402
from app.ingest.web import assert_public_http_url  # noqa: E402
from app.llm import ProviderError  # noqa: E402
from app.main import app  # noqa: E402
from app.pipeline import machine  # noqa: E402

results = []
TOKEN = "test-token-9f8e7d"


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(f"[{'PASS' if cond else 'FAIL'}] {name} {detail}")


def set_settings(token=""):
    s = store.load_settings()
    s["auth"]["token"] = token
    store.save_settings(s)


def auth_hdr(t=TOKEN):
    return {"Authorization": f"Bearer {t}"}


# ---------- A. 路径穿越 ----------

def case_path_traversal():
    for bad in ("..", "abc", "../abc2", "ABCDEF123456", "a" * 40, ""):
        try:
            store.project_dir(bad)
            check(f"A 拒绝非法 pid={bad!r}", False, "未抛 404")
        except HTTPException as e:
            check(f"A 拒绝非法 pid={bad!r}", e.status_code == 404, f"status={e.status_code}")

    pid = store.create_project("穿越测试", "video")
    d = store.project_dir(pid)
    check("A 合法 pid 返回项目目录", d == store.PROJECTS_DIR / pid)
    check("A 读路径不建目录", not d.exists(), f"exists={d.exists()}")
    store.create_project_dir(pid)
    check("A create_project_dir 显式建目录", d.exists())

    # 原攻击路径 1：pid=".." + path="data/settings.json"。
    # pid 在 store.project_dir 已被拦下；这里补 resolve_path 层面的 ../ 逃逸（从项目目录上跳两级）
    try:
        p = artifacts.resolve_path(d, "../../data/settings.json")
        check("A resolve_path 阻止 ../ 逃逸", False, f"逃逸到 {p}")
    except ValueError:
        check("A resolve_path 阻止 ../ 逃逸", True)

    # 原攻击路径 2：兄弟目录前缀碰撞（abc 通过 abc2 的字符串前缀校验）
    proj = TMP / "proj_abc123456"
    sibling = TMP / "proj_abc1234567"
    proj.mkdir(); sibling.mkdir()
    (sibling / "secret.txt").write_text("x", encoding="utf-8")
    try:
        p = artifacts.resolve_path(proj, "../proj_abc1234567/secret.txt")
        check("A resolve_path 阻止兄弟目录前缀碰撞", False, f"逃逸到 {p}")
    except ValueError:
        check("A resolve_path 阻止兄弟目录前缀碰撞", True)
    inside = artifacts.resolve_path(proj, "ok.txt")
    check("A resolve_path 允许项目内文件", inside == (proj / "ok.txt").resolve())


def case_path_http():
    set_settings(TOKEN)
    c = TestClient(app)
    for bad_pid, label in (("..", ".."), ("abc", "前缀碰撞无效 id")):
        r = c.get(f"/api/projects/{bad_pid}/file", params={"path": "data/settings.json"},
                  headers=auth_hdr(), follow_redirects=False)
        check(f"A HTTP /file 拒绝 pid={label}", r.status_code in (403, 404, 307, 400),
              f"status={r.status_code}")
    r = c.get("/api/projects/../artifacts", headers=auth_hdr(), follow_redirects=False)
    check("A HTTP /artifacts 拒绝 pid=..", r.status_code in (403, 404, 307, 400),
          f"status={r.status_code}")


# ---------- B. 鉴权 ----------

def case_auth_unconfigured():
    set_settings("")
    auth.reset_rate_limit()
    c = TestClient(app)
    r = c.get("/api/settings")
    body = r.json()
    check("B 未配置 token：/api 返回 401", r.status_code == 401)
    check("B 未配置 token：kind=auth_unconfigured",
          body.get("error", {}).get("kind") == "auth_unconfigured", json.dumps(body))
    r = c.post("/api/auth/login", json={"password": "whatever"})
    check("B 未配置 token：login 也返回 auth_unconfigured",
          r.status_code == 401 and r.json().get("error", {}).get("kind") == "auth_unconfigured")


def case_auth_channels():
    set_settings(TOKEN)
    auth.reset_rate_limit()
    c = TestClient(app)

    r = c.get("/api/settings")
    check("B 无凭据：401 auth_required",
          r.status_code == 401 and r.json().get("error", {}).get("kind") == "auth_required")

    r = c.get("/api/settings", headers=auth_hdr("wrong"))
    check("B 错误 Bearer：401", r.status_code == 401)

    r = c.post("/api/auth/login", json={"password": "wrong"})
    check("B 错误密码：401 auth_failed",
          r.status_code == 401 and r.json().get("error", {}).get("kind") == "auth_failed")

    r = c.post("/api/auth/login", json={"password": TOKEN})
    cookie = r.headers.get("set-cookie", "")
    check("B 正确密码登录成功", r.status_code == 200 and r.json().get("ok") is True)
    check("B 登录下发 HttpOnly + SameSite=Strict + Path=/ cookie",
          f"va_token={TOKEN}" in cookie and "HttpOnly" in cookie
          and "SameSite=strict" in cookie.replace("samesite", "SameSite") and "Path=/" in cookie,
          cookie)
    check("B 纯 HTTP 环境不加 Secure", "Secure" not in cookie)

    c.cookies.set("va_token", TOKEN)
    r = c.get("/api/settings")
    check("B cookie 通道放行", r.status_code == 200)
    tok_echo = r.json().get("auth", {}).get("token")
    check("B 设置接口不回传明文 token", tok_echo == "********", f"token={tok_echo}")

    c.cookies.clear()
    r = c.get("/api/settings", headers=auth_hdr())
    check("B Bearer 通道放行", r.status_code == 200)

    r = c.get(f"/api/settings?token={TOKEN}")
    check("B query token（SSE 兜底）放行", r.status_code == 200)

    r = c.get("/api/health", headers=auth_hdr())
    check("B /api/health 同样受保护（带 token 200）", r.status_code == 200)
    r = c.get("/api/health")
    check("B /api/health 无 token 401", r.status_code == 401)

    # PUT 回传脱敏 token 时保留原值（不能把密码改成 ********）
    r = c.get("/api/settings", headers=auth_hdr())
    s = r.json()
    r = c.put("/api/settings", json=s, headers=auth_hdr())
    check("B PUT 设置成功（脱敏 token 回写）", r.status_code == 200)
    check("B 脱敏 token 不会覆盖真实密码", auth.configured_token() == TOKEN)


def case_auth_logout_and_rate_limit():
    set_settings(TOKEN)
    auth.reset_rate_limit()
    c = TestClient(app)
    r = c.post("/api/auth/logout", headers=auth_hdr())
    check("B logout 清 cookie",
          r.status_code == 200 and "va_token" in r.headers.get("set-cookie", ""),
          r.headers.get("set-cookie", ""))

    auth.reset_rate_limit()
    codes = [c.post("/api/auth/login", json={"password": "bad"}).status_code for _ in range(11)]
    check("B 登录限频：前 10 次按密码错误处理", codes[:10] == [401] * 10, str(codes))
    check("B 登录限频：第 11 次 429", codes[10] == 429, str(codes))
    auth.reset_rate_limit()
    r = c.post("/api/auth/login", json={"password": TOKEN})
    check("B 限频清零后可正常登录", r.status_code == 200)


def case_static_public():
    """登录页必须能加载：SPA 与 /assets 不拦；产物端点必须拦（都在 /api 下）。"""
    import re
    from app import main as main_mod
    if not main_mod.DIST.exists():
        print("[SKIP] B 静态资源公开：frontend/dist 不存在（未构建前端）")
        return
    c = TestClient(app)
    r = c.get("/")
    check("B 静态首页公开（无 token 可加载登录页）", r.status_code == 200)
    m = re.search(r'/assets/[^"\']+\.js', r.text or "")
    if m:
        r2 = c.get(m.group(0))
        check("B /assets 公开", r2.status_code == 200, f"status={r2.status_code}")
    r = c.get("/api/projects/000000000000/artifacts")
    check("B 产物端点无 token 仍 401", r.status_code == 401)


# ---------- C. 密钥外发封堵 ----------

def case_test_provider():
    set_settings(TOKEN)
    s = store.load_settings()
    s["providers"] = [{"id": "p_saved", "name": "Saved", "type": "openai",
                       "base_url": "https://saved.example/v1", "api_key": "sk-real-secret",
                       "model": "m1"}]
    store.save_settings(s)

    import app.llm as llm
    captured = {}

    async def fake_test(p):
        captured.clear(); captured.update(p)
        return {"ok": True}

    orig = llm.test_provider
    llm.test_provider = fake_test
    try:
        c = TestClient(app)
        r = c.post("/api/settings/test_provider", headers=auth_hdr(), json={
            "id": "p_saved",
            "provider": {"type": "openai", "base_url": "https://attacker.example",
                         "api_key": "sk-attacker", "model": "m1"},
            "base_url": "https://attacker.example",
        })
        check("C test_provider 成功", r.status_code == 200 and captured.get("base_url") == "https://saved.example/v1",
              f"status={r.status_code} base={captured.get('base_url')}")
        check("C test_provider 忽略请求体 base_url", captured.get("base_url") != "https://attacker.example")
        check("C test_provider 使用已保存 key", captured.get("api_key") == "sk-real-secret")

        r = c.post("/api/settings/test_provider", headers=auth_hdr(),
                   json={"id": "p_new_unsaved", "base_url": "https://attacker.example"})
        check("C test_provider 拒绝未保存 id", r.status_code == 404, f"status={r.status_code}")
    finally:
        llm.test_provider = orig


def case_test_minimax():
    set_settings(TOKEN)
    s = store.load_settings()
    s["voice"]["minimax_api_key"] = "mm-saved-key"
    s["voice"]["minimax_base_url"] = "https://saved.example/v1"
    s["voice"]["model"] = "minimax-speech-2.8-hd"
    s["voice"]["voice_id"] = "voice-saved"
    store.save_settings(s)

    import httpx
    captured = {}

    class FakeResp:
        status_code = 200
        text = "{}"

        def json(self):
            return {"data": {"audio": "00"}, "base_resp": {"status_code": 0}}

    class FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):
            captured["url"] = url
            captured["headers"] = headers or {}
            captured["body"] = json or {}
            return FakeResp()

    orig = httpx.AsyncClient
    httpx.AsyncClient = FakeClient
    try:
        c = TestClient(app)
        r = c.post("/api/settings/test_minimax", headers=auth_hdr(), json={
            "api_key": "mm-attacker-key",
            "base_url": "https://attacker.example",
            "voice_id": "attacker-voice",
        })
        check("C test_minimax 成功", r.status_code == 200, f"status={r.status_code}")
        check("C test_minimax 忽略请求体 base_url",
              captured.get("url", "").startswith("https://saved.example/v1"),
              captured.get("url", ""))
        check("C test_minimax 使用已保存 key",
              captured.get("headers", {}).get("Authorization") == "Bearer mm-saved-key")
        check("C test_minimax 使用已保存音色",
              captured.get("body", {}).get("voice_setting", {}).get("voice_id") == "voice-saved")
    finally:
        httpx.AsyncClient = orig


# ---------- D. 执行加固 ----------

def case_exec_hardening():
    os.environ["VA_SECRET_SHOULD_NOT_LEAK"] = "leak-me"
    os.environ["MINIMAX_API_KEY"] = "leak-me-too"
    try:
        env = machine._subprocess_env()
        check("D 子进程 env 不继承任意密钥", "VA_SECRET_SHOULD_NOT_LEAK" not in env)
        check("D 渲染 env 不含 MINIMAX_API_KEY", "MINIMAX_API_KEY" not in env)
        tts_env = machine._subprocess_env({"MINIMAX_API_KEY": "k"})
        check("D TTS env 只额外带 MINIMAX_API_KEY", tts_env.get("MINIMAX_API_KEY") == "k")
        check("D env 白名单保留 PATH", "PATH" in env or os.name == "nt")
        if os.name == "nt":
            check("D Windows 保留 SYSTEMROOT 等系统变量",
                  "SYSTEMROOT" in env or "SYSTEMROOT" not in os.environ)
    finally:
        os.environ.pop("VA_SECRET_SHOULD_NOT_LEAK", None)
        os.environ.pop("MINIMAX_API_KEY", None)

    pre = machine._preexec_limits()
    if os.name == "posix":
        check("D POSIX 提供 resource 限额函数", callable(pre))
    else:
        check("D Windows 跳过 preexec_fn", pre is None)


def case_code_exec_switch():
    set_settings(TOKEN)
    s = store.load_settings()
    s["security"]["allow_generated_code_exec"] = False
    store.save_settings(s)
    pid = store.create_project("开关测试", "video")
    store.create_project_dir(pid)
    pl = machine.Pipeline(pid)
    for stage in (6, 7, 10):
        try:
            asyncio.run(getattr(pl, f"_stage_{stage}")())
            check(f"D 开关关闭：阶段 {stage} 直接失败", False, "未抛错")
        except ProviderError as e:
            check(f"D 开关关闭：阶段 {stage} 直接失败", e.kind == "config", f"kind={e.kind}")
            check(f"D 阶段 {stage} 提示去设置页开启", "设置页" in e.detail)
        except Exception as e:  # 护栏：其他异常也算失败
            check(f"D 开关关闭：阶段 {stage} 直接失败", False, f"{type(e).__name__}: {e}")
    s = store.load_settings()
    s["security"]["allow_generated_code_exec"] = True
    store.save_settings(s)


# ---------- E. SSRF ----------

def case_ssrf():
    bad = [
        "http://127.0.0.1/", "http://127.0.0.1:8080/x",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.5/", "http://172.16.3.4/", "http://192.168.1.1/",
        "http://[::1]/", "http://[::ffff:127.0.0.1]/", "http://[fc00::1]/",
    ]
    for url in bad:
        try:
            assert_public_http_url(url)
            check(f"E 拒绝内网地址 {url}", False, "未拒绝")
        except IngestError as e:
            check(f"E 拒绝内网地址 {url}", e.kind == "config", f"kind={e.kind}")

    for url in ("ftp://example.com/x", "file:///etc/passwd", "http:///nohost"):
        try:
            assert_public_http_url(url)
            check(f"E 拒绝非 http(s)/无主机名 {url}", False, "未拒绝")
        except IngestError as e:
            check(f"E 拒绝非 http(s)/无主机名 {url}", e.kind == "config", f"kind={e.kind}")


# ---------- F. 音色 ID 打码 ----------

def case_voice_mask():
    check("F mask_extra 只留后 4 位",
          artifacts.mask_extra({"voice_id": "abcdefgh1234"})["voice_id"] == "***1234")
    check("F mask_extra 空值不误伤", artifacts.mask_extra({}) == {})

    set_settings(TOKEN)
    pid = store.create_project("打码测试", "video")
    d = store.create_project_dir(pid)
    artifacts.register(d, "audio", "audio", "逐镜配音", "audio_processed",
                       "tool:minimax_tts", {"voice_id": "voice-full-id-1234", "speed": 0.925})
    c = TestClient(app)
    r = c.get(f"/api/projects/{pid}/artifacts", headers=auth_hdr())
    extra = (r.json() or [{}])[-1].get("extra", {})
    check("F 接口回显 voice_id 已打码", extra.get("voice_id") == "***1234", str(extra))
    raw = artifacts.list_artifacts(d)[-1]["extra"]["voice_id"]
    check("F 服务端注册表保留完整 voice_id", raw == "voice-full-id-1234")


def case_voice_key_mask_roundtrip():
    """脱敏掩码回写保护：前端保存时带回 ******** 不得覆盖真实 MiniMax key。

    回归 bug：PUT 只判省略号/空串，漏判星号掩码 → 保存设置后真实 key 被覆盖为
    字面 "********"，TTS 测试/合成全部 401。"""
    set_settings(TOKEN)
    s = store.load_settings()
    s["voice"]["minimax_api_key"] = "mm-real-key-88d2"
    store.save_settings(s)

    c = TestClient(app)
    r = c.get("/api/settings", headers=auth_hdr())
    masked = r.json()["voice"]["minimax_api_key"]
    check("G 前端拿到的 minimax key 为掩码", masked == "********", masked)

    s2 = store.load_settings()
    s2["voice"]["minimax_api_key"] = masked  # 模拟前端原样回传
    r2 = c.put("/api/settings", json=s2, headers=auth_hdr())
    check("G 掩码回写保存成功", r2.status_code == 200, str(r2.status_code))
    saved = store.load_settings()["voice"]["minimax_api_key"]
    check("G 保存后真实 key 未被覆盖", saved == "mm-real-key-88d2", repr(saved))


# ---------- 主流程 ----------

def main():
    case_path_traversal()
    case_path_http()
    case_auth_unconfigured()
    case_auth_channels()
    case_auth_logout_and_rate_limit()
    case_static_public()
    case_test_provider()
    case_test_minimax()
    case_exec_hardening()
    case_code_exec_switch()
    case_ssrf()
    case_voice_mask()
    case_voice_key_mask_roundtrip()

    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n===== {passed}/{len(results)} 通过 =====")
    if passed != len(results):
        print("失败项：")
        for name, ok, detail in results:
            if not ok:
                print(f"  - {name} {detail}")
        sys.exit(1)


if __name__ == "__main__":
    main()
