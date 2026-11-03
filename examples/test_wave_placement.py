# -*- coding: utf-8 -*-
"""测试 nx_create_wave_link 的摆放偏移扩展（x=/y=/z=）+ nx_read_sketch。

场景：同一个主模型在不同位置链出两个工序件（产线工位布局）。
目标文件若已存在会被命令删除重建，所以用独立文件名。

⚠️ 2026-11-04：摆放参数当前被 Loader 显式闸掉（返回清晰错误）——
   变换矩阵的行/列主序未完成几何验证前不开放（步骤见
   NX_MCP_Loader.cs CreateWaveLink 内注释）。本脚本第 2 步会收到
   该报错，属预期；原位链路（第 1 步）应全绿。
"""
import ctypes
import ctypes.wintypes  # noqa: F401  (must be imported before use)
import time

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.CreateFileW.restype = ctypes.wintypes.HANDLE  # 64 位句柄：不设 restype 会把 -1 截断成"有效"句柄
GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
OPEN_EXISTING = 3
INVALID = ctypes.wintypes.HANDLE(-1).value
PIPE = r"\\.\pipe\nx_mcp_loader"


def _pipe_call(cmd, timeout=180.0):
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
        raise RuntimeError("pipe unavailable %d" % ctypes.get_last_error())
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


def call(c, t=180.0, retries=3):
    r = ""
    for i in range(retries):
        try:
            r = _pipe_call(c, t)
        except Exception as e:
            r = "ERR: " + str(e)
        if r:
            return r
        print("  (empty, retry %d)" % (i + 1))
        time.sleep(0.5)
    return r or "(no response)"


SRC = "C:/code/ug_nx120_dev/nx_mcp_ws/链接体.prt"
# 目标件名可用环境变量覆盖（默认 place_A/B；若这些名字曾被毒化引用过，
# 换新名字即可——NX 会话的部件名注册表会记住历史引用名）
import os as _os
TGT_A = _os.environ.get("VG_TGT_A") or "C:/code/ug_nx120_dev/nx_mcp_ws/工序_place_A.prt"
TGT_B = _os.environ.get("VG_TGT_B") or "C:/code/ug_nx120_dev/nx_mcp_ws/工序_place_B.prt"

print("== 0) 状态 ==")
print(call("nx_status"))

print("== 1) 原位链出（无偏移，回归）+ 立即存盘 ==")
print(call("nx_create_wave_link %s %s" % (SRC, TGT_A)))
print(call("nx_list_bodies"))
print(call("nx_save_part"))

print("== 2) 偏移链出（x=100 y=50）+ 立即存盘 ==")
print(call("nx_create_wave_link %s %s x=100 y=50" % (SRC, TGT_B)))
print(call("nx_list_bodies"))
print(call("nx_save_part"))

print("== 3) 读主模型草图（nx_read_sketch）==")
print(call("nx_open_part %s" % SRC))
print(call("nx_list_sketches"))
print(call("nx_read_sketch 0"))

print("== 4) 状态 ==")
print(call("nx_status"))
print("DONE")
