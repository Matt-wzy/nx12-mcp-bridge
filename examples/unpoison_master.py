# -*- coding: utf-8 -*-
"""unpoison_master.py — 一次性清洗主模型 链接体.prt 的坏组件引用。

背景：早期 wave link 测试 AddComponent 成功后源件被误存盘，主模型里永久
挂上了指向不存在文件的子组件（工序_place_A/B）。这些占位组件让会话部件表
永久注册了同名部件，导致后续 wave link 建同名工序件报"文件已存在/部件已存在"。

流程：干净会话 → 打开主模型 → 卸载 A/B 占位组件（保留有效的 工序_wave_test）
→ 存盘主模型 → 重新打开验证。
"""
import ctypes, ctypes.wintypes, time

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.CreateFileW.restype = ctypes.wintypes.HANDLE
GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
OPEN_EXISTING = 3
INVALID = ctypes.wintypes.HANDLE(-1).value
PIPE = r"\\.\pipe\nx_mcp_loader"


def _pipe_call(cmd, timeout=120.0):
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
        return "PIPE-FAIL %d" % ctypes.get_last_error()
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


def call(c, t=120.0, retries=3):
    r = ""
    for i in range(retries):
        try:
            r = _pipe_call(c, t)
        except Exception as e:
            r = "ERR: " + str(e)
        if r:
            return r
        time.sleep(0.5)
    return r or "(EMPTY)"


MASTER = "C:/code/ug_nx120_dev/nx_mcp_ws/链接体.prt"
A = "C:/code/ug_nx120_dev/nx_mcp_ws/工序_place_A.prt"
B = "C:/code/ug_nx120_dev/nx_mcp_ws/工序_place_B.prt"

print("== 1) 打开主模型 ==")
print(call("nx_open_part %s" % MASTER))

print("== 2) 卸载 A 占位组件（触发 wave 命令的清理路径）==")
# wave link 会 RemoveInstance；让它跑到 CreateComponentPart 前失败没关系，
# 卸载动作已经完成。用不存在的 body_index=99 让它在选实体阶段失败前先清理？
# 不行——选实体在卸载之后。直接用正常参数，接受后续步骤的失败。
print(call("nx_create_wave_link %s %s" % (MASTER, A)))
print(call("nx_create_wave_link %s %s" % (MASTER, B)))

print("== 3) 存盘主模型（此刻 A/B 实例已被卸载）==")
print(call("nx_save_part"))

print("== 4) 重新打开验证（组件列表应只剩 wave_test）==")
print(call("nx_close_part 0"))
print(call("nx_open_part %s" % MASTER))
print(call("nx_status"))
print("DONE")
