# 拓扑安全手册（Topology Safety）

> 本文件是 `nx-mcp-modeling-planner` 的执行铁律：**所有 edge/face index 都是
> 临时数据**，识别靠几何，操作靠实时重查。上一轮 V2-TEXT-CHALLENGE 的核心
> 事故（R6 与 R8 复用同一批边索引，导致 R8 错位落在筋上）就是违反本手册造成的。

## 1. 失效清单

以下任一操作**完成后**，此前获得的 edge index / face index 一律立即失效：

| 操作 | 说明 |
|---|---|
| `nx_unite` / Subtract | 拓扑合并，面边重排；tool body 消失 |
| `nx_hole` / `nx_counterbore_hole` / `nx_countersink_hole` | 布尔减 |
| `nx_shell` | 抽壳重建内外表面 |
| `nx_linear_pattern` / `nx_circular_pattern` | 生成新 body 并改变索引域 |
| `nx_mirror` | 生成新 body |
| `nx_edge_blend` / `nx_chamfer` | 边被替换为圆弧/斜边 |
| 任何改变实体拓扑的操作 | 一律视为失效 |

失效即失效，**不存在“侥幸可用”**。index 只是 `GetEdges()/GetFaces()` 在
调用时刻的数组下标，与几何身份无关。

## 2. 边操作协议（Edge Protocol）

需要连续多个边操作时（如 R6 → R8 → C2），必须逐次执行：

```
nx_list_edges(body_id)          ← tool_args 只传 {body_id}
  → 按 selection_criteria 匹配本组目标（得到 index 集合 A）
  → 执行操作（blend/chamfer，edge_indices = A）
nx_list_edges(body_id)          ← 拓扑已变，必须重查
  → 按 selection_criteria 匹配下一组目标（得到 index 集合 B）
  → 执行操作（edge_indices = B）
```

**禁止**：一次 `nx_list_edges` 后保存 R6、R8、C2 三组 index 再连续使用。
**禁止**：把 selection_criteria（match/expected_count 等）原样传给 NX_MCP。

## 3. 面操作协议（Face Protocol）

Shell 前必须：

```
nx_list_faces(body_id)          ← tool_args 只传 {body_id}
  → 根据 selection_criteria（centroid / area / normal / topology）确认 remove face
nx_shell(body_id, thickness, remove_face_index=<确认的 index>, inward=true)
```

**禁止猜 face index**，禁止沿用 Shell 之前任何面 index。

## 4. 几何匹配条件（识别标准，禁止按 index 数字判断）

> 以下全部是 `selection_criteria`（Agent 在返回结果中筛选），**不是工具参数**。

### 边（`nx_list_edges` 返回字段）
| 特征 | 用途示例 |
|---|---|
| `curve_type` | "Linear" / "Circular" / "Elliptical" / "Conical" / ... |
| `direction` | 仅 Linear：X/Y/Z（如竖直棱 → "Z"） |
| `length` | 竖直棱长 12 → 12.0；Ø104 圆边 → 2π×52 ≈ 326.73 |
| `bbox_min` / `bbox_max` | 仅 Linear 精确；如底座角棱 → x∈{±90}、y∈{±60}、z∈[0,12] |
| `midpoint` | 直线中点在棱中点；圆边上的任意顶点 z（如法兰顶圆 → z=56） |
| `adjacent_faces` | 辅助确认（外角棱=2） |

### 面（`nx_list_faces` 返回字段）
| 特征 | 用途示例 |
|---|---|
| `face_type` | "Planar" / "Cylindrical" / "Swept" / "Conical" / "Toroidal" / ... |
| `centroid` | 顶面 (0,0,48)；法兰顶环面 (0,0,56)；凸台顶面 (x,y,20) |
| `area` | **只作辅助**（见下）；不作为单点失败条件 |
| `normal` | 仅 Planar：顶面 [0,0,1]、底面 [0,0,-1] |
| 邻接边数 | 辅助确认 |

### 4.1 边筛选稳定语法（Planner 强制）

Runner 已验证的 edge criteria 关键语法：

- `direction`：只允许 `"X"` / `"Y"` / `"Z"` / `"OTHER"` 字符串，
  **不是方向向量**。
- `midpoint`：完整 `[x,y,z]`；只筛 Z 高度用 `midpoint_z`。
- `corners_xy`：`[[x1,y1],[x2,y2],...]`，用于一次选择多个指定 XY
  位置的 Linear 竖边。
- `bbox_z`：可用 `{"min":z0,"max":z1}` 或区间形式约束实际 Z 范围。
- `linear_only:true`：当不需要依赖 curve_type 文本时可直接排除曲线边。
- **完整圆边类型语义**：当前 Loader 实测中，完整圆通常在 `nx_list_edges`
  报告为 `"Elliptical"`，而不是 `"Circular"`。为兼容版本差异，Planner
  对完整圆边必须优先使用候选 `["Elliptical","Circular"]`。
- **曲线边无可靠 bbox**：Circular / Elliptical / Conical 等曲线边禁止
  `bbox` / `bbox_x` / `bbox_y` / `bbox_z` / `corners_xy`。

**板件四角 R 圆角的推荐模板：**

```json
{
  "tool": "nx_list_edges",
  "tool_args": {"body_id": "body_main"},
  "selection_criteria": {
    "curve_type": "Linear",
    "direction": "Z",
    "corners_xy": [[-90,-60],[-90,60],[90,-60],[90,60]],
    "bbox_z": {"min": 0, "max": 14}
  },
  "expectation": {"count": 4}
}
```

随后 `nx_edge_blend.edge_indices` 引用该 selection 的全部命中项。

**两个 Ø34 凸台顶外圆做 C1.5 倒角的推荐模板：**

```json
{
  "tool": "nx_list_edges",
  "tool_args": {"body_id": "body_main"},
  "selection_criteria": {
    "curve_type": ["Elliptical", "Circular"],
    "length": {"value": 106.81, "tol": 1.0},
    "midpoint_z": {"value": 36.0, "tol": 0.5}
  },
  "expectation": {"count": 2}
}
```

随后 `nx_chamfer.edge_indices` 引用这两个命中边。

**禁止模板：**
- `"direction":[0,0,1]`
- 四角分别 group + 精确 `midpoint`，只为表达不同 XY
- `midpoint_x` / `midpoint_y`
- `{"groups": {...}}` 这种额外包装

只有各组确实需要**不同类型/长度/高度等不同条件**时才使用 group mode。

### Loader Face Semantics Contract（冻结实测，2026-09-18）

> 本小节记录当前 Loader/NX 实机语义；**Runner 代码禁止硬编码这些数值/类型**，
> Planner 也必须从每个零件的最终几何关系推导，不得把建模命令输入参数
> 直接当作最终拓扑验证参数。

1. **布尔切孔侧面优先按 `Swept` 识别**（经 `nx_hole` / `nx_counterbore_hole`
   等布尔减生成的孔侧面，`nx_list_faces` 中通常报告为 `Swept`，
   而不是 `Cylindrical`）。
2. **`Cylindrical` 不能作为"孔侧面"的固定判断类型**；`Cylindrical`
   也可能出现在 Edge Blend 等其他曲面上。
3. 验证孔是否存在时：
   - `face_type` 使用 `["Swept", "Cylindrical"]` 候选（当前实测为 Swept）；
   - 以 `centroid_radius`（质心 xy 到轴心距离）+ `centroid_z`（侧壁实际材料
     区间中点）+ `count` 为主要判据。
4. 后续若 Loader 版本对孔侧面的报告类型发生变化，以本契约的更新为准；
   **不允许每张 plan 自行猜测 face_type**。

### 最终拓扑验证几何（必须基于最终材料区间）

**FAST 验证几何必须基于最终实体中真实存在的材料区间和最终拓扑，
而不是直接复制建模命令输入参数。**

规划验证时必须考虑：
- Boolean subtract 后哪些面仍然存在（被截断的侧壁只保留实际区间）；
- counterbore / countersink 是否截断较小孔的侧壁（Ø16 沉孔段把 Ø9 侧壁
  截为 Z=5..14，Ø9 centroid_z≈9.5，而不是 Ø9 通孔全高 14 的 7.0）；
- hole depth 超过局部材料高度时，最终面只存在于实际材料区间
  （depth=36 但 [0,±22] 处实体只有 Z=0..24 → 孔侧壁 centroid_z≈12.0，
  而不是 18.0）；
- 后续 Unite / subtract / blend / chamfer 是否改变面范围
  （凸台顶面 C1.5 后变小但 centroid 不变；口袋底面不变）。

推算方法：**材料区间 = 该位置实际实体 Z 范围 ∩ 孔工具 Z 范围**；
孔侧壁 centroid_z = 材料区间中点；centroid_radius = 孔中心到轴心距离
（质心 xy 落在孔轴上）。

**面积值随加工顺序变化（使用前必须核对当前特征状态）**：
| 时刻 | 面 | 面积 |
|---|---|---|
| Shell 前（未加工） | 圆柱顶面 Ø84 | π×42² ≈ 5541.77 |
| C2 之前（未倒角） | 法兰顶环面 Ø104/Ø72 | π(52²-36²) ≈ 4423.36 |
| **C2×45° 之后** | **法兰顶环面 Ø100/Ø72** | **π(50²-36²) ≈ 3782.48** |
| Ø6.6 孔之前 | 凸台顶面 Ø18 | π×9² ≈ 254.47 |
| **Ø6.6 孔之后** | **凸台顶环面 Ø18/Ø6.6** | **π(9²-3.3²) ≈ 220.26** |

**规则：FAST 模式面积只作辅助判断，不作为单点失败条件；优先验证 centroid /
Z 高度 / 数量 / face type / 直径相关几何。**

## 5. 常见误判与规避

| 陷阱 | 规避 |
|---|---|
| R8 选到筋/耳板其他竖直边 | 匹配条件必须同时限定 bbox x∈{±115} **且** y∈{±22}，不可只按方向+长度 |
| 板件四角圆角查边返回空 | 不用方向向量或四组精确 midpoint；使用 `direction="Z" + corners_xy + bbox_z/midpoint_z`，一次选齐四条边 |
| 完整圆边查找为空 | 当前 Loader 完整圆常报 `Elliptical`；不要只写 `Circular`，用 `["Elliptical","Circular"] + length + midpoint_z + count` |
| 凸台顶圆边查找为空 | 圆边无可靠 bbox；禁止 `bbox_x/bbox_y/bbox_z`，改用候选 curve_type + `length + midpoint_z + count` |
| 法兰顶圆与底圆混淆 | 圆边无 bbox；用候选 `["Elliptical","Circular"]` + `length`（Ø104→326.73）+ `midpoint_z`（顶=56，底=48） |
| Ø104 外圆与 Ø72 内圆混淆 | 长度不同（326.73 vs 226.19） |
| 圆角后角棱消失 | 先 R6 后找 R8 时，R8 边仍在（互不相邻），但必须重查 |
| Shell 移除面猜错 | 顶面唯一判定：Planar + centroid.z=48 + normal=[0,0,1]（面积≈5541.77 仅辅助） |
| 用加工前面积做最终验证 | C2 后法兰顶环面是 **3782.48** 不是 4423.36；Ø6.6 后凸台顶环面是 **220.26** 不是 254.47 |
| 只相切却 Unite 成主体 | 底板顶边与 R35 圆立板底端单点相切 → Unite 不可靠、工具体未并入、后续孔"工具体完全在目标体外"；此类主体改**单一连续闭合轮廓一次拉伸**（SKILL.md §3.0 Profile-First），禁止零面积接触 Unite |

## 6. 验证纪律

**FAST（默认）只保留**：
- A. `nx_list_bodies` → 最终 Body 数量 = 1
- B. Bounding Box / 极值：X / Y / Z（底面与顶面 face centroid 定 Z；线性边
  bbox 定 X/Y）
- C. 少量关键结构确认：法兰顶部 Z=56、凸台数量、沉头锥面数量、关键孔数量合理
- D. Save 成功
- E. STEP 导出成功且文件非空

**面积只作辅助**：禁止因单个 face area 与理论值细微差异进入 DIAGNOSTIC。
**只有以下情况才进入 DIAGNOSTIC**：Body 数量错误；Bounding Box 明显错误；
关键特征缺失；Save / STEP 失败。

**默认禁止**：解析 STEP 文本、搜 AXIS2_PLACEMENT_3D、遍历 STEP 圆弧、
对已确认尺寸再做深度几何审计。
