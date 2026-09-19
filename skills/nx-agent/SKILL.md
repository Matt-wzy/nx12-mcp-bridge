---
name: nx-agent
description: 作者：抖音 无趣。Siemens NX 自动建模统一入口。支持文字描述建模、工作区内已保存零件的安全修改，以及二维机械工程图自动读取→建模规划→Plan Runner 执行→输出 PRT/STEP；遵守拓扑安全、阶段门禁与 Controlled Self-Healing（受控自动修复）规则。
---

# NX Agent — Siemens NX 自动建模统一入口

本 Skill 是 NX_MCP-Enhanced 的唯一用户入口。对外只显示一个 Skill，对内按模块化规则执行。

## 1. 自动选择模式

### 模式 A：文字描述建模（Fast Path）
用户直接用文字描述零件、尺寸、孔位、圆角、倒角等要求时：

1. **正常路径只读取** `references/text-modeling.md` 与
   `references/certified-tool-contract.json`；本 `SKILL.md` 已包含阶段 C、
   拓扑和输出硬规则。禁止为了“确认规则”重复读取其它 reference。
2. 不经过工程图读取模块；把明确文字尺寸直接整理为结构化建模意图。
3. 按 `text-modeling.md` 的固定 runtime-config 路径一次定位 Runner；
   **禁止扫描 Skill 目录、仓库目录、安装脚本、Python 环境或聊天目录**。
4. 正常新零件任务禁止主动读取 `run_history.json`、旧 frozen/executable plan、
   旧 report 或旧 PRT/STEP 内容。历史安全判断由 Runner preflight 自己完成。
5. 直接生成 frozen plan → build/check → Plan Runner。
6. 只有以下触发条件才读取详细 reference：
   - build/check 明确报告 schema/contract incompatibility → `runner-contract.md`；
   - attempt 1 失败且符合 Controlled Self-Healing → `pipeline-contract.md` +
     与失败类型直接相关的 `topology-safety.md`；
   - 用户明确要求解释底层 Planner 规则 → 再读取对应详细文档。
   正常一次通过路径不得预读这些文件。

默认用于创建新零件。修改已有零件时，仅允许打开 `NX_MCP_WORKSPACE` 内、用户明确指定路径的已保存零件；不得自动接管无关或未保存的当前零件。

### 模式 B：二维机械工程图自动建模
用户上传二维机械工程图并要求“开始建模”“按图建模”“用 NX 画出来”等时：

1. 读取 `references/drawing-reader.md` + `references/nx-drawing-rules.md`。正常路径**不读取 examples**；只有 validator 报 schema 错误时才查看 `examples/example-output.json`。
2. 输出结构化 drawing JSON 后，按“运行时与路径”的固定规则只解析一次 `runtime-config.json`，禁止扫描目录；用其中 `python_exe` 执行同目录 `runner.py validate-drawing <drawing.json>`。该命令会在同一次调用内执行严格 schema-only normalization；normalizer 失败或 Gate A 失败时，禁止 Agent 手工补尺寸、方向、位置、数量、证据或删除 blocking unresolved 后重试。
3. 只有 validate-drawing exit code=0，且返回 `source_ownership.status="pass"` 与 `coordinate_sanity.status="pass"`，才通过门禁 A。
4. 读取建模规划与拓扑规则，生成 frozen plan，并使用 `runner.py build <frozen> <executable> --drawing <drawing.json>` 通过门禁 B；Mode B 禁止省略 `--drawing`。
5. 调用 Plan Runner 执行。
6. 总控规则见 `references/pipeline-contract.md`；用户输出规范见 `references/chinese-output.md`。

两条链路：

```text
文字描述 → 建模规划 → Plan Runner → Siemens NX → PRT + STEP

二维工程图 → 工程图读取 → 建模规划 → Plan Runner → Siemens NX → PRT + STEP
```

## 2. 运行时与路径

安装器会在 Plan Runner 目录生成 `runtime-config.json`。路径只允许按以下顺序解析一次：

1. 若环境变量 `NX_MCP_WORKSPACE` 存在：使用其值下的 `nx-mcp-plan-runner/runtime-config.json`；
2. 否则使用用户主目录下 `NX_MCP_WORKSPACE/nx-mcp-plan-runner/runtime-config.json`。

禁止递归搜索 Runner、Python、仓库或 Skill 目录。读取成功后使用其中：

- `python_exe`
- `workspace_root`
- `nx_mcp_src`

所有 PRT / STEP / plan / report 都必须落在 `workspace_root` 内。禁止把聊天目录、临时对话目录或其它任意绝对路径当作输出工作区。

## 3. 模式选择优先级

1. 有工程图且用户要求依据图纸建模 → 模式 B。
2. 没有工程图、用户直接给明确几何要求 → 模式 A。
3. 工程图 + 补充文字同时存在：工程图为几何主来源；补充文字仅作为明确附加约束。发生冲突必须停止并报告。
4. 禁止把工程图任务退化成“看图后直接手工调用 NX_MCP”。
5. 禁止让纯文字建模任务无意义地走工程图读取。

## 4. 工程图门禁

### 门禁 A
Gate A 判断的是**最低充分建模闭合**，不是“整张图所有文字/工艺信息都必须 100% 解释”。

同时满足以下条件才通过：
- 本地 `runner.py validate-drawing <drawing.json>` exit code=0；
- validator 返回 `source_ownership.status="pass"`；
- validator 返回 `coordinate_sanity.status="pass"`；
- `blocking_unresolved = 0`、`dimension_conflicts = 0`、`dimension_closure.status = "closed"`。

其中：
- 能由图中**明确尺寸 + 明确拓扑关系**唯一计算出的值进入 `derived`，不进入 `unresolved`；
- `required_for_modeling=false` 的粗糙度、普通工艺说明、非建模表格字段、无关 OCR 模糊项进入 `warnings` / soft unresolved，**不得阻塞 Gate A**；
- 只有会改变最终三维实体的尺寸、位置、数量、方向、轮廓、贯穿/深度等关键项无法唯一确定时才 BLOCKED。
- required feature 的顶层 `type` 也属于 HARD 几何语义，必须由 `feature_kind` source 绑定，不能作为 metadata 跳过。

Gate A 失败时只向用户询问**真正 blocking 的最少问题**，不得把 non-blocking warning 一并当成澄清问题。

### 门禁 B
- capability_violations = 0；required feature 超出 32 个 certified tools 时，优先使用 drawing 明确提供的 surrogate；否则仅允许使用本地机器维护、可唯一映射的 deterministic surrogate recipe；两者都不存在时 B 阶段失败
- feature source/count preservation 通过：Reader 已闭合的 HARD 字段、derived.target、pattern 总 count 不得被 Planner 改写
- runner build/check 通过
- unresolved reference = 0
- illegal tool_args = 0
- natural language placeholder = 0
- executable plan 存在且非空

frozen plan 首次落盘前必须完成静态自检。B 阶段 build/check 失败直接结束，禁止现场补丁后继续。
required threaded feature 优先使用输入明确提供并批准交付的 `surrogate_geometry`。没有输入 surrogate 时，`validate-drawing` 返回本地确定性 thread surrogate recipe，Planner 必须原样携带 recipe 与哈希 provenance，Gate B 重新计算并核对后才允许执行；Planner 禁止自行推导底孔直径。recipe 只能补 thread representation/diameter，不能补 axis、center、count、depth/range、side 或 ownership。未知 thread spec 没有 recipe 时返回 capability violation。使用 surrogate 的最终报告必须列入 `approximations`，不得描述为真实螺纹牙型。

## 5. 阶段 C 与受控自动修复

- 单次 Runner 尝试严格 fail-fast：任意 modeling step 失败，当前尝试立即停止。
- 每个任务最多允许 1 次 Controlled Self-Healing。
- 只允许修复根因明确、且不改变尺寸/位置/特征数量/几何语义的计划级问题。
- 典型允许：edge/face `selection_criteria` 过严、Loader 已冻结的类型语义差异。
- 禁止：猜尺寸、改图纸、改变主体结构、绕过能力边界、修改 Runner/NX_MCP/Loader。
- 禁止把任何图纸/derived/frozen 几何数值做 nudge/epsilon 改写来“救”执行，例如 `Z=50 → Z=49`；Boolean 相切失败不能通过改坐标、深度、孔位、直径等设计数值自动修复。
- 修复后必须重新 build/check，并由 Runner 安全 preflight 丢弃本任务自己的失败零件，然后从第 1 步完整重跑。
- attempt 1 失败后，禁止改 PRT/STEP 文件名、复制 drawing 或改成新的 normal run 来绕过 repair lineage；下一次执行只能携带原失败 report 的合法 repair attempt。
- repair 前后的 drawing semantic projection 与 plan geometry projection 必须一致；尺寸、轴、中心、数量、深度/范围、贯穿范围和 thread interpretation 的任何变化都会被机器拒绝。
- 禁止从失败步骤续跑。
- 第二次失败必须结束。

## 6. 拓扑与选择规则

- edge / face index 都是临时数据；任何拓扑变化后旧 index 立即失效。
- Linear 边 `direction` 只能是 `"X"|"Y"|"Z"|"OTHER"`。
- 四角竖边优先 `corners_xy + bbox_z/midpoint_z`。
- Circular / Elliptical / Conical 曲线边禁止 bbox 类条件。
- 完整圆边优先 `curve_type:["Elliptical","Circular"] + length + midpoint_z + expectation.count`。
- 唯一顶/底 Planar 面优先 `face_type:"Planar" + centroid_z + expectation.count`。
- `normal` 与 `area` 只作辅助，不作为唯一顶/底面的首要硬筛选条件。
- 连续多个圆角/倒角必须每次重新 `nx_list_edges`。
- 禁止只按 index 数字猜边/面。

## 7. 输出

- 用户可见回复全部使用自然中文。
- 阶段 C 状态必须与 Runner report 完全一致：只有 `report.status="success"` 才能写“成功”；若 `report.status="failed"` 或存在 `failed_step`，即使 PRT/STEP 已生成也必须写“失败”。
- 自动修复后成功必须披露首次失败步骤、原因和修复内容，不能伪装成一次通过。
- 总耗时必须是真实 wall clock，包含失败、诊断、修复、清理和第二次执行。
- 不输出长 plan、内部 JSON 状态或调试噪音。

## 8. 能力边界

只使用仓库当前 32 个 certified tools。禁止为了完成任务自行新增工具或修改 NX_MCP / Loader。

当前不建议或不支持：Sweep、Loft、真实螺纹、渐开线/斜齿轮、任意倾斜工作平面、复杂自由曲面。
能力边界不能靠改变几何语义绕过：例如真实螺纹不支持时，禁止把 threaded hole 删除或改成普通通孔/间隙孔。只有输入明确给出允许使用的几何替代体及其实际尺寸时才可建模该替代体；否则在 B 阶段 fail-closed。
