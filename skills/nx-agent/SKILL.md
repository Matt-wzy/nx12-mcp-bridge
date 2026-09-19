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

1. 读取 `references/drawing-reader.md` + `references/nx-drawing-rules.md`。
2. 输出结构化 JSON，并通过门禁 A。
3. 读取建模规划与拓扑规则，生成 frozen plan，并通过门禁 B。
4. 调用 Plan Runner 执行。
5. 总控规则见 `references/pipeline-contract.md`。
6. 用户输出规范见 `references/chinese-output.md`。

两条链路：

```text
文字描述 → 建模规划 → Plan Runner → Siemens NX → PRT + STEP

二维工程图 → 工程图读取 → 建模规划 → Plan Runner → Siemens NX → PRT + STEP
```

## 2. 运行时与路径

安装器会在 Plan Runner 目录生成 `runtime-config.json`。执行 Runner 前优先读取其中：

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
- `blocking_unresolved = 0`，即 `unresolved` 中没有 `required_for_modeling=true` 的项目；
- `dimension_conflicts = 0`；
- `dimension_closure.status = "closed"`；
- 存在 `overall_dimensions`、`coordinate_system`、`features`。

其中：
- 能由图中**明确尺寸 + 明确拓扑关系**唯一计算出的值进入 `derived`，不进入 `unresolved`；
- `required_for_modeling=false` 的粗糙度、普通工艺说明、非建模表格字段、无关 OCR 模糊项进入 `warnings` / soft unresolved，**不得阻塞 Gate A**；
- 只有会改变最终三维实体的尺寸、位置、数量、方向、轮廓、贯穿/深度等关键项无法唯一确定时才 BLOCKED。

Gate A 失败时只向用户询问**真正 blocking 的最少问题**，不得把 non-blocking warning 一并当成澄清问题。

### 门禁 B
- runner build/check 通过
- unresolved reference = 0
- illegal tool_args = 0
- natural language placeholder = 0
- executable plan 存在且非空

frozen plan 首次落盘前必须完成静态自检。B 阶段 build/check 失败直接结束，禁止现场补丁后继续。

## 5. 阶段 C 与受控自动修复

- 单次 Runner 尝试严格 fail-fast：任意 modeling step 失败，当前尝试立即停止。
- 每个任务最多允许 1 次 Controlled Self-Healing。
- 只允许修复根因明确、且不改变尺寸/位置/特征数量/几何语义的计划级问题。
- 典型允许：edge/face `selection_criteria` 过严、Loader 已冻结的类型语义差异。
- 禁止：猜尺寸、改图纸、改变主体结构、绕过能力边界、修改 Runner/NX_MCP/Loader。
- 禁止把任何图纸/derived/frozen 几何数值做 nudge/epsilon 改写来“救”执行，例如 `Z=50 → Z=49`；Boolean 相切失败不能通过改坐标、深度、孔位、直径等设计数值自动修复。
- 修复后必须重新 build/check，并由 Runner 安全 preflight 丢弃本任务自己的失败零件，然后从第 1 步完整重跑。
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
