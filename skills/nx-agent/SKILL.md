---
name: nx-agent
description: 作者：抖音 无趣。Siemens NX 自动建模统一入口。支持文字描述直接建模、修改当前 NX 零件，以及二维机械工程图自动读取→建模规划→Plan Runner 执行→输出 PRT/STEP。根据输入自动选择模式，并遵守冻结的 NX_MCP、拓扑安全、门禁与 fail-fast 规则。
---

# NX Agent — Siemens NX 自动建模统一入口

本 Skill 是 NX_MCP-Enhanced 的唯一用户入口。对外只显示一个 Skill，对内仍保留原有模块化规则。

## 1. 自动选择模式

### 模式 A：文字描述建模 / 修改已有零件
当用户直接描述要创建的零件、特征、尺寸，或要求修改当前已打开的 NX 零件时：
- 读取并遵循 `references/text-modeling.md`
- 直接使用已认证 NX_MCP tools
- 不经过工程图解析 Pipeline

### 模式 B：二维机械工程图自动建模
当用户上传二维机械工程图并要求“开始建模”“按图建模”“用 NX 画出来”等时：
- 读取 `references/drawing-reader.md` + `references/nx-drawing-rules.md`
- 生成结构化 JSON
- 通过门禁 A 后读取 `references/modeling-planner.md`
- 同时遵循 `references/nx-mcp-rules.md`、`references/topology-safety.md`、`references/runner-contract.md`、`references/certified-tool-contract.json`
- 通过门禁 B 后调用 Plan Runner
- 总控规则见 `references/pipeline-contract.md`
- 用户输出规范见 `references/chinese-output.md`

固定链路：
```text
工程图 → 工程图读取 → 建模规划 → Plan Runner → Siemens NX → PRT + STEP
```

## 2. 模式选择优先级
1. 有工程图且用户要求依据图纸建模 → 模式 B。
2. 没有工程图、用户直接给几何/修改要求 → 模式 A。
3. 工程图 + 补充文字同时存在：工程图为几何主来源，补充文字仅作为明确附加约束；发生冲突必须停止并报告。
4. 禁止把工程图任务退化成 Agent 自己逐步直接调用 NX_MCP。
5. 禁止让纯文字建模任务无意义地走工程图解析 Pipeline。

## 3. 工程图模式门禁
### 门禁 A
必须同时满足：
- `unresolved = 0`
- `dimension_closure.status = "closed"`
- 存在 `overall_dimensions`、`coordinate_system`、`features`

否则立即停止，不进入 B。

### 门禁 B
必须同时满足：
- runner build/check 通过
- unresolved reference = 0
- illegal tool_args = 0
- natural language placeholder = 0
- executable plan 存在且非空

B 阶段必须在 frozen plan 首次落盘前完成静态自检。禁止先生成错误 plan，再修改 JSON 补丁后继续。

### 阶段 C
- 先做 preflight
- resident C# Loader 只以 named pipe `nx_mcp_loader` ready 为准
- 禁止使用 legacy `bridge.json` 判断 Loader 是否加载
- Runner 正式建模后任意 modeling step 失败 → 整个任务失败
- 失败后禁止自动 repair、修改 plan 续跑、手动补建、手动 save、手动 STEP 导出

## 4. 拓扑与边选择
- 所有 edge / face index 都是临时数据；拓扑改变后旧 index 立即失效
- Linear 边 `direction` 只能是 `"X"|"Y"|"Z"|"OTHER"`
- 四角竖边优先 `corners_xy + bbox_z/midpoint_z`
- Circular / Elliptical / Conical 曲线边禁止 bbox 类条件
- 曲线边优先 `curve_type + length + midpoint_z + expectation.count`
- 连续多个圆角/倒角必须每次重新 `nx_list_edges`
- 禁止只按 index 数字猜边/面

完整细则以 `references/topology-safety.md` 和 `references/nx-mcp-rules.md` 为准。

## 5. 输出
- 用户可见回复使用自然中文
- 工程图模式成功时输出：图纸解析耗时、建模规划耗时、NX 建模耗时、总耗时、实体数量、模型尺寸、PRT 路径、STEP 路径
- 失败只报告失败阶段、失败步骤和失败原因
- 不输出内部 JSON 键、长 plan、思考过程或调试噪音

## 6. 能力边界
只使用仓库已认证 NX_MCP 能力。禁止为了完成任务自行新增工具或修改 NX_MCP / Loader。

当前不建议或不支持：Sweep、Loft、真实螺纹、渐开线/斜齿轮、任意倾斜工作平面、复杂自由曲面。
