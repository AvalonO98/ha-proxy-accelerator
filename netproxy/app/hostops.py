"""宿主机操作层。

插件运行在容器里，而 docker 守护进程配置在宿主机上。三种能力来源：

  host 模式：容器带 host_pid + SYS_ADMIN，用 `nsenter -t 1 -m` 进入宿主 mount
             命名空间读写宿主文件/执行宿主命令。这是 HAOS 上的正常路径。
  local 模式：没有 nsenter 权限（例如在普通 Docker 里跑），只能操作容器自己的
             文件系统；能力探测会报告不可用，面板上会明确提示。
  fake 模式：测试用。NETPROXY_HOST_ROOT 指向一个假根目录，所有路径被重定向到
             它下面，于是 daemon.json 的读写可以脱离 HAOS 做单元测试。

注意：host_network: true 让容器与宿主机共享网络命名空间，host_pid: true 共享
PID 命名空间，所以宿主进程可以直接用 os.kill 发信号，不需要再 setns。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import threading
from typing import Optional, Sequence, Tuple

FAKE_ROOT = os.environ.get("NETPROXY_HOST_ROOT")  # 测试注入
_GLOBAL_PID1 = 1

_mode: Optional[str] = None
_mode_lock = threading.Lock()

#: 允许的宿主命令白名单（第一段），避免被面板参数牵着执行任意命令
_ALLOWED_BIN = {
    "sh", "cat", "ls", "mv", "cp", "rm", "mkdir", "test", "stat", "date",
    "systemctl", "systemd-run", "docker", "dockerd", "kill", "chmod", "sync",
    "ifconfig", "ip", "ping", "nc", "pgrep", "pidof", "reboot", "sleep", "tr",
}


def _nsenter_path() -> Optional[str]:
    return shutil.which("nsenter")


def _nsenter_works() -> bool:
    ns = _nsenter_path()
    if not ns:
        return False
    try:
        r = subprocess.run(
            [ns, "-t", str(_GLOBAL_PID1), "-m", "--", "true"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10,
        )
        return r.returncode == 0
    except Exception:
        return False


def namespace_mode() -> str:
    global _mode
    with _mode_lock:
        if _mode is None:
            if FAKE_ROOT is not None:
                _mode = "fake"
            elif _nsenter_works():
                _mode = "host"
            else:
                _mode = "local"
        return _mode


def reset_mode_cache() -> None:
    global _mode
    with _mode_lock:
        _mode = None


def hp(path: str) -> str:
    """把宿主绝对路径映射为当前模式下可操作的路径。"""
    if namespace_mode() == "fake":
        return os.path.join(FAKE_ROOT, path.lstrip("/"))
    return path


def host_available() -> bool:
    return namespace_mode() == "host"


# --------------------------------------------------------------------------- #
# 执行
# --------------------------------------------------------------------------- #
def run_host(args: Sequence[str], input_text: Optional[str] = None,
             timeout: int = 60) -> Tuple[int, str, str]:
    """在宿主命名空间执行命令（host 模式）或本地执行（local/fake 模式）。

    返回 (returncode, stdout, stderr)；任何异常都收敛成 returncode=-1。
    """
    args = [str(a) for a in args]
    if args and os.path.basename(args[0]) not in _ALLOWED_BIN:
        return -1, "", f"command not allowed: {args[0]}"

    mode = namespace_mode()
    if mode == "host":
        cmd = [_nsenter_path(), "-t", str(_GLOBAL_PID1), "-m", "--"] + args
    else:
        cmd = args

    try:
        r = subprocess.run(
            cmd,
            input=(input_text.encode("utf-8") if input_text is not None else None),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=timeout,
        )
        return r.returncode, r.stdout.decode("utf-8", "replace"), r.stderr.decode("utf-8", "replace")
    except subprocess.TimeoutExpired:
        return -1, "", f"timeout after {timeout}s: {' '.join(args[:3])}"
    except FileNotFoundError as e:
        return -1, "", f"not found: {e}"
    except Exception as e:  # pragma: no cover - 兜底
        return -1, "", f"{type(e).__name__}: {e}"


def sh_host(script: str, positional: Sequence[str] = (), input_text: Optional[str] = None,
            timeout: int = 60) -> Tuple[int, str, str]:
    """在宿主上执行一段 POSIX sh 脚本；positional 作为 $1.. 传入，避免拼接注入。"""
    return run_host(["sh", "-c", script, "netproxy"] + list(positional),
                    input_text=input_text, timeout=timeout)


# --------------------------------------------------------------------------- #
# 文件读写
# --------------------------------------------------------------------------- #
def read_text(path: str, max_bytes: int = 512 * 1024) -> Optional[str]:
    """读取（宿主）文件；不存在或不可读返回 None。"""
    if namespace_mode() == "host":
        rc, out, _ = run_host(["cat", path], timeout=20)
        if rc != 0:
            return None
        return out[:max_bytes]
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read(max_bytes)
    except Exception:
        return None


def exists(path: str) -> bool:
    if namespace_mode() == "host":
        rc, _, _ = run_host(["sh", "-c", '[ -e "$1" ]', path], timeout=15)
        return rc == 0
    return os.path.exists(path)


def write_text(path: str, text: str, atomic: bool = True) -> Tuple[bool, str]:
    """写入（宿主）文件。atomic 时先写临时文件再 mv，避免写坏导致 dockerd 起不来。"""
    if namespace_mode() == "host":
        if atomic:
            script = 'd=$(dirname "$1"); f=$(basename "$1"); t="$d/.$f.netproxy.$$"; cat > "$t" && sync && mv -f "$t" "$1"'
        else:
            script = 'cat > "$1"'
        rc, _, err = sh_host(script, [path], input_text=text, timeout=30)
        if rc != 0:
            return False, err.strip() or f"write failed (rc={rc})"
        return True, "ok"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if atomic:
            tmp = os.path.join(os.path.dirname(path), "." + os.path.basename(path) + ".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(text)
            os.replace(tmp, path)
        else:
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
        return True, "ok"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def remove(path: str) -> Tuple[bool, str]:
    if namespace_mode() == "host":
        rc, _, err = run_host(["rm", "-f", path], timeout=15)
        return (rc == 0), (err.strip() or "ok")
    try:
        if os.path.exists(path):
            os.remove(path)
        return True, "ok"
    except Exception as e:
        return False, str(e)


def parent_writable(path: str) -> Tuple[bool, str]:
    """探测目标文件的父目录是否可写（决定能否落地配置）。"""
    parent = os.path.dirname(path)
    probe = os.path.join(parent, ".netproxy-write-test")
    if namespace_mode() == "host":
        rc, _, err = sh_host('mkdir -p "$(dirname "$1")" && : > "$1" && rm -f "$1"',
                             [probe], timeout=15)
        return (rc == 0), (err.strip() or "ok")
    try:
        os.makedirs(parent, exist_ok=True)
        with open(probe, "w") as f:
            f.write("")
        os.remove(probe)
        return True, "ok"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


# --------------------------------------------------------------------------- #
# 进程（host_pid 共享宿主 PID 命名空间，所以 /proc 里能看到宿主进程）
# --------------------------------------------------------------------------- #
def find_pids(name: str, cmdline_contains: Optional[str] = None) -> list:
    out = []
    try:
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            pid = int(entry)
            try:
                with open(f"/proc/{pid}/comm", "r") as f:
                    comm = f.read().strip()
            except OSError:
                continue
            if comm != name:
                continue
            if cmdline_contains:
                try:
                    with open(f"/proc/{pid}/cmdline", "rb") as f:
                        cl = f.read().decode("utf-8", "replace")
                except OSError:
                    cl = ""
                if cmdline_contains not in cl:
                    continue
            out.append(pid)
    except Exception:
        pass
    return out


def signal_pid(pid: int, sig: int) -> Tuple[bool, str]:
    try:
        os.kill(pid, sig)
        return True, "ok"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def capability_report(paths: Sequence[str] = ()) -> dict:
    """能力探测：面板首页展示"能不能真正落地"。"""
    mode = namespace_mode()
    rep = {
        "namespace_mode": mode,
        "host_access": mode == "host",
        "pid1": _read_local("/proc/1/comm") or "?",
        "nsenter": _nsenter_path() or "",
        "host_pid_shared": _read_local("/proc/1/comm") in ("systemd", "init", "tini"),
        "paths": {},
    }
    for p in paths:
        eff = hp(p)
        rep["paths"][p] = {
            "exists": exists(eff),
            "writable_parent": parent_writable(eff)[0],
        }
    return rep


def _read_local(path: str) -> Optional[str]:
    try:
        with open(path, "r") as f:
            return f.read().strip()
    except Exception:
        return None
