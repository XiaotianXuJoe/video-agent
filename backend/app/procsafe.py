"""子进程加固（C-SEC-04 / C-SEC-07 / D3）：resource 限额 + env 白名单 + 进程组管理。

所有执行工具/生成代码的子进程都应经这里构造 env 与 preexec_fn：
- POSIX：CPU 600s、地址空间 4GB、单文件 2GB、禁 core dump；Windows 不支持 preexec_fn。
- POSIX 下子进程同时 setsid 成为新会话首进程，取消任务时可整组回收（D3/C-REL-07），
  避免 ffmpeg/Python 渲染子进程被杀父进程后成孤儿继续吃 CPU。
- env：默认只传运行必需变量（PATH/HOME/LANG/LC_*/TZ 与 Windows 系统变量），
  绝不继承进程环境中的 API key、SSH_AUTH_SOCK 等凭据。
"""
import os

CPU_SECONDS = 600
ADDRESS_SPACE = 4 * 1024**3
FILE_SIZE = 2 * 1024**3

_BASE_ENV_KEYS = ("PATH", "HOME", "LANG", "TZ", "FFMPEG", "FFPROBE")
# Windows 本地开发必需的系统变量（缺 SYSTEMROOT 时 Python 子进程可能起不来）
_WIN_ENV_KEYS = ("SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP",
                 "USERPROFILE", "APPDATA", "LOCALAPPDATA", "NUMBER_OF_PROCESSORS",
                 "PROCESSOR_ARCHITECTURE", "HOMEDRIVE", "HOMEPATH")


def preexec_limits():
    """POSIX 下返回「新会话 + resource 限额」函数；Windows 返回 None。"""
    if os.name != "posix":
        return None

    def _set():
        import resource  # 仅 POSIX 有
        os.setsid()  # 新会话首进程：取消时按进程组整体回收（见 kill_process_tree）
        resource.setrlimit(resource.RLIMIT_CPU, (CPU_SECONDS, CPU_SECONDS))
        resource.setrlimit(resource.RLIMIT_AS, (ADDRESS_SPACE, ADDRESS_SPACE))
        resource.setrlimit(resource.RLIMIT_FSIZE, (FILE_SIZE, FILE_SIZE))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))

    return _set


def kill_process_tree(proc):
    """取消/超时场景下回收子进程：POSIX 杀整个进程组（孙子进程一并回收），
    Windows 退化为杀进程本身。进程已退出时静默返回。"""
    if proc.returncode is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(os.getpgid(proc.pid), 9)
        else:
            proc.kill()
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except (ProcessLookupError, PermissionError, OSError):
            pass


def subprocess_env(extra: dict | None = None) -> dict:
    """白名单 env；extra 只用于显式追加该子进程必需的最小变量（如 TTS 的 MINIMAX_API_KEY）。"""
    keys = set(_BASE_ENV_KEYS)
    if os.name == "nt":
        keys.update(_WIN_ENV_KEYS)
    env = {k: v for k, v in os.environ.items() if k in keys or k.startswith("LC_")}
    if extra:
        env.update({k: v for k, v in extra.items() if v})
    return env
