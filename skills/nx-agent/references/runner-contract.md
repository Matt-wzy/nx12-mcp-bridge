# Runner Contract（Planner → nx-mcp-plan-runner 冻结接口契约）

> 本契约一次性取自 `nx-mcp-plan-runner`（2026-09-18 冻结）。
> **正常 B 阶段生成 plan 时禁止读取 runner.py / README / plan_schema.json / certified.py / NX_MCP 源码**，一律以本文件 + certified-tool-contract.json 为准。
> 仅当 schema 版本变化、build/check 返回 schema incompatibility、contract 版本变化、或用户明确要求重新校验接口时，才允许重新检查外部文件。

## 1. Executable plan Schema 版本

- 当前 `plan_schema.json` 的 `schema_version` = **1.1**（冻结于 2026-09-18）。
- 两种格式：
  - **frozen**（Planner 输出）：顶层含 `skill / mode / part / coordinate_system / notes / operations / final_validation / fallbacks`；`mode` 必填且只允许 `FAST | DIAGNOSTIC`；`operations` 必填。
  - **executable**（Runner 执行格式）= frozen + 扩展字段（`result_bindings`、`selection_binding`、`retry`、`$references`）。
- Runner 可读/校验两种格式；只有 executable 可被 `run` 执行。

## 2. Operation 允许字段

每个 operation 只允许以下字段（frozen 中可省略可选项）：

| 字段 | 必填 | 说明 |
|---|---|---|
| `step` | 是 | int，全局递增 |
| `goal` | 否 | 人类可读目标 |
| `tool` | 是 | 必须是 32 个 certified tool 之一（见 certified-tool-contract.json） |
| `target` | 否 | 逻辑名/说明 |
| `tool_args` | 是 | 传给 NX_MCP 的真实参数；值可为逻辑名或 `<stepN ...>` 占位符 |
| `selection_criteria` | 否 | 本地筛选规则；**绝不发给 NX_MCP** |
| `expectation` | 否 | 判定规则；**绝不发给 NX_MCP** |
| `topology_changes` | 是 | bool；是否改变实体拓扑 |
| `refresh_edges_after` | 否 | bool，仅信息：旧 edge index 失效（Runner 不会自动重列） |
| `refresh_faces_after` | 否 | bool，仅信息：旧 face index 失效 |
| `result_bindings` | 否 | **executable 扩展**；build 自动生成，Planner 不写 |
| `selection_binding` | 否 | **executable 扩展**；build 自动生成，Planner 不写 |
| `retry` | 否 | **executable 扩展**；仅 build 声明的安全重试（save） |

## 3. 引用语法

| 语法 | 含义 |
|---|---|
| `$name` | 逻辑名引用（如 `$body_main`、`$sketch_base`），由 `result_bindings` 注册 |
| `$selection.name` | 引用名为 `name` 的 selection 的**全部命中项**（list_edges/list_faces 步骤的 `selection_binding`） |
| `$selection.name.group` | 引用该 selection 中命名分组 `group` 的命中项（group criteria 专用） |

- Planner 在 frozen 里写**裸逻辑名**（`body_main`、`sketch_base`）和**占位符**（`<stepN 匹配的 ...>`）；build 统一重写为 `$` 引用。
- 占位符正则：`<step(\d+)[^>]*>` → `$selection.sel_<step数字>`。
- 单元素 selection 用于标量参数（如 `remove_face_index`）时自动解包为标量；`edge_indices` / `tool_body_ids` 始终保持列表（LIST_PARAMS）。

## 4. tool_args 规则

1. `tool_args` 只能包含该 certified tool 真正支持的参数（required + optional，见 certified-tool-contract.json）。
2. `selection_criteria` / `expectation` 及其任何内部字段（`match` / `expected_count` / `purpose` / `checks` / `expect_extent` 等）**禁止**放入 `tool_args`。
3. 禁止把 planner 自定义字段原样传给 NX_MCP —— 参数错误会直接导致 MCP 调用失败。
4. 值形式：数值、`{x,y}` 对象、逻辑名字符串、`<stepN ...>` 占位符字符串。

## 5. result_bindings 语义（build 自动推断）

- 作用：把"产出者"（producer op）映射到其首个消费者的逻辑名，使 `$name` 可解析。
- 产出者注册规则：
  - `nx_create_sketch` → 注册未绑定 sketch；字段 `object`。
  - `nx_extrude(operation=create)` → 注册新 body；字段 `body`。
  - `nx_mirror` / `nx_unite` → 注册新 body；字段 `object`（桥接适配响应字段）。
  - `nx_linear_pattern` / `nx_circular_pattern` → 注册 count-1 个副本；字段 `objects`（按序绑定副本）。
  - `nx_extrude(operation=subtract)` 不注册产出者。
- 绑定规则：
  - sketch 引用：从最近的未绑定 sketch 产出者取。
  - body 引用：unite 的 `target_body_id` 取**最早**未绑定 body；其余 body 引用取**最近**未绑定 body。
  - unite 的 `tool_body_ids` 消费对应产出者（consumed）。
- Planner 写 frozen 时**不需要**手写 result_bindings。

## 6. selection_binding 语义（build 自动生成）

- 触发条件：operation 含 `selection_criteria` 且 `tool` 是 `nx_list_edges` / `nx_list_faces`。
- 命名：`sel_<step>`（如第 28 步 → `sel_28`）。
- kind：criteria 为 group 形式时 kind=`groups`（引用 `$selection.sel_N.group`）；否则 kind=`indices`。
- 消费步骤（如 edge_blend / chamfer）的 `edge_indices` 用占位符 `<stepN ...>` 引用对应 list 步骤。

## 7. rectangle adapter

`nx_sketch_rectangle` 的 `corner1` / `corner2` 在 Runner 侧自动适配为 loader 语法：
- `corner1` = 中心点 `{x:(c1.x+c2.x)/2, y:(c1.y+c2.y)/2}`
- `corner2` = 宽高 `{x:c2.x-c1.x, y:c2.y-c1.y}`

Planner 直接写对角点 `{x,y}` 即可，无需自行换算。

## 8. Topology invalidation 规则

以下操作完成后，之前获得的 edge index / face index **一律立即失效**（见 topology-safety.md 完整清单）：
`Unite / Subtract / Hole / Counterbore / Countersink / Shell / Pattern / Mirror / Edge Blend / Chamfer` 及任何改变实体拓扑的操作。

- 每个边/面操作前必须重新 `nx_list_edges` / `nx_list_faces` 并按**几何条件**筛选，禁止猜 index。
- face 选择的 Planner 稳定策略：唯一顶/底 Planar 面优先 `face_type + centroid_z + count`；`normal` 与 `area` 只作辅助。只有同一 Z 高度存在多个 Planar 面时，再增加完整 `centroid` 或 `area`。
- 连续多个边操作必须：list → 定位 → 执行 → 再 list → 定位 → 执行，禁止一次 list 保存多组 index 连续使用。

## 9. build / check 输入输出约定

```
python runner.py build <frozen_plan.json> <out_executable.json>
# 输出: {"built": path, "operations": N, "check_errors": [...], "ok": bool}
# 机械转换 + 绑定推断 + 对产物执行 executable check；exit 0/1

python runner.py check <plan.json> [--frozen]
# 输出: {"plan": path, "frozen": bool, "operations": N, "errors": [...], "ok": bool}
# frozen 模式跳过自然语言占位符检查（frozen 允许占位符）
```

check（executable）检查项：
1. tool ∈ 32 certified tools；
2. tool_args 是对象；
3. selection_criteria 无未知键（edge: curve_type/direction/length/midpoint/midpoint_z/bbox/bbox_x/bbox_y/bbox_z/corners_xy/adjacent_faces/linear_only；face: face_type/centroid/centroid_z/centroid_radius/normal/area；group 模式递归检查；info 键 note/use/*auxiliary* 与 *_approx 忽略）；
4. 无自然语言占位符（`<...>` 或含"匹配的"）—— executable 模式；
5. `$` 引用可解析（selection 必须先于消费步骤注册；group 引用必须存在）；
6. 参数校验（rectangle 先适配；required 齐全；无非法参数）。

**Loader Face Semantics（冻结实测，2026-09-18）**：布尔切孔侧面
（nx_hole / nx_counterbore_hole / nx_countersink_hole 生成）在
`nx_list_faces` 中通常报告为 **`Swept`**（不是 `Cylindrical`）；
`Cylindrical` 也可能出现在 Edge Blend 等曲面，不能作为"孔侧面"固定类型。
验证孔时 `face_type` 用候选 `["Swept", "Cylindrical"]`，判据以
`centroid_radius` + `centroid_z` + `count` 为主；centroid_z 必须按该位置
**最终实际材料 Z 区间**中点推算（counterbore 截断、hole depth 超出材料
高度时以实际区间为准）。完整契约见 topology-safety.md §4；Runner 代码禁止
硬编码，Planner 从每个零件的最终几何关系推导。

selection_criteria 值语法：精确值（默认容差 0.5，可 `{"value":n,"tol":t}`）、`{"min","max"}` 范围、`[lo,hi]`、候选数组、`{"any":[...]}`。
face 额外冻结语义：对唯一顶/底 Planar 面，Planner 优先 `face_type:"Planar" + centroid_z + expectation.count`；`normal` 只作辅助，不作为首要硬筛选条件；`area` 只辅助。
edge 额外冻结语法：`direction` 必须是 `"X"|"Y"|"Z"|"OTHER"` 字符串；
`midpoint` 是完整 `[x,y,z]`；只筛高度用 `midpoint_z`；
`corners_xy` 是 `[[x1,y1],...]`，用于一次匹配多个指定 XY 位置的 Linear 边。
禁止 `direction:[0,0,1]`、`midpoint_x`、`midpoint_y`。
对于四角竖边等“条件相同、仅 XY 不同”的目标，优先 flat criteria +
`corners_xy` + `expectation.count`，不要拆成 group。
**完整圆边语义**：当前 Loader 的 `nx_list_edges` 实测完整圆通常报告为
`"Elliptical"`。Planner 不得把完整圆边写死为 `"Circular"`；统一优先使用
候选数组 `["Elliptical","Circular"]`，并结合 `length + midpoint_z + count`。
当 curve_type 为 Circular / Elliptical / Conical（或候选仅含这些曲线类型）时，
禁止同时生成 `bbox / bbox_x / bbox_y / bbox_z / corners_xy`。
group 模式：criteria 所有 value 均为条件对象时按命名组独立筛选；
每个 group value 本身就是 criteria 对象，禁止外层再加 `groups` 键。

expectation 语法：`count` / `count_range` / `body_count`（nx_list_bodies）/ `<group>_count` / `<group>_count_range` / `x_min..z_max`（selection extents，Linear 边贡献 X/Y、face centroid 贡献 Z；`tolerance_mm` 覆盖默认 0.5）/ `done`（edge_blend / chamfer 走 raw 通道解析 done=N）；`purpose` / `note` / `*auxiliary*` / `*_approx` 只记录不判失败。

## 10. 当前路径与版本

- Runner 路径：`%USERPROFILE%\NX_MCP_WORKSPACE\nx-mcp-plan-runner\runner.py`
- plan_schema：`%USERPROFILE%\NX_MCP_WORKSPACE\nx-mcp-plan-runner\plan_schema.json`（schema_version 1.1）
- certified 参数来源：runner.py TOOL_PARAMS（冻结镜像于 certified-tool-contract.json v1.0）
- 本契约版本：`runner_contract_version = 1.0`；`certified_tool_contract_version = 1.0`
