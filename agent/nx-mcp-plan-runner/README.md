# nx-mcp-plan-runner

通用、零件无关、plan 无关的 NX_MCP 建模计划执行器。
任何符合 `nx-agent` 建模规划模块输出规范的 modeling plan，都可以直接交给
本 Runner 执行 —— 禁止再针对单个零件编写专用 Python 驱动。

## 设计原则

- **Plan 驱动**：Runner 只读取 plan JSON（`operations` / `tool` / `tool_args` /
  `selection_criteria` / `expectation` / `topology_changes`），不包含任何按
  step 编号、特征名或零件尺寸的特殊逻辑。`if step == 3:` 这类代码在本项目中
  不存在。
- **通用 symbol table**：`sketch_base` / `body_main` 等逻辑名在执行期绑定到
  真实 NX id。绑定关系只来自 plan 中的显式 `result_bindings`（可多值别名），
  绝不手写"每零件名称映射表"。
- **机器可执行引用**：`tool_args` 支持 `$body_main`、`$selection.sel_49`、
  `$selection.sel_56.boss_tops` 等引用；禁止自然语言占位符
  （`"<step49 匹配的 4 个 index>"`）存在于可执行计划中。
- **通用 selection engine**：`nx_list_edges` / `nx_list_faces` 返回后按
  `selection_criteria` 本地筛选；engine 只理解几何条件，不知道 R6 / R8 / C2
  是什么。
- **拓扑安全**：`topology_changes=true` 的操作成功后立即作废 edge/face 缓存，
  但绝不自动重新 list；只有后续 plan 显式调用 list 步骤时才查询。
- **错误策略**：单步失败 → Runner 停止 → 输出 `failed_step` + 原始错误。
  只有 plan 中显式声明的 `retry` 才会被重试；Runner 从不"思考式修复"。
- **冻结边界**：不修改 NX_MCP-Enhanced v2.1.1、不修改 C# Loader、不修改
  named pipe / resident Loader 架构、不新增 NX Tool、不安装大型依赖。

## 目录结构

```
nx-mcp-plan-runner/
├─ runner.py                       # 执行器 + builder + 静态检查 + CLI
├─ plan_schema.json                # plan 格式规范（frozen + executable 扩展）
├─ README.md
├─ tests/
│  └─ test_plan_resolution.py      # 单元测试（不触 NX）
└─ examples/
   ├─ modeling-plan-example.json       # 冻结计划原文（59 步，FAST）
   └─ modeling-plan-executable.json    # 由 build 生成 + 机器精确化数据修正
```

## 两种 plan 格式

| | 冻结格式（planner 输出） | 可执行格式（Runner 执行） |
|---|---|---|
| 逻辑名引用 | 裸名 `body_main` | `$body_main` |
| 结果绑定 | 隐式（由执行者推断） | 显式 `result_bindings` |
| selection 绑定 | 无 | 显式 `selection_binding` + `$selection.*` 引用 |
| 边/面 index 占位 | 自然语言 `"<step49 匹配的 4 个 index>"` | 机器引用 `"$selection.sel_49"` |
| 重试 | fallbacks 文字描述 | 显式 `retry` 对象 |

转换由 `build` 子命令完成（机械转换 + 推断绑定），规划规则本身不被修改。

## 用法

```text
python runner.py run   <executable-plan.json> [--workspace DIR] [--report out.json]
                       [--mode normal|benchmark] [--allow-overwrite] [--history FILE]
                       [--repair-attempt 0|1] [--repair-report attempt1.json]
python runner.py check <plan.json> [--frozen]
python runner.py build <frozen-plan.json> <out.json>
python runner.py test  #（等价：运行 tests/test_plan_resolution.py）
```

- `run`：静态校验 → Loader ping → **preflight 安全检查** → 顺序执行全部
  operation → 输出 JSON 报告（per-step 日志 + 汇总 + 分阶段计时）。
- `check`：不触 NX 的静态检查（工具合法性、参数合法性、引用可解析、占位符
  清零、selection criteria 语法）。
- `build`：冻结 → 可执行转换（无 NX）。

## Runtime Config

安装器会在 Runner 目录生成 `runtime-config.json`，包含 `python_exe`、
`workspace_root`、`nx_mcp_src`、`repo_root`。Runner 会用它补充 workspace /
import 路径；总控应使用其中的 `python_exe` 启动 Runner。

## Controlled Self-Healing 门禁

Runner 不自行修改 plan，但会机器校验第二次 repair：

- `--repair-attempt 1`
- `--repair-report <attempt1-report>`
- 必须同时使用 `--mode benchmark --allow-overwrite`
- previous report 必须是本零件第一次失败报告
- previous report 的 `repair_attempt` 必须为 0

## Preflight 安全检查（run 子命令）

Runner 只会自动关闭两类 part：

- **A**：与当前 plan 的目标输出文件路径完全一致的残留 part（`nx_create_part` /
  `nx_open_part` 的 `path` 解析后的绝对路径）；
- **B**：Runner 自己上一轮执行记录（`run_history.json`，`--history` 可指定）中
  明确创建的临时/测试 part。

其他情况：

- NX 当前打开的是**其他 part** → 不自动关闭、不丢弃未保存修改、不切换或覆盖，
  立即停止并返回：
  ```json
  { "status": "precheck_blocked", "reason": "unrelated_part_open",
    "active_part": "...", "planned_part": "..." }
  ```
- 当前打开的是同名目标 part 且**检测到未保存修改**（见下）→ 默认不得直接丢弃；
  仅当 `--mode benchmark` **且** part 路径与 planned_part 完全一致 **且** 显式
  传入 `--allow-overwrite` 时才允许关闭不保存。否则返回
  `"reason": "planned_part_dirty"` 并停止。

### 脏状态如何判定（诚实说明）

C# Loader 的 certified 工具**不暴露 IsModified / 脏状态标记**（`nx_status`
只返回活动 part 路径），且 `nx_close_part(save=false)` 会静默丢弃修改。
因此 Runner 采用**保守重建**策略：

- part 路径在 `run_history.json` 中且最近一次记录 `save_ok=true` → 视为 clean；
- 无记录（来源未知）或最近一次运行失败/中止（`save_ok=false`）→ 视为 dirty。

`run_history.json` 记录 Runner 每次对目标 part 的运行开始（save_ok=false）、
保存成功（save_ok=true）、失败（save_ok=false）。这意味着：一个从未被 Runner
记录过的同名 part 会被当作 dirty 处理——宁可阻止，也不丢弃未知工作。

### 运行模式

- `--mode normal`（默认）：禁止丢弃任何未保存 part（dirty → blocked）。
- `--mode benchmark`：仅对当前 plan 指定的测试件允许安全覆盖
  （仍需 `--allow-overwrite` 显式授权）。

## 分阶段计时（run 报告）

| 字段 | 定义 |
|---|---|
| `runner_start_to_first_nx_call` | Runner 启动（CLI 进入）→ 第一个 plan operation 派发（preflight/ping 归入 setup，不计入此值；与历史基准口径一致） |
| `nx_execution_elapsed` | 第一个 operation 派发 → 最后一个 operation 完成 |
| `final_validation_elapsed` | 最后一个拓扑变更操作之后的验证阶段（如 list_bodies/list_faces/list_edges/save/export）开始 → 结束 |
| `step_export_settle_elapsed` | `nx_export_step` 返回后等待 STEP 文件落盘且非空的耗时（轮询最多 60 s，失败则该步报错） |
| `total_runner_elapsed` | Runner 启动 → 报告生成完成 |

环境：Python 解释器使用 NX_MCP-Enhanced venv；`NX_MCP_ENHANCED_SRC` 环境变量
可显式指定 bridge 源码目录，缺省时按 `<workspace>/NX_MCP-Enhanced/src` 约定
布局自动发现。

## 可执行扩展（对 planner 输出 Schema 的最小向后兼容扩展）

仅扩展输出格式，不改变建模规划规则：

```json
{
  "tool": "nx_extrude",
  "tool_args": {"sketch_id": "$sketch_flange", "distance": 8},
  "result_bindings": {"body": "body_flange"}
}
```

- `result_bindings`: `{响应字段: 逻辑名}`。响应字段为适配后响应中的键
  （`object` / `body` / `sketch` / `objects` / `part` / `path` 等）；
  值为字符串（单个名）或数组（多值别名，如 unite 后把主实体重绑定到别名）。
  多值数组语义：响应为列表时按序一一绑定（pattern 副本），响应为单值时全部
  绑定同一 id（别名）。
- `selection_binding`: list 步骤本地筛选结果的符号名。扁平 criteria →
  `[indices]`；命名分组 → `{group: [indices]}`。
- `retry`: `{"max": 1, "if_error_contains": ["子串"]}` —— 唯一允许的重试来源。
  build 过程会把 loader 已知瞬态错误（`nx_save_part` 的"撤消标记"）写为声明式
  retry；planner 也可自行声明。
- 引用语法：`$name`、`$selection.name`、`$selection.name.group`。
  单元素 selection 列表用在标量参数（如 `remove_face_index`）时自动解包。

## 通用 adapter（工具级，非零件级）

- **矩形**：plan 中的 `corner1` / `corner2`（角点语义）自动转换为 bridge 槽位
  `corner1={center}` / `corner2={width,height}`（C# Loader 语法 `cx cy w h`）。
  任何使用 corner1/corner2 的步骤都走同一 adapter，不限定 step。
- **圆边**：完整圆在 loader 中报告为 `Elliptical`（圆弧才是 `Circular`），
  选 C2 类圆边时 criteria 应写 `"curve_type": ["Circular", "Elliptical"]`。
- **edge done 计数**：`nx_edge_blend` / `nx_chamfer` 的 `done=N` 在 bridge 适配
  后被丢弃；Runner 对这些工具走 raw named-pipe 通道，并在 expectation 中支持
  `"done": N` 判定。

## selection_criteria 语法（概要）

edge：`curve_type` / `direction` / `length` / `midpoint` / `midpoint_z` /
`bbox` / `bbox_x|y|z` / `corners_xy` / `adjacent_faces` / `linear_only`
face：`face_type` / `centroid` / `centroid_z` / `centroid_radius` / `normal` / `area`

数值取值：精确值（容差内）、`{"value": n, "tol": t}`、`{"min","max"}` 区间、
`[lo, hi]` 区间、候选数组（list-any）、`{"any": [...]}`。命名分组模式：criteria
全部为"组名 → 条件对象"时按组独立筛选。信息键（`note` / `use` / 含
`auxiliary` / `*_approx`）不影响匹配。详见 `plan_schema.json`。

## 执行报告

per-step：`step / tool / started_at / elapsed_seconds / status / retry_count /
note(或 error)`；汇总：`status / elapsed_seconds / runner_start_to_first_nx_call /
nx_execution_elapsed / final_validation_elapsed / step_export_settle_elapsed /
total_runner_elapsed / operations_total / operations_completed / failed_step /
retries / body_count / model_bbox / linear_edge_bbox / selections / prt_path / step_path`。
- `model_bbox`：跨全部 list 步骤合并的**完整模型极值**（线性边 bbox 与面
  centroid 的并集；如 V2 的 Z=0..56 由法兰顶面 centroid 提供）。
- `linear_edge_bbox`：仅由线性边 bbox 得到的极值（曲线边 bbox 为空，故
  Z 顶面会被截断，如 0..26）。两者语义不同，禁止将后者当作真实包围盒。
`selections` 为各 `selection_binding` 的本地筛选结果（扁平 `[indices]` 或
`{group: [indices]}`），供最终验证按计划语义（如 R6/R8/C2）核对。

## examples 说明

- `modeling-plan-example.json`：冻结计划原文（V2-TEXT-CHALLENGE，59 步，FAST），
  未改动。
- `modeling-plan-executable.json`：由 `build` 生成，随后对 selection criteria
  做了**数据层机器精确化**（生成器保持通用，不承载零件数据）：
  - step 49（R6）与 step 51（R8）：`bbox_x/bbox_y` 范围改为精确 `corners_xy`
    角点候选（`[-90,±60]` / `[±115,±22]`）+ `bbox_z` 0..12 + 长度容差；
  - step 53（C2）：curve_type 接受 `Circular|Elliptical`、周长/圆心 z 带容差；
  - step 56：`centroid_radius` 显式容差；expectation 键名与 criteria 分组名
    对齐（`boss_tops_count` / `countersink_cones_count` /
    `holes_reasonable_count_range`）。
  冻结计划不因上述修正而改动。

## 测试

```text
python tests/test_plan_resolution.py    # 36 项，全绿，不触 NX
```

覆盖：symbol table / result binding（含多值与长度失配报错）/ variable
resolution / rectangle adapter / edge selection（角点候选、圆边、z、线性、
bbox 包含、邻接面）/ face selection（扁平、命名分组、半径）/ topology
invalidation（且不自动 list）/ expectation checker（含 done）/ 冻结计划加载 /
build + check 全引用可解析 / build 幂等 / 非法参数拦截 / **preflight 策略**
（无关 part 阻塞、planned clean 放行、dirty+benchmark+overwrite 放行、
dirty+normal 阻塞、无活动 part 放行、Runner 自有测试件放行、路径归一化）/
**run_history 与 runtime_dirty 判定**。

## 验收口径

- 本 Runner 源码不得出现：`step == N` 分支、R6/R8/C2 坐标、
  V2-TEXT-CHALLENGE 或任何零件尺寸字面量（已程序化扫描确认）。
- 本阶段不执行 NX、不运行 benchmark、不修改任何冻结对象。
