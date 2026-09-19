# NX Agent 工程图/文字建模执行规范

## 1. 总控职责
本规范负责统一编排文字建模与工程图建模的计划执行层，不直接手工补建几何。

核心策略：**Fail-fast + Controlled Self-Healing（受控自动修复）**。

含义：
- 每一次 Runner 尝试内部仍然严格 fail-fast；
- 失败后当前尝试立即停止；
- 只有满足安全门禁时，才允许最多 1 次受控自动修复；
- 自动修复必须从干净状态完整重跑，禁止从失败步骤续跑。

## 2. Runtime Config（强制）

安装器会在 Runner 目录生成 `runtime-config.json`，至少包含：

- `python_exe`
- `workspace_root`
- `nx_mcp_src`
- `repo_root`

执行 Runner 时必须优先读取该文件，并使用其中的 `python_exe` 与 `workspace_root`。
所有 plan / report / PRT / STEP 都必须落在 `workspace_root` 内，禁止把聊天目录或临时对话目录当作工作区。

## 3. 阶段 A：工程图读取
读取上传图纸，再依次读取 `drawing-reader.md` 与 `nx-drawing-rules.md`。规则读取完成后，立即按 §2 固定路径只定位并读取一次 runtime-config；不得等待全部视图解析、全部尺寸绑定、feature ownership、relation closure、global coordinate conversion、source ledger 或 drawing JSON 完成。随后进行完整 drawing interpretation，输出 drawing JSON 并落盘。正常路径不预读 examples。
使用 runtime-config 中的 `python_exe` 执行：

```text
runner.py validate-drawing <drawing.json> --new-task
```

门禁：
- validate-drawing exit code=0
- `mode_b_task_id` 由 Runner 生成；保存返回的 `mode_b_task.task_root`
- 同一任务的 schema retry 使用 `--task-root <path>`，不得重新 `--new-task`
- validator 返回 `source_ownership.status="pass"`
- validator 返回 `coordinate_sanity.status="pass"`
- blocking_unresolved=0（只统计 `required_for_modeling=true`）
- dimension_conflicts=0
- dimension_closure.status="closed"

bbox / feature center / profile range / count / symmetry / source semantic 由 validator 自己计算；禁止把 Agent 自写的 pass 状态当机器门禁。
validator 失败立即停止，**不允许 Agent 自己覆盖错误，也不允许自动修复**。warnings 与 soft unresolved 不阻塞 A。

## 4. 阶段 B：建模规划
输入可以是工程图模式 A 阶段 JSON，或文字模式中已经确认完整的结构化建模意图。
读取：
- `modeling-planner.md`
- `runner-contract.md`
- `certified-tool-contract.json`
- `nx-mcp-rules.md`
- `topology-safety.md`

正常路径：
```text
结构化输入 → 语义/数量/能力预检 → FAST plan → 发布前静态自检 → frozen plan → runner build → runner check → executable plan
```

frozen plan 落盘前必须满足：
- `capability_violations=0`；required feature 超出 certified tools 时优先使用输入 surrogate，否则只允许机器参数化 resolver 的可追溯 approximation；两者都不可用时直接 B 失败；
- Reader/结构化输入中的 HARD geometry field 与 `derived.target` 原样保留，禁止跨 feature 复用 source；
- pattern 总实例数与源 `count` 完全一致，禁止因 symmetry/mirror 再次乘倍；
- unsupported threaded feature 不得被删除或替换成普通 through/clearance hole；metric thread surrogate 必须来自 project-supported coarse-pitch subset 或 drawing explicit pitch，再统一使用 `nominal_diameter - pitch`，禁止 thread-size → 最终孔径映射。Gate B 必须核对实际 operation 的 axis/center/depth/显式 axial range/count 与 drawing，禁止依赖 Loader 默认补设计几何。

上述预检或 runner build/check 任一失败即 B 失败。B 阶段失败**不进入自修复**，禁止修改 frozen plan 后自动重跑。

## 5. 阶段 C：Plan Runner
Runner 路径：
`<runtime-config.workspace_root>\nx-mcp-plan-runner\runner.py`

### 5.1 Preflight
只允许：
- 检查/启动 NX
- 探测 named pipe `nx_mcp_loader`
- 检查 Python/Runner/plan/workspace

Loader ready：
优先使用仓库 `loader/nx_client.ps1 -Cmd nx_status`；
CONNECTED + ok=true + ready=true 即 ready。

禁止用 `%LOCALAPPDATA%\nx-mcp\bridge.json` 判断 resident Loader。

### 5.2 单次尝试的 fail-fast
Runner 正式开始建模后，任一 operation 失败：
- 当前 Runner 尝试立即停止；
- 禁止在当前 dirty model 上继续执行后续步骤；
- 禁止从失败步骤续跑；
- 禁止手动 bridge 补建；
- 禁止手动 save / STEP 导出；
- 记录 failed_step / reason / attempt_elapsed_seconds。

这一步只结束“当前尝试”，是否进入一次受控自动修复由 §5 决定。

## 6. Controlled Self-Healing（受控自动修复）

### 6.1 次数
每次 Pipeline **最多 1 次**自动修复：
- attempt 1 失败 → 可评估 repair；
- repair 后 attempt 2 成功 → 最终“成功（自动修复后）”；
- attempt 2 再失败 → 最终失败，禁止第三次尝试。

### 6.2 允许修复的范围
只有根因明确、可确定、不会改变设计语义时才允许：
1. edge / face `selection_criteria` 匹配 0 条或数量不符；
2. Loader 已冻结的返回语义差异，例如：
   - 完整圆边 `Elliptical` / `Circular`；
   - 孔侧面 `Swept` / `Cylindrical`；
3. 筛选条件过严，可替换为冻结契约中已验证的稳定组合；
4. frozen/executable 边界污染等**纯计划表达错误**（例如 Planner 误写
   `result_bindings` / `selection_binding` / `retry` / `$reference`），
   且修复仅删除/改写绑定表达，不改变任何尺寸、特征、选择几何或建模顺序；
5. 上一次失败由**当前 Pipeline 自己创建**的计划输出零件处于 dirty 状态，需要无保存清理后完整重跑。

### 6.3 禁止自动修复
以下任一情况必须最终失败：
- A 阶段 blocking_unresolved > 0 / dimension conflict / 关键几何尺寸缺失；
- 需要猜尺寸、改尺寸、改孔位、改特征数量；
- **禁止数值 nudge / epsilon 修复**：任何来自工程图、derived、frozen plan 的几何数值（坐标、起止面、slot 底、孔中心、直径、深度、厚度、圆角/倒角等）都不得为了让 Boolean/切除成功而改成邻近值，例如 `Z=50 → Z=49`。即使 Agent 认为“最终实体等价”，也不能把这种数值改写当成 Controlled Self-Healing；
- 若失败根因是精确相切/共面导致的 CAD kernel Boolean 问题，只能使用**不改变任何设计数值与最终几何语义**、且已由 frozen contract 明确允许的计划级/选择级修复；没有这样的确定性修复路径就最终失败，不得靠扩大/缩短工具体来穿过门禁；
- Boolean 不相交且根因属于几何设计/规划错误；
- 超出 certified tools 能力边界；
- 需要新增或修改 NX_MCP / Runner / Loader；
- 当前 dirty part 不是本 Pipeline 本次任务自己创建的目标零件；
- 无法确定修复是否改变最终几何；
- 第一次修复后的 attempt 2 再次失败。

### 6.4 修复过程（强制）
允许修复时必须按以下顺序：
1. 保留 attempt 1 失败报告；
2. 只读诊断失败原因；
3. 生成 **repair plan v1**，只修改已确认的计划级问题；
4. 重新执行 runner build + check；失败则最终失败；
5. 禁止 Agent 手动关闭 dirty part；
6. 第二次执行必须交给 Runner preflight 安全处理本任务自己的 planned dirty part；
7. 从 C 的第 1 步完整重跑 executable plan；
8. 禁止从 failed_step 接着执行；
9. 最终报告必须披露自动修复次数、首次失败步骤和首次失败原因。

第二次执行必须使用：
```text
python_exe runner.py build <repair-frozen.json> <repair-executable.json> \
  --drawing <drawing.json> \
  --task-root <首次 validate 返回的 task_root>

python_exe runner.py run <repair-executable.json>
  --workspace <workspace_root>
  --report <attempt2-report.json>
  --mode benchmark
  --allow-overwrite
  --repair-attempt 1
  --repair-report <attempt1-report.json>
```

Runner 会机器校验 previous report、planned part 和 repair 次数，防止第三次 repair。

### 6.5 允许的 selection 修复示例
- 唯一顶面：
  `Planar + centroid_z + count=1`
  优先于 `Planar + normal + ...`。
- 完整圆顶边：
  `["Elliptical","Circular"] + length + midpoint_z + count`
  禁止曲线 bbox。
- 四角竖边：
  `Linear + direction="Z" + corners_xy + bbox_z + count`。

## 7. 面选择稳定规则
当目标是唯一顶/底 Planar 面时：
- 首选 `face_type:"Planar" + centroid_z + expectation.count`；
- `normal` 只作为辅助信息，不作为首要硬筛选；
- `area` 只辅助，不作单点失败条件；
- 同一 Z 高度存在多个 Planar 面时，再增加完整 `centroid` 或 `area` 区分。

Shell remove face 若最高 Z 只有一个 Planar 面，固定按：
```json
{
  "selection_criteria": {
    "face_type": "Planar",
    "centroid_z": {"value": 40.0, "tol": 0.5}
  },
  "expectation": {"count": 1}
}
```

## 8. 总耗时
`total_elapsed_seconds` 必须是**真实墙钟时间**：
从收到“开始建模”并正式执行，到最终成功/失败报告返回。

如果发生自动修复，总耗时必须包含：
- attempt 1；
- 失败诊断；
- repair plan；
- build/check；
- dirty part 清理；
- attempt 2；
- 最终验证与汇报。

禁止用“成功那一轮”的 A+B+C 或 Runner 时间冒充总耗时。

`C_RUNNER_SECONDS` = 所有 Runner 尝试的 elapsed_seconds 之和。

## 9. 用户输出
最终用户可见格式以 `chinese-output.md` 为准。
