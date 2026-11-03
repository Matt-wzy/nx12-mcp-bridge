import sys, json, math, os, shutil, time
sys.path.insert(0, 'src')
# Use the standalone pipe call (avoid importing the mcp-dependent module)
import ctypes
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
GENERIC_READ=0x80000000; GENERIC_WRITE=0x40000000; OPEN_EXISTING=3
INVALID=ctypes.wintypes.HANDLE(-1).value
PIPE=r"\\.\pipe\nx_mcp_loader"

def _one_pipe(cmd):
    h=None
    for _ in range(40):
        h=kernel32.CreateFileW(PIPE, GENERIC_READ|GENERIC_WRITE,0,None,OPEN_EXISTING,0,None)
        if h and h!=INVALID: break
        err=ctypes.get_last_error()
        if err==231: kernel32.WaitNamedPipeW(PIPE,600)
        else: time.sleep(0.3)
        h=None
    if not h or h==INVALID: raise RuntimeError("pipe unavailable %d"%ctypes.get_last_error())
    try:
        payload=(cmd+"\n").encode(); w=ctypes.wintypes.DWORD(0)
        okw=False
        for _ in range(5):
            okw=bool(kernel32.WriteFile(h,payload,len(payload),ctypes.byref(w),None))
            if okw: break
            time.sleep(0.25)
        if not okw: raise RuntimeError("write failed")
        buf=bytearray(); chunk=ctypes.create_string_buffer(65536); n=ctypes.wintypes.DWORD(0)
        while True:
            if not kernel32.ReadFile(h,chunk,65536,ctypes.byref(n),None): break
            if n.value==0: continue
            buf.extend(chunk.raw[:n.value])
            if b"\n" in buf: break
        return bytes(buf).decode("utf-8","replace").strip()
    finally:
        kernel32.CloseHandle(h)

def _pipe_call(cmd, timeout=180.0):
    # retry on empty response (single-instance pipe connect/read race)
    last=None
    for attempt in range(4):
        try:
            r=_one_pipe(cmd)
        except Exception as e:
            r='ERR: '+str(e)[:300]
        if r and r!='':
            return r
        last=r
        time.sleep(0.8)
    return last if last is not None else ''

def call(c, t=180.0):
    try:
        r=_pipe_call(c, t)
        time.sleep(0.4)  # let the single-instance pipe re-arm
        return r
    except Exception as e:
        return 'ERR: '+str(e)[:500]

def js(c, t=180.0):
    r=call(c,t)
    if isinstance(r,dict): return r
    try: return json.loads(r)
    except Exception: return {"raw":r}

PART="C:/code/ug_nx120_dev/nx_mcp_ws/转化图.prt"
PDF ="C:/code/ug_nx120_dev/nx_mcp_ws/转化图.pdf"
OLD ="C:/code/ug_nx120_dev/nx_mcp_ws/转化图_old.prt"

print("== close part ==")
print(call('nx_close_part 0'))
# remove old file so nx_create_part starts fresh
if os.path.exists(PART):
    if os.path.exists(OLD): os.remove(OLD)
    os.rename(PART, OLD)
    print("renamed old -> 转化图_old.prt")

print("== create part ==")
print(js('nx_create_part '+PART))

# ---- SKETCH_1 : mid-tolerance revolve profile (all 3 diameters at mid) ----
# left  Ø20 +0.1/0   -> mid 20.05  (r 10.025)
# mid   Ø100 +0.02/-0.1 -> mid 99.96 (r 49.98)
# right Ø80  -0.1/+0.2  -> mid 80.05 (r 40.025)
print("== sketch1 ==")
print(js('nx_create_sketch XY'))
lines=[
 (0,0,0,10.025),
 (0,10.025,20,10.025),
 (20,10.025,20,49.98),
 (20,49.98,40,49.98),
 (40,49.98,40,40.025),
 (40,40.025,60,40.025),
 (60,40.025,60,0),
 (60,0,0,0),
]
for (x1,y1,x2,y2) in lines:
    print('  L:', call('nx_sketch_line SKETCH_1 %g %g %g %g'%(x1,y1,x2,y2)))
print('finish1:', call('nx_finish_sketch SKETCH_1'))
print('revolve:', js('nx_revolve SKETCH_1 0 0 1 0 360'))

# ---- SKETCH_2 : D-slot (free dims, nominal) ----
print("== sketch2 D-slot ==")
print(js('nx_create_sketch XY'))
print('  L1:', call('nx_sketch_line SKETCH_2 -6.154 5 10 5'))
print('  L2:', call('nx_sketch_line SKETCH_2 10 -5 -6.154 -5'))
print('  L3:', call('nx_sketch_line SKETCH_2 -6.154 -5 -6.154 5'))
print('  ARC:', call('nx_sketch_arc SKETCH_2 10 0 5 -90 90'))
print('finish2:', call('nx_finish_sketch SKETCH_2'))
print('extrude subtract:', js('nx_extrude SKETCH_2 1.66 0 7 subtract BODY_1'))

# ---- chamfer at x=20 shoulder (r~49.98) ----
j=js('nx_list_edges 0')
edges=j.get('edges',[])
target=None
for e in edges:
    if e['curve_type']=='Circular':
        m=e['midpoint']; r=math.hypot(m[1],m[2])
        if abs(m[0]-20)<0.5 and abs(r-49.98)<0.1:
            target=e['index']; break
print('CHAMFER_EDGE_IDX:', target)
if target is not None:
    print('chamfer:', js('nx_chamfer BODY_1 1 %d'%target))

# ---- 锐边倒钝 R0.2 (edge blend all sharp edges) ----
print('edge_blend:', js('nx_edge_blend BODY_1 0.2'))

print("== features ==")
print(js('nx_list_features'))

# ---- save model ----
print('save:', call('nx_save_part'))

# ================= DRAWING =================
print("== drawing sheet ==")
print(js('nx_create_drawing_sheet A3_1 A3 first 1 Top'))
views=js('nx_list_drafting_views')
print('views:', views)
# base view id (expect Top@1)
vid='Top@1'
vv=views.get('message','') if isinstance(views,dict) else ''
print('views msg:', vv)

# re-read edges for dimension placement (after blend)
j=js('nx_list_edges 0')
edges=j.get('edges',[])
def find_circle(r_target, xmin, xmax, tol=0.3):
    best=None; bd=1e9
    for e in edges:
        if e['curve_type']!='Circular': continue
        m=e['midpoint']; r=math.hypot(m[1],m[2]); x=m[0]
        if xmin<=x<=xmax and abs(r-r_target)<tol:
            d=abs(r-r_target)
            if d<bd: bd=d; best=e['index']
    return best
def find_x(x_target, tol=0.6, prefer='Circular'):
    best=None; bd=1e9
    for e in edges:
        m=e['midpoint']; x=m[0]
        if abs(x-x_target)<tol:
            d=abs(x-x_target)
            if d<bd: bd=d; best=e['index']
    return best

e20  = find_circle(10.025, -1, 1)      # left end face  -> Ø20.05
e100 = find_circle(49.98, 20, 40)      # Ø99.96 section
e80  = find_circle(40.025, 40, 61)     # right end face -> Ø80.05
e0   = find_x(0)                       # left end
e20x = find_x(20)                      # step at x=20
e40x = find_x(40)                      # step at x=40
e60x = find_x(60)                      # right end
print('edge picks: e20=%s e100=%s e80=%s e0=%s e20x=%s e40x=%s e60x=%s'%(e20,e100,e80,e0,e20x,e40x,e60x))

print("== diameter dims (mid + bilateral tol) ==")
# Ø20.05  +0.1/0
print('D20:', js('nx_create_diameter_dimension %s 0 %s 120 95 0.1 0 D20'% (vid, e20)))
# Ø99.96  +0.02/-0.1
print('D100:', js('nx_create_diameter_dimension %s 0 %s 230 120 0.02 -0.1 D100'%(vid, e100)))
# Ø80.05  -0.1/+0.2
print('D80:', js('nx_create_diameter_dimension %s 0 %s 330 115 -0.1 0.2 D80'%(vid, e80)))

print("== linear dims (free, GB/T 1804-m) ==")
# total length 60 : between left end (x0) and right end (x60)
print('LEN60:', js('nx_create_dimension %s 0 %s %s 250 25 horizontal 0 0 L60'%(vid, e0, e60x)))
# left section length 20 : x0 -> x20
print('LEN20a:', js('nx_create_dimension %s 0 %s %s 150 70 horizontal 0 0 L20a'%(vid, e0, e20x)))
# mid section length 20 : x20 -> x40
print('LEN20b:', js('nx_create_dimension %s 0 %s %s 250 70 horizontal 0 0 L20b'%(vid, e20x, e40x)))

print("== notes / roughness ==")
print('NOTE:', js('nx_create_note 70 210 技术要求：|1、未注尺寸公差按GB/T 1804-m；|2、锐边倒钝R0.2±0.1；'))
print('ROUGH:', js('nx_create_roughness 300 200 6.3'))
print('update:', call('nx_update_views'))

print("== list dims ==")
print(js('nx_list_dimensions'))
print('save:', call('nx_save_part'))
print('export pdf:', js('nx_export_pdf %s A3_1'%PDF))
print('DONE')
