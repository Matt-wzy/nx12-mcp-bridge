# 文字描述建模模块

本文件是 `nx-agent` 的内部规则，不是独立 Skill。

## 1. 输入要求

文字建模适用于用户已经用文字明确给出三维几何要求的任务。必须先确认：

1. 单位明确；未明确时不得静默假设。
2. 建模所需尺寸足以唯一确定最终几何。
3. 不依赖图像比例、像素测量或猜尺寸。
4. 不超出当前 certified tools 能力边界。

如果缺少会改变最终几何的关键尺寸，先向用户确认，不进入 Runner。

## 2. 统一执行链路

文字建模不直接依赖 MCP 客户端 JSON 配置，也不由 Agent 临时逐步调用 NX_MCP。

```text
用户文字 → 结构化建模意图 → 建模规划模块 → frozen plan → runner build/check → Plan Runner → resident C# Loader → Siemens NX → PRT + STEP
```

读取并遵循：

- `references/modeling-planner.md`
- `references/nx-mcp-rules.md`
- `references/topology-safety.md`
- `references/runner-contract.md`
- `references/certified-tool-contract.json`
- `references/pipeline-contract.md` 中阶段 C / Controlled Self-Healing 规则

## 3. 路径

- 新零件使用工作区相对路径。
- `nx_create_part` 显式写 `units`。
- PRT / STEP / plan / report 必须位于 `runtime-config.json.workspace_root`。
- 禁止把聊天目录或任意绝对路径作为输出目录。

## 4. 已有零件修改

一体化零配置路径只允许修改：用户明确指定、已经保存、位于 `NX_MCP_WORKSPACE` 内、可由 `nx_open_part` 以工作区相对路径打开的零件。

如果当前 NX 中有无关零件或未保存工作，Runner preflight 必须阻止覆盖。禁止为了继续任务自动关闭无关零件。

## 5. 规划规则

- 只使用 32 个 certified tools。
- `tool_args` 只含真实工具参数。
- edge/face 选择写入 `selection_criteria`。
- `expectation` 只做判定。
- 拓扑变化后重新 list。
- 圆角/倒角放在最终 Boolean 和孔之后。
- 分离闭合轮廓遵守 Separated Closed Profiles Rule。
- 连续主轮廓遵守 Profile-First Rule。
- build/check 必须一次通过。

## 6. 执行与自修复

第一次执行失败时，当前 attempt 立即停止。只允许按 `references/pipeline-contract.md` 判断是否可做一次受控自动修复。不得在失败模型上接着补；repair 后从头完整重跑；第二次失败结束。

## 7. `nx_release` 语义

在 resident C# Loader 架构下，`nx_release` 不会停止 Loader，也不会关闭 named pipe。它只清除当前任务状态并恢复正常 NX 交互；Loader 在整个 NX 会话中继续保持 ready。

## 8. 最终汇报

只报告实际完成并验证的内容：状态、自动修复次数、实体数量、模型尺寸、PRT/STEP 路径、真实总耗时；若修复过，必须披露首次失败与修复内容。
