# 二维机械图自动建模

可选 **Agent Pack**：将“二维机械工程图 → Siemens NX 自动建模”完整链路打包进豆包。

```
工程图 → Drawing Reader → Modeling Planner → Plan Runner → Siemens NX → PRT + STEP
```

## 使用流程

1. 安装 NX_MCP-Enhanced（Loader、named pipe、resident Loader 架构按 INSTALL.md 就绪）。
2. 确认 Loader 正常连接。
3. PowerShell 执行：

   ```powershell
   .\install-agent.ps1
   ```

   多 Profile 环境可手动指定：

   ```powershell
   .\install-agent.ps1 -DoubaoProfile "Profile 7"
   ```

4. 重启 / 新开一个豆包对话。
5. 上传二维机械工程图。
6. 输入：

   ```
   开始建模
   ```

7. 自动执行：

   ```
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

## 说明

- NX_MCP 本身可以独立使用；Agent Pack 是可选增强层。
- Pipeline 由三个冻结阶段组成：`nx-engineering-drawing-reader`（图纸解析）、
  `nx-mcp-modeling-planner`（建模规划）、`nx-mcp-plan-runner`（NX 执行）。
- Agent Pack 不修改 NX_MCP-Enhanced 核心、C# Loader、named pipe / resident
  Loader 架构，也不新增 NX 建模工具。
