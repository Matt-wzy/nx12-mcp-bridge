# NX_MCP 已验证工具规则（32 certified tools）

> 本文件为 `nx-agent` 建模规划模块 的规则底稿。只描述**已冻结、已验证**的
> 32 个 certified 工具（`src/nx_mcp/certified.py` 的 `CERTIFIED_TOOL_NAMES`）。
> 不得修改 NX_MCP-Enhanced、C# Loader 或现有环境。

## 1. 工具清单（按用途分组）

### 会话与文件
| 工具 | 参数 | 说明 |
|---|---|---|
| `nx_status` | `{}` | 查询 bridge / NX 版本 / 活动零件状态 |
| `nx_create_part` | `path`（工作区相对路径，.prt）, `units`="mm"/"inch" | 创建工作区内零件 |
| `nx_open_part` | `path` | 打开工作区内零件 |
| `nx_save_part` | `{}` | 保存活动工作零件 |
| `nx_close_part` | `save`=true | 关闭活动零件（默认先保存） |
| `nx_export_step` | `path`（工作区相对路径，.step） | 导出 STEP |
| `nx_release` | `{}` | 清除当前任务状态并恢复正常 NX 交互；resident Loader / named pipe 保持 ready |
| `nx_undo` | `{}` | 撤销最近一次 NX MCP 操作 |
| `nx_fit_view` | `{}` | 适配建模视图 |

### 查询（只读，不改变拓扑；**真实参数仅 `body_id`（或空），其余字段一律不能传给工具**）
| 工具 | 参数 | 返回要点 |
|---|---|---|
| `nx_list_bodies` | `{}` | body 列表与 id；**Unite 后 tool body 会从列表消失** |
| `nx_list_features` | `{}` | 特征列表 |
| `nx_list_sketches` | `{}` | 草图列表 |
| `nx_list_edges` | `body_id` | 每条边：`index`、`tag`、`curve_type`（Linear/Circular/…）、`start`、`end`、`midpoint`、`length`、`bbox_min`/`bbox_max`（**仅 Linear 边精确**，曲线为 null）、`direction`（X/Y/Z/OTHER，仅 Linear）、`adjacent_faces` |
| `nx_list_faces` | `body_id` | 每个面：`index`、`tag`、`face_type`（Planar/Cylindrical/Conical/Toroidal/…）、`centroid`、`area`、`normal`（仅 Planar）、邻接边数 |

### 草图
| 工具 | 参数 | 说明 |
|---|---|---|
| `nx_create_sketch` | `plane`="XY"/"XZ"/"YZ" | 创建并激活真实主基准面草图；局部二维坐标映射见下方“主平面契约” |
| `nx_sketch_line` | `sketch_id`, `start`{x,y}, `end`{x,y} | 直线 |
| `nx_sketch_rectangle` | `sketch_id`, `corner1`, `corner2` | 矩形（两点） |
| `nx_sketch_circle` | `sketch_id`, `center`{x,y}, `diameter` | 圆（中心+直径） |
| `nx_sketch_arc` | `sketch_id`, `center`, `radius`, `start_angle`, `end_angle` | 圆弧（角度制） |
| `nx_finish_sketch` | `sketch_id` | 结束草图，之后才能挤出 |

### 主平面契约（强制）

`nx_create_sketch` 的二维 `{x,y}` 是**草图局部坐标**，固定映射为：

| plane | 局部 x | 局部 y | `nx_extrude reverse=false` |
|---|---|---|---|
| `XY` | 全局 X | 全局 Y | +Z |
| `XZ` | 全局 X | 全局 Z | +Y |
| `YZ` | 全局 Y | 全局 Z | +X |

- `reverse=true` 反转上表挤出方向。
- `start_offset` 沿对应挤出轴偏移，不再固定解释为 Z。
- `nx_sketch_line / rectangle / circle / arc` 均必须遵守同一局部坐标映射。
- `nx_revolve` 的二维旋转轴也在所属 sketch 的局部平面内解释。
- `nx_hole / nx_counterbore_hole / nx_countersink_hole` 当前仍是 **Z 轴孔工具**；
  X/Y 轴方向的侧板/竖板孔必须在 `XZ` / `YZ` 主平面画圆，再用
  `nx_extrude(operation="subtract")` 沿该平面法向切除，禁止拿 Z 轴孔工具硬套。
- 当前 32 个 certified tools **没有真实螺纹建模工具**。螺纹规格不能由 Planner 临场等同于任意普通孔直径，也不能用同轴的 clearance/through hole 代替。
- 上游输入明确给出允许建模的 `surrogate_geometry` 时优先使用；否则只允许使用机器参数化 resolver：解析 metric nominal diameter/pitch，bare designation 的 pitch 查独立标准粗牙 metadata，tap-drill surrogate 统一按 `nominal_diameter - pitch` 计算。禁止 thread-size → 最终孔径硬编码。resolver 不提供 axis、center、depth、range、count、side 或 ownership；这些字段未闭合时仍须阻断。

### 实体创建
| 工具 | 参数 | 说明 |
|---|---|---|
| `nx_extrude` | `sketch_id`, `distance`, `reverse`=false, `start_offset`=0, `operation`="create"/"subtract", `target_body_id` | 沿该草图主平面的法向轴挤出；`reverse=false`：XY→+Z、XZ→+Y、YZ→+X；`start_offset` 沿同一挤出轴计 |
| `nx_revolve` | `sketch_id`, `axis_start`{x,y}, `axis_end`{x,y}, `angle`=360, `reverse` | 旋转体（仅 create） |

### 特征（均改变拓扑 → 使旧 index 失效）
| 工具 | 参数 | 说明 |
|---|---|---|
| `nx_shell` | `body_id`, `thickness`, `remove_face_index`, `inward`=true | 抽壳。**先 `nx_list_faces` 按几何确认移除面**；`inward=true` 保持外尺寸 |
| `nx_hole` | `body_id`, `center`{x,y}, `diameter`, `depth`, `start_offset`=0 | 直孔（圆草图+布尔减，Z 从 start_offset 到 start_offset+depth） |
| `nx_counterbore_hole` | `body_id`, `center`, `hole_diameter`, `hole_depth`, `counterbore_diameter`, `counterbore_depth`, `start_offset`=0 | 沉孔：Øcb 段在 Z=start_offset..+cb_depth，Øhole 段继续到底。要求 cb_dia>hole_dia、cb_depth<hole_depth |
| `nx_countersink_hole` | `body_id`, `center`, `hole_diameter`, `hole_depth`, `countersink_diameter`, `countersink_angle`=90, `start_offset`=0 | 沉头：锥体**大端（countersink_diameter）位于 Z=start_offset 底面**，小端=hole_diameter；锥深=(cs_dia-hole_dia)/2/tan(angle/2) |
| `nx_edge_blend` | `body_id`, `radius`, `edge_indices`=None | 圆角。edge_indices 为空=全部边。**index 为 list_edges 输出时的 GetEdges() 下标** |
| `nx_chamfer` | `body_id`, `offset`, `edge_indices`=None | 对称倒角，同上 |
| `nx_unite` | `target_body_id`, `tool_body_ids`[列表] | 布尔加；**target 保留 id，tool bodies 被消费消失**。可一次传入多个 |

### 阵列与镜像（均生成独立 body，不自动 Unite）
| 工具 | 参数 | 说明 |
|---|---|---|
| `nx_circular_pattern` | `body_id`, `axis`="X"/"Y"/"Z", `center`{x,y,z}, `count`, `angle`, `reverse` | **count 含原始实体**；angle=360 均匀整圆；不重复末位实例 |
| `nx_linear_pattern` | `body_id`, `direction`="X"/"Y"/"Z", `count`, `spacing`, `reverse` | **count 含原始实体**；spacing 为相邻实例距离 |
| `nx_mirror` | `body_id`, `plane`="XY"/"XZ"/"YZ", `offset`=0 | 镜像；plane 为基准面，offset 沿法向平移；生成独立 body，需自行 Unite |

## 2. 已验证稳定路径（优先采用）

- **Profile-First（最高优先级）**：主视图/侧视图为连续二维外轮廓、厚度基本
  恒定的支架/托架件 → **单一连续闭合草图（直线+圆弧）→ 一次 `nx_extrude`
  形成主体 → 剖视减料 → 孔 → Fillet/Chamfer**。禁止拆成多个局部实体后
  Unite。若两实体仅单点/单线/零面积接触，**禁止依赖 Unite 形成主体**。
- **对称耳板/侧板**：建一侧 → `nx_mirror` → 与主体一次性 `nx_unite`。
- **环形法兰**：同草图双同心圆（Ø外/Ø内）→ `nx_extrude`（区域模式出环）。
- **薄壁壳体**：独立圆柱/主体 → `nx_list_faces` 按 centroid/normal 确认开面 →
  `nx_shell` → 再与底座 Unite。禁止先 Unite 再 Shell。
- **圆周分布凸台**：建单个 → `nx_circular_pattern`（count=总数, angle=360）→
  一次性 Unite 全部。
- **阵列筋板**：建单个 → `nx_linear_pattern` → 镜像 → 一次性 Unite 全部。
- **圆角/倒角收尾**：全部 Boolean 与孔完成后再做；每次操作前重新
  `nx_list_edges` 按几何匹配选边。

## 3. 行为细节（影响计划正确性）

1. **index = GetEdges()/GetFaces() 实时下标**：任何拓扑操作后顺序即变，
   旧 index 无意义（见 `topology-safety.md`）。
2. **曲线边 bbox 为 null / 不可靠**：只对 Linear 边使用 bbox。
   当前 Loader 实测完整圆边在 `nx_list_edges` 中通常报告为 `Elliptical`，
   **不是固定的 `Circular`**。因此完整圆边统一优先使用
   `curve_type:["Elliptical","Circular"]` + `length` +
   `midpoint_z` + `expectation.count`。
   Circular / Elliptical / Conical 等曲线边禁止使用
   `bbox / bbox_x / bbox_y / bbox_z / corners_xy`。
3. **计数器参数约束**：counterbore：cb_dia>hole_dia、cb_depth<hole_depth；
   countersink：cs_dia>hole_dia、cs_angle∈(0,180)、锥深<hole_depth。
4. **Unite 消费 tool body**：Unite 后不要再引用 tool body id。
5. **Extrude 的 start_offset**：沿 sketch 对应挤出轴偏移；XY 沿 Z、XZ 沿 Y、YZ 沿 X。
6. **Mirror 平面语义**：`plane="YZ"` + `offset=0` → 关于 X=0 平面镜像。
7. **Unite 前必须确认接触面积 > 0**：仅点/线相切（如底板顶边 Y=35 与
   R35 圆立板底端单点接触）时 Unite 不可靠——工具体可能不并入目标体，
   后续孔报"工具体完全在目标体外"。此类主体必须改单一 profile 建模
   （见 SKILL.md §3.0 Profile-First Rule）。
8. **边选择协议必须使用 Runner 冻结语法**：Linear 方向只写 `"X"/"Y"/"Z"`；
   多个板件角棱优先 `corners_xy + bbox_z/midpoint_z`；禁止方向向量、
   `midpoint_x/midpoint_y`、以及为相同条件四角创建脆弱的精确 midpoint group。
9. **完整圆边禁止写死 `Circular`**：当前 Loader 完整圆通常报告为
   `Elliptical`。完整圆边必须优先使用候选
   `["Elliptical","Circular"]`，再以 `length + midpoint_z + count` 限定。
   例如两个 Ø34 凸台顶圆边：`length≈106.81`、`midpoint_z≈36`、`count=2`。

## 4. 工具参数与规划条件分离（tool_args / selection_criteria / expectation）

每个 operation 必须区分三类内容，**禁止混淆**：

| 字段 | 内容 | 是否传给 NX_MCP |
|---|---|---|
| `tool_args` | 该 certified tool **真实支持的参数**（上表“参数”列）；如 `nx_list_edges` 只有 `body_id` | **是**（原样传递） |
| `selection_criteria` | Agent 在工具返回结果中自行筛选的几何/逻辑条件（curve_type / bbox / direction / length / midpoint / centroid / area / normal / expected_count 等） | **否** |
| `expectation` | 只用于判断结果（期望值、容差、数量） | **否** |

**禁止**：
- 把 `match` / `expected_count` / `purpose` / `checks` / `expect_extent` 等
  planner 自定义字段放入 `tool_args`；
- 因为 planner 自定义字段导致 MCP 参数错误；
- 把 `selection_criteria` / `expectation` 原样发送给工具。

典型写法（板件四角竖边，一次稳定选齐）：
```json
{
  "tool": "nx_list_edges",
  "tool_args": { "body_id": "body_main" },
  "selection_criteria": {
    "curve_type": "Linear",
    "direction": "Z",
    "corners_xy": [[-90.0,-60.0],[-90.0,60.0],[90.0,-60.0],[90.0,60.0]],
    "bbox_z": {"min": 0.0, "max": 14.0}
  },
  "expectation": { "count": 4 }
}
```
其中 `direction` 是枚举字符串，不是 `[0,0,1]`；只需要 Z 高度时使用
`midpoint_z`，不要虚构 `midpoint_x/midpoint_y`。四角目标条件相同、只有
XY 不同时优先 `corners_xy`，不要拆成四个精确 midpoint group。

典型写法（两个 Ø34 凸台顶外圆）：
```json
{
  "tool": "nx_list_edges",
  "tool_args": { "body_id": "body_main" },
  "selection_criteria": {
    "curve_type": ["Elliptical", "Circular"],
    "length": { "value": 106.81, "tol": 1.0 },
    "midpoint_z": { "value": 36.0, "tol": 0.5 }
  },
  "expectation": { "count": 2 }
}
```

非查询类工具一般只有 `tool_args`（如 `nx_extrude` 的 sketch_id/distance/start_offset）。
