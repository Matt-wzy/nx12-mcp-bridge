# -*- coding: utf-8 -*-
"""单发定位：wave link 的空响应/重试问题（非幂等命令不能盲重试）"""
import ctypes, ctypes.wintypes, time

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.CreateFileW.restype = ctypes.wintypes.HANDLE  # 64 位句柄：不设 restype 会把 -1 截断成"有效"句柄
GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
OPEN_EXISTING = 3
INVALID = ctypes.wintypes.HANDLE(-1).value
PIPE = r"\\.\pipe\nx_mcp_loader"


def call1(cmd, timeout=240.0):
    """单次管道调用，不重试。返回原始响应（可为空）。"""
    h = None
    for _ in range(40):
        h = kernel32.CreateFileW(PIPE, GENERIC_READ | GENERIC_WRITE, 0, None, OPEN_EXISTING, 0, None)
        if h and h != INVALID:
            break
        if ctypes.get_last_error() == 231:
            kernel32.WaitNamedPipeW(PIPE, 500)
        else:
            time.sleep(0.3)
        h = None
    if not h or h == INVALID:
        return "PIPE-UNAVAILABLE %d" % ctypes.get_last_error()
    try:
        payload = (cmd + "\n").encode("utf-8")
        w = ctypes.wintypes.DWORD(0)
        kernel32.WriteFile(h, payload, len(payload), ctypes.byref(w), None)
        buf = bytearray()
        chunk = ctypes.create_string_buffer(65536)
        n = ctypes.wintypes.DWORD(0)
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not kernel32.ReadFile(h, chunk, 65536, ctypes.byref(n), None):
                break
            if n.value == 0:
                continue
            buf.extend(chunk.raw[: n.value])
            if b"\n" in buf:
                break
        return bytes(buf).decode("utf-8", "replace").strip()
    finally:
        kernel32.CloseHandle(h)


def probe(tag, cmd):
    r = call1(cmd)
    print("[%s] %s" % (tag, r if r else "(EMPTY RESPONSE)"))
    return r


print("--- 0) 状态 ---")
probe("status", "nx_status")

SRC = "C:/code/ug_nx120_dev/nx_mcp_ws/链接体.prt"
TGT = "C:/code/ug_nx120_dev/nx_mcp_ws/工序_place_A.prt"

print("--- 1) 打开源件 ---")
probe("open-src", "nx_open_part %s" % SRC)

print("--- 2) 单发 wave link（无偏移）---")
probe("wave", "nx_create_wave_link %s %s" % (SRC, TGT))

print("--- 3) 状态（工作部件应为目标件）---")
probe("status2", "nx_status")

print("--- 4) 列实体 ---")
probe("bodies", "nx_list_bodies")
print("PROBE DONE")
