import sys, json
sys.path.insert(0, 'src')
from nx_mcp.loader_bridge import _pipe_call

def call(c, t=90.0):
    try:
        r = _pipe_call(c, timeout=t)
        return r
    except Exception as e:
        return 'ERR: ' + str(e)[:400]

def js(c, t=90.0):
    r = call(c, t)
    if isinstance(r, dict):
        return r
    try:
        return json.loads(r)
    except Exception:
        return r

# 关闭当前打开的原件（不保存）
print('CLOSE:', call('nx_close_part 0'))

# 新建 转化图.prt（覆盖旧复制件）
print('CREATE:', js('nx_create_part C:/code/ug_nx120_dev/nx_mcp_ws/转化图_mid.prt'))

# 草图1：中差回转轮廓（SKETCH_000 中差版）
# 中差：Ø100->99.96 (r 50->49.98), Ø80->80.05 (r 40->40.025)，其余不变
print('SK1:', js('nx_create_sketch XY'))
lines = [
    (0,0,0,10),
    (0,10,20,10),
    (20,10,20,49.98),
    (20,49.98,40,49.98),
    (40,49.98,40,40.025),
    (40,40.025,60,40.025),
    (60,40.025,60,0),
    (60,0,0,0),
]
for (x1,y1,x2,y2) in lines:
    print('  LINE:', call('nx_sketch_line SKETCH_1 %g %g %g %g' % (x1,y1,x2,y2)))
print('FINISH1:', call('nx_finish_sketch SKETCH_1'))

# 绕 X 轴(y=0)旋转 360 度
print('REVOLVE:', js('nx_revolve SKETCH_1 0 0 1 0 360'))

# 验证：列出特征与关键外圆半径
print('FEATURES:', js('nx_list_features'))
j = js('nx_list_edges 0')
print('EDGES count:', j.get('edge_count'))
import math
maxr = {}
for e in j.get('edges', []):
    if e['curve_type'] == 'Circular':
        m = e['midpoint']
        r = math.hypot(m[1], m[2])
        maxr.setdefault(round(m[0]), []).append(round(r,2))
for xk in sorted(maxr):
    print('  x=%-4s radii=%s' % (xk, sorted(set(maxr[xk]))))
