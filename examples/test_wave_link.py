# -*- coding: utf-8 -*-
"""测试 nx_create_wave_link：把 链接体.prt 以固定时间戳记链入一个空的工序目标件。"""
import ctypes, ctypes.wintypes, time, sys

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
        err = ctypes.get_last_error()
        if err == 231:
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


def call(c, t=180.0, settle=0.6, retries=3):
    last = ""
    for i in range(retries):
        try:
            r = _pipe_call(c, t)
        except Exception as e:
            r = "ERR: " + str(e)
        if r:
            return r
        last = r
        print("  (empty response, retry %d/%d)" % (i + 1, retries))
        time.sleep(0.5)
    return last or "(no response)"


TARGET = "C:/code/ug_nx120_dev/nx_mcp_ws/工序_wave_test.prt"
SOURCE = "C:/code/ug_nx120_dev/nx_mcp_ws/链接体.prt"

print("== 1) WAVE 链入（源件=装配根，目标件=下游组件，固定时间戳记）==")
print(call("nx_create_wave_link %s %s" % (SOURCE, TARGET)))

print("== 2) 列目标件（工序件）实体 ==")
print(call("nx_list_bodies"))

print("== 3) 状态 ==")
print(call("nx_status"))

print("== 4) 存盘（工序件）==")
print(call("nx_save_part"))

print("== 5) 再次列实体（确认已落盘）==")
print(call("nx_list_bodies"))
print("DONE")
