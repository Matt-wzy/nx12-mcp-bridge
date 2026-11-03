import sys, json, math
sys.path.insert(0, 'src')
from nx_mcp.loader_bridge import _pipe_call

def call(c, t=120.0):
    try:
        return _pipe_call(c, timeout=t)
    except Exception as e:
        return 'ERR: ' + str(e)[:400]

def js(c, t=120.0):
    r = call(c, t)
    if isinstance(r, dict):
        return r
    try:
        return json.loads(r)
    except Exception:
        return r

print('SK2:', js('nx_create_sketch XY'))
# D 形截面（中差不变）：顶边、右半圆、底边、左边
print('  L1:', call('nx_sketch_line SKETCH_2 -6.154 5 10 5'))
print('  L2:', call('nx_sketch_line SKETCH_2 10 -5 -6.154 -5'))
print('  L3:', call('nx_sketch_line SKETCH_2 -6.154 -5 -6.154 5'))
print('  ARC:', call('nx_sketch_arc SKETCH_2 10 0 5 -90 90'))
print('FINISH2:', call('nx_finish_sketch SKETCH_2'))

# 沿 +Z 拉伸切除，start_offset=7, depth=1.66（与原件一致，槽在 z=7..8.66）
print('EXTRUDE:', js('nx_extrude SKETCH_2 1.66 0 7 subtract BODY_1'))

# 定位 x=20 处 r≈49.98 的外缘圆边（倒斜角位置）
j = js('nx_list_edges 0')
target = None
for e in j.get('edges', []):
    if e['curve_type'] == 'Circular':
        m = e['midpoint']
        r = math.hypot(m[1], m[2])
        if abs(m[0]-20) < 0.5 and abs(r-49.98) < 0.05:
            target = e['index']
            break
print('CHAMFER_EDGE_IDX:', target)
if target is not None:
    print('CHAMFER:', js('nx_chamfer BODY_1 1 %d' % target))

# 锐边倒钝 R0.2（技术要求）
print('EDGE_BLEND:', js('nx_edge_blend BODY_1 0.2'))

print('FEATURES:', js('nx_list_features'))
