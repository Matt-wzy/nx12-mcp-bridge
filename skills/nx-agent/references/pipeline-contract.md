# NX Agent 工程图模式接口规范

## 1. 总控职责
工程图模式只负责编排 A→B→C，不自行解析图纸、不重新解释尺寸、不直接生成 operation、不手动调用 NX_MCP 补建。

## 2. 阶段 A：工程图读取
读取 `drawing-reader.md` 与 `nx-drawing-rules.md`。
输出结构化 JSON 并落盘。

门禁：
- unresolved=0
- dimension_closure.status="closed"
- overall_dimensions / coordinate_system / features 均存在

失败立即停止。

## 3. 阶段 B：建模规划
输入只允许 A 生成的 JSON。
读取：
- `modeling-planner.md`
- `runner-contract.md`
- `certified-tool-contract.json`
- `nx-mcp-rules.md`
- `topology-safety.md`

正常路径：
```text
A JSON → FAST plan → 发布前静态自检 → frozen plan → runner build → runner check → executable plan
```

runner build/check 任一失败即 B 失败。禁止修改 frozen plan 后自动重跑。

## 4. 阶段 C：Plan Runner
Runner 路径：
`%USERPROFILE%\NX_MCP_WORKSPACE\nx-mcp-plan-runner\runner.py`

Preflight 只允许：
- 检查/启动 NX
- 探测 named pipe `nx_mcp_loader`
- 检查 Python/Runner/plan/workspace

Loader ready：
优先使用仓库 `loader/nx_client.ps1 -Cmd nx_status`；CONNECTED + ok=true + ready=true 即 ready。
禁止用 `%LOCALAPPDATA%\nx-mcp\bridge.json` 判断 resident Loader。

Runner 正式开始建模后，任一 operation 失败：
- Runner failed = Pipeline failed
- 立即停止
- 禁止自动 repair/fallback repair
- 禁止修改 plan 续跑
- 禁止手动 bridge 补建
- 禁止手动 save / STEP 导出

## 5. 总耗时
`total_elapsed_seconds` = 从收到“开始建模”并正式执行，到 Runner 最终返回的真实墙钟时间。
禁止用 A+B+C 内部耗时简单相加代替。

## 6. 用户输出
最终用户可见格式以 `chinese-output.md` 为准。
