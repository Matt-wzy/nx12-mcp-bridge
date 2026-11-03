# -*- coding: utf-8 -*-
"""Minimal non-destructive ping: confirm Loader responsive and list current part bodies."""
import ctypes, ctypes.wintypes, time

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.CreateFileW.restype = ctypes.wintypes.HANDLE  # 64 位句柄：不设 restype 会把 -1 截断成"有效"句柄
GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
OPEN_EXISTING = 3
INVALID = ctypes.wintypes.HANDLE(-1).value
PIPE = r"\\.\pipe\nx_mcp_loader"


def _pipe_call(cmd, timeout=60.0):
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


def call(c, settle=0.6, retries=3):
    last = ""
    for i in range(retries):
        try:
            r = _pipe_call(c)
        except Exception as e:
            r = "ERR: " + str(e)
        if r:
            return r
        last = r
        print("  (empty, retry %d/%d)" % (i + 1, retries))
        time.sleep(0.5)
    return last or "(no response)"


print("== status ==")
print(call("nx_status"))
print("== list bodies (current in-session part) ==")
print(call("nx_list_bodies"))
print("DONE")
