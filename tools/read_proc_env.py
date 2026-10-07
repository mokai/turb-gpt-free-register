# -*- coding: utf-8 -*-
"""读取指定 pid 的启动参数与环境变量（macOS，KERN_PROCARGS2）。

用途：诊断「常驻服务被启动它的 shell 环境污染」这类问题——例如 WebUI 从 Agent
会话里被拉起，继承了 NODE_OPTIONS / CODEBUDDY_* / HTTP_PROXY 等，导致后续
spawn 出来的 node 子进程被沙箱 shim 劫持。见 skill `live-check-failure-triage`。

用法：
    .venv/bin/python tools/read_proc_env.py <pid> [关键字 ...]
    .venv/bin/python tools/read_proc_env.py 12345 NODE_OPTIONS CODEBUDDY PROXY

不带关键字时打印全部环境变量；关键字大小写不敏感，命中即打印。
本机 `ps eww` 在沙箱里不可用，用本脚本替代。
"""
from __future__ import annotations

import ctypes
import ctypes.util
import sys

_CTL_KERN = 1
_KERN_PROCARGS2 = 49


def _libc():
    return ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)


def read_raw(pid: int) -> bytes:
    libc = _libc()
    mib = (ctypes.c_int * 3)(_CTL_KERN, _KERN_PROCARGS2, pid)
    size = ctypes.c_size_t(0)
    if libc.sysctl(mib, 3, None, ctypes.byref(size), None, 0) != 0:
        raise OSError(ctypes.get_errno(), f"sysctl 取长度失败（pid={pid}）")
    buf = ctypes.create_string_buffer(size.value)
    if libc.sysctl(mib, 3, buf, ctypes.byref(size), None, 0) != 0:
        raise OSError(ctypes.get_errno(), f"sysctl 取数据失败（pid={pid}）")
    return buf.raw[: size.value]


def parse(data: bytes) -> tuple[list[str], list[str]]:
    """KERN_PROCARGS2 布局：[int argc][exec_path\\0][\\0 填充][argv...][\\0][env...]"""
    argc = int.from_bytes(data[:4], "little")
    rest = data[4:]
    rest = rest[rest.index(b"\x00"):]  # 丢掉 exec_path
    while rest[:1] == b"\x00":  # 跳过填充
        rest = rest[1:]
    parts = rest.split(b"\x00")
    argv = [p.decode("utf-8", "replace") for p in parts[:argc]]
    env = [p.decode("utf-8", "replace") for p in parts[argc:] if p]
    return argv, env


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    pid = int(sys.argv[1])
    needles = [n.lower() for n in sys.argv[2:]]

    argv, env = parse(read_raw(pid))
    print(f"=== PID {pid} ARGV ===")
    for item in argv:
        print("  " + item)

    print(f"=== PID {pid} ENV ===")
    for line in env:
        if needles and not any(n in line.lower() for n in needles):
            continue
        print("  " + line[:300])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
