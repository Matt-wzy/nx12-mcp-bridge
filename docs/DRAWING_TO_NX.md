# 二维机械图自动建模

NX_MCP-Enhanced 仓库已内置完整 Agent Pack：

```text
工程图
→ Drawing Reader
→ Modeling Planner
→ Plan Runner
→ Siemens NX
→ PRT + STEP
```

## 安装

推荐直接使用仓库根目录的一体化安装器：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1
```

它会一次完成 NX_MCP-Enhanced 核心、C# Loader、统一 `nx-agent` Skill 和 Plan Runner 的安装。正常用户不需要再单独执行 `install-agent.ps1`。

多 Profile 环境可手动指定：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 -AgentProfile "Profile 7"
```

## 使用

1. 安装完成后启动 / 重启 Siemens NX。
2. 新开一个Agent 对话。
3. 上传二维机械工程图。
4. 输入：

```
开始建模
```

之后自动执行：

```text
工程图 → Drawing Reader → Modeling Planner → Plan Runner → Siemens NX → PRT + STEP
```

## 当前适合

- 安装座 / 支架 / 法兰 / 板件 / 轴承座 / 阶梯轴 / 普通壳体
- Extrude / Cut / Hole
- Counterbore / Countersink
- Mirror / Pattern
- Fillet / Chamfer
- Shell / Revolve

## 当前不支持或不建议

- Sweep
- Loft
- 渐开线齿轮
- 斜齿轮
- 真实螺纹
- 任意倾斜工作平面
- 复杂自由曲面

**尺寸闭合约束**：如果图纸关键尺寸不闭合，Pipeline 必须停止，不允许猜尺寸。

## 组件

对外只安装一个 Skill：

- `nx-agent`：统一入口，自动识别文字建模 / 已有零件修改 / 二维工程图建模

工程图模式内部仍按以下模块执行：

- 工程图读取：二维工程图 → 结构化 JSON
- 建模规划：JSON → FAST modeling plan → executable plan
- `nx-mcp-plan-runner`：确定性执行 plan

Agent Pack 不修改 NX_MCP-Enhanced 核心、C# Loader、named pipe / resident
Loader 架构，也不新增 NX 建模工具。

高级用户如果只需要重新安装 Agent Pack，可以单独运行：

```powershell
.\install-agent.ps1
```
