---
name: nx-mcp-pipeline
description: 作者：抖音 无趣。Use when a user uploads a 2D mechanical engineering drawing and gives a single start instruction such as “开始建模” — this skill is the top-level orchestrator that chains three frozen stages end to end without user confirmation: A nx-engineering-drawing-reader (drawing → structured modeling JSON), B nx-mcp-modeling-planner (JSON → FAST modeling plan → executable plan via runner build/check), C nx-mcp-plan-runner (executable plan → Siemens NX → PRT + STEP). Enforces strict stage-entry gates, keeps an internal pipeline-state.json, and reports the final result only in natural Chinese. Orchestration only: it never parses the drawing, never reinterprets dimensions, never generates modeling operations, never calls NX_MCP tools itself, and never modifies the frozen stages.
---

# nx-mcp-pipeline — 总控编排器

## 1. 角色与边界（冻结约束，最高优先级）

本 Skill 只做**最上层编排**。它把三个已冻结并验证通过的阶段自动串联：

```
A nx-engineering-drawing-reader     二维机械工程图 → 结构化建模 JSON
B nx-mcp-modeling-planner           建模 JSON → FAST modeling plan → executable plan
C nx-mcp-plan-runner                executable plan → Siemens NX → PRT + STEP
```

**禁止修改以下任何冻结对象**：
- `nx-engineering-drawing-reader`
- `nx-mcp-modeling-planner`
- `nx-mcp-plan-runner`
- `NX_MCP-Enhanced v2.1.1`
- `C# Loader`
- named pipe / SendMessage / resident Loader 架构

**禁止复制或重写 A/B/C 的核心逻辑**。总控只允许做：调用阶段、接收输出路径、判断进入下一阶段的条件、汇总耗时、用中文向用户汇报。

**总控自身禁止**：视觉解析工程图、重新解释尺寸、生成建模 operation、逐步调用 NX_MCP tools、自行做 edge/face selection、修改 executable plan、修模型、解析 STEP、重写 A/B/C 已有规则。

## 2. 触发与输入

- 触发：用户上传一张二维机械工程图，并发出一次"开始建模"类指令。
- 输入：用户上传的工程图文件。
- 执行前不向用户索取额外确认；正常成功路径中间不停顿。

## 3. 端到端流程

```
用户上传工程图 + “开始建模”
→ A（调用 nx-engineering-drawing-reader）
→ 门禁 A 判定
→ B（调用 nx-mcp-modeling-planner，输入仅限 A 的 JSON）
→ 门禁 B 判定
→ C（调用 nx-mcp-plan-runner，输入为 executable plan）
→ 记录 C 结果
→ 输出最终中文汇报（见 references/chinese-output.md）
```

任一阶段失败或门禁不通过：**立即停止整个 pipeline**，不得进入下一阶段，不得自动跨阶段修复，输出对应阶段的中文失败信息。

## 4. 阶段 A 接口

1. 调用 `nx-engineering-drawing-reader`（读取并遵循其 SKILL.md，将上传的工程图交给它处理）。
2. 要求 A 将最终结构化 JSON **落盘为文件**并返回：
   - JSON 文件路径
   - `unresolved` 数量（unresolved 数组长度）
   - `dimension_closure.status`
   - 解析耗时（秒）
3. 门禁 A（须**同时**满足）：
   - `unresolved` 数量 = 0
   - `dimension_closure.status = "closed"`
   - JSON 中存在 `overall_dimensions`、`coordinate_system`、`features`
4. 不满足 → 立即停止，输出中文失败（失败阶段：图纸解析），不进入 B。

## 5. 阶段 B 接口

1. 输入**只允许** A 阶段生成的 JSON 文件路径；调用 `nx-mcp-modeling-planner`（读取并遵循其 SKILL.md）。
2. B 正常路径禁止：重新读取原始工程图、OCR、联网、尺寸猜测、重读 `runner.py`、Runner README、`plan_schema.json`、`certified.py`。
3. B 必须使用已冻结契约：
   - `nx-mcp-modeling-planner/references/runner-contract.md`
   - `nx-mcp-modeling-planner/references/certified-tool-contract.json`
4. B 完成：A JSON → FAST modeling plan → runner build → executable plan → frozen check → executable check。
5. `B_BUILD_SECONDS` 单位统一为**秒**：若 runner build 原始结果为毫秒，必须先换算 `B_BUILD_SECONDS = build_ms / 1000`，禁止秒与毫秒直接相加。用户可见的"建模规划耗时" = `B_PLANNER_SECONDS` + `B_BUILD_SECONDS`。
5. 门禁 B（须**同时**满足）：
   - `check = passed`（B 报告）
   - unresolved reference = 0
   - illegal tool_args = 0
   - natural language placeholder = 0
   - executable plan 文件存在且非空
6. 任一失败 → 立即停止，输出中文失败（失败阶段：建模规划），不进入 C。

## 6. 阶段 C 接口

### 6.0 Preflight（Runner 启动前，唯一允许的环境准备）
在真正调用 `runner.py run` 之前，总控**只允许**做以下只读/环境准备，且这些都属于 preflight，不属于 repair：
- 检查 NX 进程是否已运行；未运行时启动 NX
- 等待 Loader named pipe 就绪
- 检查 Python 解释器（NX_MCP-Enhanced venv）是否存在可调用
- 检查 `runner.py`、executable plan 文件存在且非空
- 检查 workspace 目录存在

preflight 只处理"Runner 尚未正式开始建模"的环境问题。一旦 `runner.py run` 已启动并进入建模阶段，preflight 不再适用。

### 6.1 调用
1. 输入：B 输出的 executable plan 路径。
2. 调用现有 `nx-mcp-plan-runner`（`runner.py run`，工作目录 `%USERPROFILE%\NX_MCP_WORKSPACE\nx-mcp-plan-runner`）：
   - 必须 FAST 模式（B 输出为 FAST plan）
   - `--mode normal|benchmark` 由本次任务决定（默认 normal）
   - Runner 一次性连续执行全部 operation，等待其完整报告
3. 禁止：豆包人工逐步骤执行 NX_MCP、临时生成专用 Python 驱动、修改 `runner.py`、`step == N` 特判。

### 6.2 Fail-fast（最高优先级，强制执行）
- Runner 任意 operation 返回失败（`status=failed` / exit≠0 / `failed_step` 非空），即 Runner failed = Pipeline failed，整个 Pipeline **立即停止**。
- Runner 一旦进入正式建模阶段，任何 modeling step 失败都必须立即终止本次 Pipeline，**不得尝试"修一下继续"**。
- 失败后**严格禁止**以下任一行为：
  - 自动补建缺失几何
  - 手动 bridge 调用继续建模
  - 修改 `body_id` / `target_body_id`
  - 修改 executable plan 后续跑
  - 临时创建缺失的 body / feature
  - 任何自动 repair / fallback repair
  - 手动补执行后续 operation
  - 手动 save
  - 手动 STEP 导出
  - 为了产出结果绕过 Runner 失败
- 失败后只允许**只读汇报**，允许输出：failed step、tool、error、已完成 step 数、Runner log/report 路径、frozen plan 路径、executable plan 路径、其它只读诊断信息。
- 失败后**不得执行任何新的 NX 建模操作**。
- 环境问题与建模问题必须区分：
  - Runner 尚未正式开始（preflight 阶段）：可处理 NX 未启动、Loader 未就绪、Python 环境未找到。
  - Runner 已开始且某个 modeling operation 失败：必须 fail-fast，不得进入 repair mode。

### 6.3 成功
成功：记录 `C_RUNNER_SECONDS`、`BODY_COUNT`、`MODEL_BBOX`、`PRT_PATH`、`STEP_PATH`（英文键仅内部记录）。`MODEL_BBOX` 保存 min/max 结构；用户可见的"模型尺寸"按 长度 = `x_max−x_min`、宽度 = `y_max−y_min`、高度 = `z_max−z_min` 计算（单位 mm）。

## 7. Pipeline State 文件

每次任务维护轻量状态文件 `pipeline-state.json`（与阶段产物同目录）。内部字段允许英文，但**不得原样展示给用户**。必存字段：

- `stage_a`：`status` / `json_path` / `elapsed_seconds` / `unresolved_count` / `closure_status`
- `stage_b`：`status` / `plan_path` / `executable_plan_path` / `planner_elapsed_seconds` / `build_elapsed_seconds` / `check_status` / `operations`
- `stage_c`：`status` / `elapsed_seconds` / `body_count` / `model_bbox` / `prt_path` / `step_path`
- 顶层：`pipeline_status` / `current_stage` / `input_image` / `total_elapsed_seconds`

完整格式见 `examples/pipeline-state-example.json`。

**总耗时口径（强制）**：`total_elapsed_seconds` 是从 Pipeline 接收到"开始建模"并正式开始执行，到 C Runner 完成并返回最终报告为止的**真实墙钟时间**，不得用 A+B+C 内部耗时之和代替。

## 8. 中间阶段禁止长回复

- A 成功：不生成完整报告，仅在内部记录 `A_JSON_PATH / A_ELAPSED_SECONDS / A_UNRESOLVED / A_CLOSURE_STATUS`，立即进入 B。
- B 成功：不输出 plan 摘要、operation 表格、建模方案解释，仅在内部记录 `B_PLAN_PATH / B_EXECUTABLE_PLAN_PATH / B_PLANNER_SECONDS / B_BUILD_SECONDS / B_CHECK_STATUS / B_OPERATIONS`，立即进入 C。
- C 成功后才进行最终用户回复。

## 9. 用户可见输出（全部中文）

成功与各失败场景的输出模板、以及禁止出现的英文键名，见 `references/chinese-output.md`。任何情况下不得输出 `status`、`elapsed_seconds`、`operations`、`failed_step`、`body_count`、`model_bbox` 等英文键。

## 10. 性能规则

- 正常成功路径：上传 → A → B → C → 最终中文结果，中间不等待用户确认。
- 禁止：每阶段生成长回复、每阶段重新总结前一阶段、重复读取稳定源码、重复验证已通过的 contract、为最终汇报重新遍历完整 plan、生成长表格、输出思考过程。
- 总控额外编排开销目标：< 10 秒。

## 11. 资源导航

- `references/pipeline-contract.md`：三阶段完整接口规范、门禁条件、状态文件、禁止项清单。
- `references/chinese-output.md`：最终中文汇报模板与英文键→中文对照。
- `examples/pipeline-state-example.json`：完整成功任务的 pipeline-state 示例。
