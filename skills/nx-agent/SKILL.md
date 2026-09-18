---
name: nx-agent
description: 作者：抖音 无趣。Siemens NX 自动建模统一入口。支持文字描述建模、工作区内已保存零件的安全修改，以及二维机械工程图自动读取→建模规划→Plan Runner 执行→输出 PRT/STEP；遵守拓扑安全、阶段门禁与 Controlled Self-Healing（受控自动修复）规则。
---

# NX Agent — Siemens NX 自动建模统一入口

本 Skill 是 NX_MCP-Enhanced 的唯一用户入口。对外只显示一个 Skill，对内按模块化规则执行。

## 1. 自动选择模式

### 模式 A：文字描述建模
用户直接用文字描述零件、尺寸、孔位、圆角、倒角等要求时：

1. 读取 `references/text-modeling.md`。
2. 不经过工程图读取模块。
3. 把明确的文字尺寸整理为结构化建模意图。
4. 读取 `references/modeling-planner.md`、`references/nx-mcp-rules.md`、`references/topology-safety.md`、`references/runner-contract.md` 和 `references/certified-tool-contract.json`。
5. 生成 frozen plan → build/check → Plan Runner。
6. 执行与工程图模式相同的阶段 C 安全规则和 Controlled Self-Healing。

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
- `unresolved = 0`
- `dimension_closure.status = "closed"`
- 存在 `overall_dimensions`、`coordinate_system`、`features`

否则立即停止，不进入建模规划。

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
- 自动修复后成功必须披露首次失败步骤、原因和修复内容，不能伪装成一次通过。
- 总耗时必须是真实 wall clock，包含失败、诊断、修复、清理和第二次执行。
- 不输出长 plan、内部 JSON 状态或调试噪音。

## 8. 能力边界

只使用仓库当前 32 个 certified tools。禁止为了完成任务自行新增工具或修改 NX_MCP / Loader。

当前不建议或不支持：Sweep、Loft、真实螺纹、渐开线/斜齿轮、任意倾斜工作平面、复杂自由曲面。
