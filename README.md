# NX MCP — Enhanced Edition (V2)

> **原始项目**：[DreamEnding/NX_MCP](https://github.com/DreamEnding/NX_MCP) — MIT License.
> 本项目为 **增强版**，在原项目基础上增加了 C# 常驻 NXOpen 后端
>（`NX_MCP_Loader`）、扩展建模工具，以及用于“二维工程图 → NX 自动建模”的一体化 Agent Pack。

作者：抖音 无趣

NX MCP 是一套本地运行的 Siemens NX 自动化系统，可通过 Agent 自动读取二维机械工程图、生成建模计划并驱动 Siemens NX 完成建模。所有组件均在本机运行，不依赖云端服务。

---

## 推荐架构（V2）

```text
Agent
  -> Drawing Reader
  -> Modeling Planner
  -> Plan Runner
  -> Python MCP sidecar
  -> LoaderBridge
  -> named pipe (localhost)
  -> NX_MCP_Loader.dll
  -> NXOpen
  -> Siemens NX
```

常驻 Loader 会在 NX 启动时自动加载，正常使用无需 `Alt+F8` 或手动运行 Journal。
`NX_MCP_BACKEND=auto` 默认优先使用 Loader，仅在 Loader 不可用时回退到旧版 Python bridge。

---

## 已认证工具（32 个）

已在 Windows + Siemens NX 2506 真机环境完成验证。

| 分类 | 工具 |
| --- | --- |
| 文件 | `nx_create_part`, `nx_open_part`, `nx_save_part`, `nx_close_part`, `nx_export_step` |
| 状态 / 查询 | `nx_status`, `nx_list_sketches`, `nx_list_bodies`, `nx_list_features` |
| 几何检查 | `nx_list_edges`, `nx_list_faces` |
| 草图 | `nx_create_sketch`, `nx_sketch_line`, `nx_sketch_rectangle`, `nx_sketch_circle`, `nx_sketch_arc`, `nx_finish_sketch` |
| 拉伸 | `nx_extrude`（create / unite / subtract） |
| 孔 | `nx_hole`, `nx_counterbore_hole`, `nx_countersink_hole` |
| 特征 | `nx_unite`, `nx_revolve`, `nx_mirror`, `nx_linear_pattern`, `nx_circular_pattern`, `nx_shell` |
| 边处理 | `nx_edge_blend`, `nx_chamfer` |
| 视图 / 恢复 | `nx_undo`, `nx_fit_view`, `nx_release` |

---

## 安装 — 一体化一键流程

### 环境要求

- Windows 10/11
- 已安装 Siemens NX（当前在 **NX 2506** 上完成真机验证）
- Python 3.10+
- 已安装并至少启动过一次 Agent 客户端

克隆仓库后，在仓库根目录执行一条命令：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1
```

安装器会自动完成：

1. 创建 Python 虚拟环境并安装 NX_MCP-Enhanced sidecar
2. 创建 Workspace
3. 为当前 Windows 用户配置 `UGII_USER_DIR`
4. 构建 C# Loader
5. 将 Loader 部署到 `%UGII_USER_DIR%\startup`
6. 安装 NX Modeling Skill（文字描述建模）
7. 安装 Drawing Reader Skill
8. 安装 Modeling Planner Skill
9. 安装 Pipeline Skill
10. 安装 Plan Runner 并执行轻量测试

如果存在多个 Agent Profile，可手动指定：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 -AgentProfile "Profile 7"
```

> 注：`-AgentProfile` 是当前安装脚本中的兼容参数名，仅用于指定本机 Profile。

如果安装前 Siemens NX 已经处于运行状态，安装完成后需要重启一次 NX，
让新进程读取 `UGII_USER_DIR` 并自动加载新部署的 Loader。
如果 NX 尚未启动，安装完成后直接启动即可。

安装完成并启动 / 重启 NX 后，有两种建模方式：

### 方式一：文字描述建模

新建一个 Agent 对话，直接描述你希望创建或修改的 NX 模型，例如：

```
用我打开的 NX 创建一个 100×60×10 mm 的底板，四角 R8，
并在四角各打一个 Ø8 通孔。
```

文字建模由 `nx-modeling` Skill 调用现有 NX_MCP 工具完成。

### 方式二：二维工程图自动建模

新建一个 Agent 对话，上传二维机械工程图，并发送：

```
开始建模
```

工程图模式会自动执行 Drawing Reader → Modeling Planner → Plan Runner。

完整安装说明请查看 [INSTALL.md](INSTALL.md)。

---

## 路径自动检测

| 项目 | 检测 / 使用顺序 |
| --- | --- |
| NX 安装目录（`UGII_BASE_DIR`） | `UGII_BASE_DIR` 环境变量 → 注册表 → `PATH` → `%ProgramFiles%\Siemens\NX*` |
| Workspace（`NX_MCP_WORKSPACE`） | 环境变量 → `%USERPROFILE%\NX_MCP_WORKSPACE` |
| NX 用户目录（`UGII_USER_DIR`） | 优先使用已有环境变量；未设置时自动配置为 `%USERPROFILE%\.nx_mcp_user` |
| Loader 启动目录 | `%UGII_USER_DIR%\startup` |

---

## 当前限制

- 暂不支持真实螺纹 / 螺纹孔几何。
- Sweep、Loft、Draft、Spline、渐开线齿轮及复杂自由曲面不在当前认证范围内。
- 当前真机认证环境为 **NX 2506 / Windows**；其他 NX 版本可能可用，但尚未正式验证。
- STEP 导出后，NX Translator 可能需要短暂时间完成文件写入。

---

## 两种建模入口

Agent Pack 已经**内置在本仓库中，并由 `install.ps1` 自动安装**，同时支持文字描述建模和二维工程图自动建模，包含：

- `nx-modeling`：根据文字描述直接创建或修改 NX 模型

- `nx-engineering-drawing-reader`：二维机械工程图 → 结构化 JSON
- `nx-mcp-modeling-planner`：结构化 JSON → 可执行建模计划
- `nx-mcp-pipeline`：一条指令完成 A → B → C 全流程编排
- `nx-mcp-plan-runner`：在 NX 中确定性执行建模计划

详细使用说明：[docs/DRAWING_TO_NX.md](docs/DRAWING_TO_NX.md)

NX_MCP 核心仍可独立使用；`install-agent.ps1` 保留为高级工具，
用于只重装 Agent Pack，而不重新安装 NX_MCP 核心。

---

## 致谢

- 原始项目：DreamEnding/NX_MCP
- 增强版 / 一体化 Agent Pack：NX MCP contributors

## 许可证

MIT License，详见 [LICENSE](LICENSE)。

原始项目内容继续遵循其自身的 MIT License。
