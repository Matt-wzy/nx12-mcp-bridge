# 文字描述建模模块

本文件是 `nx-agent` 的内部规则，不是独立 Skill。

## 1. 输入要求

文字建模适用于用户已经用文字明确给出三维几何要求的任务。必须先确认：

1. 单位明确；未明确时不得静默假设。
2. 建模所需尺寸足以唯一确定最终几何。
3. 不依赖图像比例、像素测量或猜尺寸。
4. 不超出当前 certified tools 能力边界。

如果缺少会改变最终几何的关键尺寸，先向用户确认，不进入 Runner。

## 2. 统一执行链路（Mode A Fast Path）

文字建模不直接依赖 MCP 客户端 JSON 配置，也不由 Agent 临时逐步调用 NX_MCP。

```text
用户文字 → 结构化建模意图 → frozen plan → runner build/check → Plan Runner → resident C# Loader → Siemens NX → PRT + STEP
```

**正常一次通过路径只需要本文件 + `references/certified-tool-contract.json`。**
`SKILL.md` 已提供阶段 C、拓扑和输出硬规则；禁止再次预读
`modeling-planner.md / nx-mcp-rules.md / topology-safety.md / runner-contract.md /
pipeline-contract.md` 来“确认规则”。

详细 reference 只按需读取：
- build/check 明确出现 schema/contract incompatibility → 读 `runner-contract.md`；
- attempt 1 失败且允许一次 Controlled Self-Healing → 读 `pipeline-contract.md`
  与失败类型直接相关的 `topology-safety.md`；
- 正常成功路径不读旧 plan/report/history，不读取安装脚本，也不扫描仓库。

## 3. Runtime Config 与路径（固定解析，禁止搜索）

Runner 配置只允许按以下顺序解析一次：

1. 若存在环境变量 `NX_MCP_WORKSPACE`：
   `<NX_MCP_WORKSPACE>\nx-mcp-plan-runner\runtime-config.json`
2. 否则：
   `%USERPROFILE%\NX_MCP_WORKSPACE\nx-mcp-plan-runner\runtime-config.json`

读取成功后直接使用其中：
- `python_exe`
- `workspace_root`
- `repo_root`

Runner 固定为：
`<workspace_root>\nx-mcp-plan-runner\runner.py`

若上述 runtime-config 不存在或字段缺失，立即停止并报告“Agent Pack 安装不完整”；
**禁止递归搜索 runtime-config、runner.py、Python、仓库目录、Skill 目录或 install 脚本。**

文件规则：
- 新零件使用工作区相对路径；`nx_create_part` 显式写 `units`。
- PRT / STEP / frozen plan / executable plan / report 全部位于 `workspace_root`。
- 禁止把聊天目录、临时对话目录或其它任意绝对路径作为输出目录。
- 用户未指定文件名时生成简洁稳定的描述性文件名；若该目标文件已存在，
  只对**该候选路径**做存在性检查并追加 `_2 / _3 ...`，禁止为了处理重名去读取
  旧 plan、旧 report 或遍历历史任务。

## 4. 已有零件修改

一体化零配置路径只允许修改：用户明确指定、已经保存、位于 `NX_MCP_WORKSPACE` 内、可由 `nx_open_part` 以工作区相对路径打开的零件。

如果当前 NX 中有无关零件或未保存工作，Runner preflight 必须阻止覆盖。禁止为了继续任务自动关闭无关零件。

已有零件修改的 frozen plan 必须先：
1. `nx_open_part(path=<工作区相对路径>)`
2. `nx_list_bodies` 且 `expectation.body_count=1`
3. 后续第一个 `body_main` 引用由 Runner 自动绑定到该唯一实体

当前一体化安全路径只自动绑定**单实体零件**。多实体已有零件若没有明确可验证的目标 body 选择规则，必须停止并说明，禁止猜测。

## 5. 规划规则（Fast Path 冻结摘要）

- 只使用 `certified-tool-contract.json` 中的 32 个工具；`tool_args` 只含真实参数。
- frozen plan 顶层使用 `skill / mode / part / coordinate_system / operations /
  final_validation / fallbacks`；operation 至少含 `step / tool / tool_args /
  topology_changes`。逻辑对象名写裸名。**禁止手写** `result_bindings` /
  `selection_binding` / `retry` / `$reference`；这些全部由 Runner build 生成。
  frozen check/build 对混入 executable 字段的 plan 必须 fail-closed。
- edge/face 选择只写 `selection_criteria`；`expectation` 只做判定。
  后续 `edge_indices` / `remove_face_index` 若消费该选择，frozen 中必须写
  `<stepN ...>` 占位符，禁止写裸语义名；build 自动转换为 `$selection.sel_N`。
- 任何 Boolean / Hole / Pattern / Mirror / Blend / Chamfer 等拓扑变化后，
  旧 edge/face index 立即失效；需要继续选边/面时必须重新 list。
- 完整圆边使用 `curve_type:["Elliptical","Circular"] + length + midpoint_z + count`，
  禁止圆边 bbox；唯一顶/底面优先 `Planar + centroid_z + count`。
- `centroid_radius = sqrt(global_x² + global_y²)`，是到**全局 XY 原点**的距离，
  不是孔半径，禁止写 `diameter/2`。已知孔轴时优先按完整
  `centroid:[cx,cy,cz]` named group 验证。
- 圆角/倒角放在最终 Boolean 和孔之后。
- 连续主轮廓遵守 Profile-First：按真实主截面选择 XY / XZ / YZ。
  - XY 局部(x,y)→全局(X,Y)，挤出 +Z
  - XZ 局部(x,y)→全局(X,Z)，挤出 +Y
  - YZ 局部(x,y)→全局(Y,Z)，挤出 +X
- 竖板/侧板 X/Y 轴孔：对应主平面圆草图 + `nx_extrude(operation="subtract")`；
  hole 系列只用于 Z 轴孔。
- build/check 必须一次通过；成功后直接进入 Runner，不做额外“二次规则核对”。

## 6. 执行与自修复

正常新零件任务**不主动读取** `run_history.json`、旧 report、旧 frozen/executable
plan，也不自行判断旧零件 dirty 状态；这些安全判断全部交给 Runner preflight。

第一次执行失败时，当前 attempt 立即停止。此时才允许读取本次失败 report，
并按需读取 `references/pipeline-contract.md` 判断是否可做最多 1 次受控自动修复。
不得在失败模型上接着补；repair 后从头完整重跑；第二次失败结束。

## 7. `nx_release` 语义

在 resident C# Loader 架构下，`nx_release` 不会停止 Loader，也不会关闭 named pipe。它只清除当前任务状态并恢复正常 NX 交互；Loader 在整个 NX 会话中继续保持 ready。

## 8. 最终汇报

只报告实际完成并验证的内容：状态、自动修复次数、实体数量、模型尺寸、PRT/STEP 路径、真实总耗时；若修复过，必须披露首次失败与修复内容。
