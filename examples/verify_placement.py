# -*- coding: utf-8 -*-
"""verify_placement.py — 几何验证摆放：对比原位件与偏移件的边坐标"""
import ctypes, ctypes.wintypes, time

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.CreateFileW.restype = ctypes.wintypes.HANDLE
GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
OPEN_EXISTING = 3
INVALID = ctypes.wintypes.HANDLE(-1).value
PIPE = r"\\.\pipe\nx_mcp_loader"


def call(c, timeout=120.0):
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
        return "PIPE-FAIL"
    try:
        payload = (cmd_buf(c)).encode("utf-8") if False else (c + "\n").encode("utf-8")
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


print("== 打开原位件 place_I ==")
print(call("nx_open_part C:/code/ug_nx120_dev/nx_mcp_ws/工序_place_I.prt"))
r1 = call("nx_list_edges BODY_1")
print("edges(I) 前 200 字符:", r1[:200])

print("== 打开偏移件 place_J ==")
print(call("nx_open_part C:/code/ug_nx120_dev/nx_mcp_ws/工序_place_J.prt"))
r2 = call("nx_list_edges BODY_1")
print("edges(J) 前 200 字符:", r2[:200])

# 粗略比较：解析 JSON 里的坐标和
import json
def coord_sum(txt):
    try:
        d = json.loads(txt)
    except Exception:
        return None
    s = 0.0
    def walk(o):
        nonlocal s
        if isinstance(o, dict):
            for k, v in o.items():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
        elif isinstance(o, (int, float)):
            s += abs(float(o))
    walk(d)
    return round(s, 1)

a, b = coord_sum(r1), coord_sum(r2)
print("edges(I) 坐标绝对值和:", a)
print("edges(J) 坐标绝对值和:", b)
if a is not None and b is not None and abs(a - b) > 1.0:
    print("=> 两件几何坐标不同，摆放生效 ✓")
elif a is not None:
    print("=> 两件坐标一致，摆放未生效 ✗")
print("VERIFY DONE")
