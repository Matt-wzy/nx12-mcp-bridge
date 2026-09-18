# nx-mcp-pipeline 阶段接口规范（冻结编排契约）

> 本文件是 `nx-mcp-pipeline` 的编排接口规范，只描述**总控如何调用三个冻结阶段、如何判定门禁、如何记录状态**。
> 它不是 A/B/C 的实现，不包含任何图纸解析规则、建模规划规则或 Runner 执行逻辑。

## 1. 总控职责边界

### 1.1 总控只允许负责
- 调用阶段 A
- 接收 A 输出文件路径
- 判断 A 是否满足进入 B 的条件
- 调用阶段 B
- 接收 B 输出文件路径
- 判断 B 是否满足进入 C 的条件
- 调用阶段 C
- 接收 Runner 最终结果
- 汇总阶段耗时
- 最终用中文向用户汇报

### 1.2 总控禁止
- 自己进行工程图视觉解析
- 自己重新解释尺寸
- 自己生成建模 operation
- 自己逐步调用 NX_MCP tools
- 自己进行 edge / face selection
- 自己修改 executable plan
- 自己修模型
- 自己解析 STEP
- 自己重写 A/B/C 已有规则

### 1.3 冻结对象（禁止修改）
| 冻结对象 | 说明 |
|---|---|
| `nx-engineering-drawing-reader` | 阶段 A 本体 |
| `nx-mcp-modeling-planner` | 阶段 B 本体 |
| `nx-mcp-plan-runner` | 阶段 C 本体（`%USERPROFILE%\NX_MCP_WORKSPACE\nx-mcp-plan-runner`） |
| `NX_MCP-Enhanced v2.1.1` | 冻结版本 |
| `C# Loader` | 冻结二进制 |
| named pipe / SendMessage / resident Loader 架构 | 冻结通信架构 |

任何阶段失败时，总控**不得**修改上述对象来"修复"，只能停止并上报。

## 2. 阶段 A 接口（图纸解析）

### 2.1 调用
- 调用方式：遵循 `nx-engineering-drawing-reader` 的 SKILL.md，把用户上传的二维机械工程图交给该阶段。
- 阶段产物：A 输出的最终结构化 JSON，必须**落盘为文件**（建议 `<任务目录>/<part>_drawing.json`）。

### 2.2 A 必须返回给总控
| 内部记录键 | 来源 |
|---|---|
| `A_JSON_PATH` | A 输出的 JSON 文件绝对路径 |
| `A_UNRESOLVED` | JSON 中 `unresolved` 数组长度 |
| `A_CLOSURE_STATUS` | JSON 中 `dimension_closure.status` |
| `A_ELAPSED_SECONDS` | A 阶段解析耗时（秒） |

### 2.3 门禁 A（进入 B 的条件，须同时满足）
1. `A_UNRESOLVED` = 0
2. `A_CLOSURE_STATUS` = `"closed"`
3. JSON 中存在：
   - `overall_dimensions`
   - `coordinate_system`
   - `features`

### 2.4 不满足时的行为
- 立即停止整个 pipeline，**不得继续 B**。
- 用户可见输出（全部中文，模板见 chinese-output.md）：
  - 状态：失败
  - 失败阶段：图纸解析
  - 失败原因：……
  - 需要确认：……
- 禁止输出英文状态字段。

## 3. 阶段 B 接口（建模规划）

### 3.1 调用
- 输入：**只允许** A 阶段生成的 JSON 文件路径。
- 调用方式：遵循 `nx-mcp-modeling-planner` 的 SKILL.md。

### 3.2 B 正常路径禁止
- 重新读取原始工程图
- OCR
- 联网
- 尺寸猜测
- 重读 `runner.py`
- 重读 Runner README
- 重读 `plan_schema.json`
- 重读 `certified.py`

### 3.3 B 必须使用的冻结契约（路径）
```
<nx-mcp-modeling-planner>/references/runner-contract.md
<nx-mcp-modeling-planner>/references/certified-tool-contract.json
```
（Skill 一律按名称定位，不绑定任何机器绝对路径：
`<nx-mcp-modeling-planner>/references/`，由 Agent 按已安装 Skill 名称解析）

### 3.4 B 必须完成
```
A JSON → FAST modeling plan → runner build → executable plan
       → frozen check → executable check
```

### 3.5 B 必须返回给总控
| 内部记录键 | 来源（B 报告） |
|---|---|
| `B_PLAN_PATH` | frozen plan 文件路径 |
| `B_EXECUTABLE_PLAN_PATH` | executable plan 文件路径 |
| `B_PLANNER_SECONDS` | 规划耗时（秒） |
| `B_BUILD_SECONDS` | build 耗时（**秒**）；若 runner build 原始结果为毫秒，Pipeline 接收后必须先换算 `B_BUILD_SECONDS = build_ms / 1000`，再写入；禁止秒与毫秒直接相加 |

用户可见的"建模规划耗时" = `B_PLANNER_SECONDS` + `B_BUILD_SECONDS`（两值单位均为秒）。
| `B_CHECK_STATUS` | check 结果（`passed` / `failed`） |
| `B_OPERATIONS` | operation 数量 |

### 3.6 门禁 B（进入 C 的条件，须同时满足）
1. `B_CHECK_STATUS` = `passed`
2. unresolved reference = 0
3. illegal tool_args = 0
4. natural language placeholder = 0
5. `B_EXECUTABLE_PLAN_PATH` 文件存在且非空

### 3.7 不满足时的行为
- 任一失败立即停止，**不得进入 C**。
- 用户可见输出：状态：失败 / 失败阶段：建模规划 / 失败原因：……

## 4. 阶段 C 接口（NX 建模执行）

### 4.1 调用
- 输入：B 输出的 executable plan 路径。
- 调用：现有 `nx-mcp-plan-runner`，命令（工作目录 `%USERPROFILE%\NX_MCP_WORKSPACE\nx-mcp-plan-runner`）：

```
python runner.py run <executable-plan.json> [--workspace DIR] [--report out.json]
                    [--mode normal|benchmark] [--allow-overwrite] [--history FILE]
```

- 必须：
  - FAST 模式（B 输出为 FAST plan）
  - `normal` 或 `benchmark` 模式由本次任务决定（默认 `normal`；仅当任务明确要求对测试件安全覆盖时用 `benchmark` + `--allow-overwrite`）
  - Runner 一次性连续执行全部 operation，等待完整报告
- 禁止：
  - Agent 人工逐步骤执行 NX_MCP
  - 临时生成专用 Python 驱动
  - 修改 `runner.py`
  - `step == N` 特判

### 4.2 Preflight（Runner 启动前）
在调用 `runner.py run` 之前，总控只允许做以下环境准备，属于 preflight，不属于 repair：
- 检查 NX 是否运行；未运行时启动 NX
- 等待并验证 resident C# Loader 的 named pipe `nx_mcp_loader` ready
- 检查 Python 环境（NX_MCP-Enhanced venv）
- 检查 `runner.py` 与 executable plan 文件
- 检查 workspace 目录

**Loader readiness 契约（强制）：**
- 当前推荐后端是 C# resident Loader，唯一有效的就绪判据是 named pipe `nx_mcp_loader` 可以连接并返回成功响应。
- 推荐探测命令：
  ```powershell
  powershell -NoProfile -ExecutionPolicy Bypass -File "<repo>\loader\nx_client.ps1" -Cmd nx_status
  ```
  `CONNECTED` + `"ok":true` + `"ready":true` → Loader ready。
  `ping` 返回 `pong` 也可作为轻量 ready 判据。
- **不得检查或依赖 `%LOCALAPPDATA%\nx-mcp\bridge.json` 来判断 C# Loader。**
  `bridge.json` 是 legacy Python visual bridge descriptor；resident Loader 模式不要求创建该文件。
- `bridge.json` 缺失时不得推断 Loader 未加载、不得要求重启 NX。
- named pipe 失败时才允许进一步检查 DLL 部署位置与 `nx_mcp_loader.log`；只有确认 NX 是在安装 Loader / 设置 `UGII_USER_DIR` 之前启动时，才允许要求一次重启。
- 重启后必须重新 probe named pipe；同一任务不得因 legacy descriptor 缺失反复要求重启。

preflight 只处理"Runner 尚未正式开始建模"的环境问题。Runner 一旦进入建模阶段，preflight 不再适用。

### 4.3 Fail-fast（最高优先级，强制执行）
- Runner 任意 operation 返回失败（`status=failed` / exit≠0 / `failed_step` 非空）→ **Runner failed = Pipeline failed**，整个 Pipeline 立即停止。
- Runner 一旦进入正式建模阶段，任何 modeling step 失败都必须立即终止本次 Pipeline，**不得尝试"修一下继续"**。
- 失败后严格禁止：
  - 自动补建缺失几何
  - 手动 bridge 调用继续建模
  - 修改 `body_id` / `target_body_id`
  - 修改 executable plan 后续跑
  - 临时创建缺失 body / feature
  - 任何自动 repair / fallback repair
  - 手动补执行后续 operation
  - 手动 save
  - 手动 STEP 导出
  - 为了产出结果绕过 Runner 失败
- 失败后只允许**只读汇报**，允许输出：failed step、tool、error、已完成 step 数、Runner log/report 路径、frozen plan 路径、executable plan 路径、其它只读诊断信息；**不得执行任何新的 NX 建模操作**。
- 环境问题与建模问题必须区分：
  - Runner 尚未正式开始 → 可按 §4.2 处理 NX/Loader/Python 环境问题。
  - Runner 已开始且 modeling operation 失败 → 必须 fail-fast，不得进入 repair mode。
- 禁止 Pipeline 自己修改 plan 后继续；禁止自动修改 Runner / NX_MCP / Loader。
- 用户可见输出：状态：失败 / 失败阶段：NX 建模 / 失败步骤：…… / 失败原因：……（全部中文）。

### 4.4 成功时记录
| 内部记录键 | 来源（Runner 报告） |
|---|---|
| `C_RUNNER_SECONDS` | Runner 总耗时（秒） |
| `BODY_COUNT` | 最终实体数量 |
| `MODEL_BBOX` | 模型包围盒，min/max 结构（见下） |
| `PRT_PATH` | PRT 文件绝对路径 |
| `STEP_PATH` | STEP 文件绝对路径 |

`MODEL_BBOX` 必须保存 min/max 结构，例如：
```json
{"x_min": -90, "x_max": 90, "y_min": -55, "y_max": 55, "z_min": 0, "z_max": 36}
```
用户可见的"模型尺寸"由此计算（单位 mm）：
- 长度 = `x_max - x_min`
- 宽度 = `y_max - y_min`
- 高度 = `z_max - z_min`

上例 → `180 × 110 × 36 mm`。禁止向用户输出 `-90..90 × -55..55 × 0..36` 这类区间表示。

## 5. Pipeline State 文件

- 每次任务维护一个轻量状态文件：`pipeline-state.json`，与阶段产物同目录。
- 建议格式（与 `examples/pipeline-state-example.json` 一致）：

```json
{
  "pipeline_status": "running",
  "current_stage": "A",
  "input_image": "",
  "stage_a": {
    "status": null,
    "json_path": null,
    "elapsed_seconds": null,
    "unresolved_count": null,
    "closure_status": null
  },
  "stage_b": {
    "status": null,
    "plan_path": null,
    "executable_plan_path": null,
    "planner_elapsed_seconds": null,
    "build_elapsed_seconds": null,
    "check_status": null,
    "operations": null
  },
  "stage_c": {
    "status": null,
    "elapsed_seconds": null,
    "body_count": null,
    "model_bbox": null,
    "prt_path": null,
    "step_path": null
  },
  "total_elapsed_seconds": null
}
```

- 内部字段允许英文；**这些英文 JSON 字段不得原样展示给用户**。
- 生命周期：任务开始置 `pipeline_status=running`；每阶段完成后更新对应小节与 `current_stage`；结束时置 `pipeline_status=success|failed` 并写 `total_elapsed_seconds`。
- 状态文件字段与总控实际使用的门禁（`unresolved_count` / `closure_status` / `check_status` / `operations`）和最终汇报字段（`planner_elapsed_seconds` + `build_elapsed_seconds` / `body_count` / `model_bbox` / `prt_path` / `step_path`）保持一致。

### 5.1 总耗时口径（强制）

`total_elapsed_seconds` 必须定义为**真实墙钟时间**：
从 Pipeline 接收到"开始建模"并正式开始执行，到 C Runner 完成并返回最终报告为止。
**不得**简单使用 A、B、C 内部耗时之和（`A_ELAPSED_SECONDS + B_PLANNER_SECONDS + B_BUILD_SECONDS + C_RUNNER_SECONDS`）代替。

## 6. 中间阶段禁止输出长回复

| 阶段 | 成功后行为 |
|---|---|
| A | 不生成完整报告；内部记录 `A_JSON_PATH / A_ELAPSED_SECONDS / A_UNRESOLVED / A_CLOSURE_STATUS`，立即进入 B |
| B | 不输出 plan 摘要、operation 表格、建模方案解释；内部记录 `B_PLAN_PATH / B_EXECUTABLE_PLAN_PATH / B_PLANNER_SECONDS / B_BUILD_SECONDS / B_CHECK_STATUS / B_OPERATIONS`，立即进入 C |
| C | 记录 `C_RUNNER_SECONDS / BODY_COUNT / MODEL_BBOX / PRT_PATH / STEP_PATH`，然后才进行最终用户回复 |

## 7. 性能规则

- 正常成功路径：用户上传工程图 → A → B → C → 最终中文结果，中间不得等待用户再次确认。
- 禁止：
  - 每阶段生成长回复
  - 每阶段重新总结前一阶段
  - 重复读取稳定源码
  - 重复验证已经通过的 contract
  - 为最终汇报重新遍历完整 plan
  - 生成长表格
  - 输出思考过程
- 总控本身额外编排开销目标：< 10 秒。

## 8. 版本标识

- 本编排契约版本：`pipeline_contract_version = 1.0`（冻结于 2026-09-18）。
- 所依赖的冻结契约版本：`runner_contract_version = 1.0`、`certified_tool_contract_version = 1.0`、`plan_schema.json schema_version = 1.1`。
