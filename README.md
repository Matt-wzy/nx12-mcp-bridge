# nx12-mcp-bridge

用自然语言（AI）驱动 **Siemens NX 12**：建模 → 出二维图纸 → 标注公差 → 读回校验 → 编制工艺 → 导出 PDF。

本项目是 [`kongbaic/NX_MCP-Enhanced`](https://github.com/kongbaic/NX_MCP-Enhanced)
（作者：抖音 无趣；其原始项目为 [`DreamEnding/NX_MCP`](https://github.com/DreamEnding/NX_MCP)）
的 **NX 12 适配与功能扩展**分支。

---

## 架构

```
AI（自然语言）
  → MCP 服务器（Python sidecar）
  → Windows 命名管道
  → NX 进程内常驻的 C# Loader
  → NXOpen
```

核心设计：**C# Loader 随 NX 启动加载并在整个会话中常驻**，
不必每次执行都跑一遍 journal —— 这是相对"逐条 journal"方案最大的效率优势。

- C# 侧：`loader/NX_MCP_Loader.cs`
- Python 侧：`src/nx_mcp/`（`certified.py` 工具注册、`loader_bridge.py` 命令映射）
- 构建产物：`NX_MCP_Loader.dll`（部署到 NX 的 startup 目录）

---

## 与上游的差异

### NX 12 适配（上游面向更早版本，在 NX 12 上会编译失败）
- 修复 `MirrorBody` 相关 CS1061（改用保留源体的方式）
- 修复 `ShellBuilder` 相关 CS1061（改用 `ReplaceRules`）
- 迁移 `Update.AddToDeleteList` → `AddObjectsToDeleteList`（NX12.0.0 弃用）

### 新增能力（共 68 个 MCP 工具）

| 类别 | 命令 |
|---|---|
| 尺寸标注 | `nx_create_diameter_dimension`（直径，轴类必需）、`nx_create_radius_dimension`（半径）、`nx_set_dimension_value`（中差显示值强制） |
| 草图读取 | `nx_read_sketch`（按草图局部坐标导出全部曲线，支持外部绘制的草图） |
| WAVE 链接 | `nx_create_wave_link`（主模型 → 工序件固定时间戳记/提取几何链接） |
| 图纸文字 | `nx_create_note`（技术要求/工序文字，`|` 换行） |
| 表面粗糙度 | `nx_create_roughness`（写入符号 a1 位） |
| 质量属性 | `nx_mass_properties`（体积/表面积/重心/质量，密度可传，工艺材料定额用） |
| 对称中心线 | `nx_create_centerline`（轴类零件轴线，SymmetricalCenterlineBuilder） |
| DXF/DWG 导出 | `nx_export_dwg`（进程外官方 translator，激光切割/套料用 .dxf） |
| 角度测量 | `nx_measure_angle`（两直线边夹角 + 锐角补角） |
| 剖视图 | `nx_create_section_view`（UFUN CreateSimpleSxview，剖切线 + A-A 箭头 + 剖面线，PDF 实测渲染）+ `nx_add_section_segment`（阶梯剖，追加割段自动转折） |
| 删图纸视图 | `nx_delete_view`（改图前删旧视图；剖视图连带删剖切线） |
| 装配 / BOM | `nx_list_assembly`（装配树拍平 + qty 合计数量 + 跨部件属性）+ `nx_export_bom_csv`（CSV 明细表，含 material/drawing_no 列，Excel 直接开）+ `nx_create_id_symbol`（零件序号球标，与 BOM 配套） |
| 基准符号 | `nx_create_datum_symbol`（GD&T 基准字母框，GB/T 17851，与形状公差标注配套） |
| 形位公差框 | `nx_create_gdt_frame`（GD&T 特征控制框，GB/T 1182：14 种特征中文名直认 + 公差值 + 三基准字母 + Ø 公差带 + Ⓜ/Ⓛ，PDF 实测渲染） |
| 工序卡 | `nx_export_process_card`（OP\<nn\>_字段 属性模板 → 结构化工艺卡片，text/CSV 双格式，缺字段/typo 警告） |
| 全套图纸 PDF | `nx_export_pdf <路径> ALL`（全部图纸页合成一个多页 PDF） |
| 标题栏 | `nx_create_title_block` |
| 部件属性 | `nx_set_attribute` / `nx_list_attributes`（**标题栏内容来源**） |
| 读回校验 | `nx_list_dimensions`（值+公差）、`nx_list_annotations`（NOTE/GDT/ROUGH） |
| 清理 | `nx_delete_annotation`（按 ID/名称或 `note:0`/`dim:1`） |
| 导出 | `nx_export_pdf`（图纸 → PDF） |
| 三维查询 | `nx_measure_distance`（两棱边最小距离，基于 UFUN） |
| 选择回流 | `nx_get_selection`（读取图形区当前选择：用户在 NX 里点面/边/体，AI 拿到 body/face 索引，
面带 made_by 特征归属；反射路由，空选择返回 count=0 事实而非报错） |
| 面溯源 | `nx_face_provenance`（face_index 与 nx_list_faces 同索引空间 → 特征名/类型（made_by），
点选面后 AI 直接定位生成它的特征；owner_count=0 为事实（unite 后所有权丢失）非错误） |

### 稳定性修复
- **"保存 CGM"弹窗把 NX 卡死**：含多张图纸页的部件保存时 NX 弹模态框阻塞，
  管道命令会一直等到超时。已通过在保存前关闭 `SaveOptions.DrawingCgmData` 根治
- **重复打开同一部件**读到陈旧数据：路径分隔符归一化修复

### 构建
- `build.bat` 增加 `NXOpen.UF.dll` 引用（三维测量需要 UFUN）

---

## 环境要求

- Windows
- Siemens NX （需 `NXBIN\managed\NXOpen*.dll`）
- .NET Framework 4.x（构建用 `csc.exe`）
- Python 3.x + MCP 客户端

---

## 安装与运行

```bash
# 1) 构建 Loader
cd loader && build.bat

# 2) 部署 DLL 到 NX 的 startup 目录
#    即 %USERPROFILE%\.nx_mcp_user\startup\NX_MCP_Loader.dll
#    该目录需通过 UGII_USER_DIR 环境变量被 NX 识别

# 3) 启动 NX（需带上述环境变量）
#    从开始菜单直接打开 NX 也可以

# 4) 验证管道
python pipe_smoke.py ping nx_status
```

MCP 配置示例（`~/.workbuddy/mcp.json`）：

```json
{
  "mcpServers": {
    "nx-mcp": {
      "command": "<venv>/Scripts/python.exe",
      "args": ["-m", "nx_mcp.server"],
      "cwd": "<repo>"
    }
  }
}
```

> **注意**：NX 退出后管道即断开。建议用一个后台常驻脚本守护 NX
> （检测到 NX 未运行则拉起）。后台任务通常**不跨会话/跨天存活**，新会话需重新拉起。

---

## 使用要点

1. **边索引在部件实例间不稳定**（重建后会变）→ 绝不硬编码索引，
   先 `nx_list_edges` 再按半径/长度/坐标**自动筛选**
2. **body_id 有两套 ID 空间**：`nx_list_bodies` 显示 NX 日志标识（如 `EXTRUDED(1)`），
   命令只认建模时返回的内部 ID（如 `BODY_1`）
3. **图纸名/视图名不能有空格**（管道按空格分词）
4. **标题栏内容 = 部件属性**：格子引用属性显示，填标题栏要用 `nx_set_attribute`
5. 坐标用 `{"x":..,"y":..}` 字典传，传 list 会报错

---

## 已知限制

以下功能已研究但**未打通**，命令保留在源码中、未注册到 MCP
（避免 AI 用到必失败的工具）：

| 功能 | 状态 |
|---|---|
| 局部放大 | 视图/边界/标签可创建，但放大几何不渲染（UFUN 三条路线已探明，差最后一步） |
| 图框边框线 | `NXOpen.Diagramming` 属图表应用，不适用于制图图纸页 |
| 真实螺纹 | 上游亦不支持 |

~~中心线 / DWG-DXF 导出 / 剖视图~~ 已分别于 2026-11 攻克
（`nx_create_centerline`、`nx_export_dwg`、`nx_create_section_view`）。

上表各项的**详细调研过程**（入口 API、已试组合、排除项、错误原文、下一步建议）
由维护者另行整理，欢迎开 issue 认领其中任意一项——这些是最有价值的待攻克点。

## 维护者档案（不入库）

代码注释与文档中提到的「**受阻项调研档案**」「**经验总结档案**」「**交接清单档案**」
是维护者的本地调研/交接档案，保存在仓库外（已加入 `.gitignore`），**不随仓库分发**。
提及处的章节号指向这些本地档案；需要对应内容的请开 issue 或联系维护者。

---

## 致谢

本项目是在以下开源项目基础上的 NX 12 适配与功能扩展：

- **DreamEnding/NX_MCP** — 原始项目，奠定了 MCP 驱动 NX 的整体思路
  https://github.com/DreamEnding/NX_MCP （MIT License）
- **kongbaic/NX_MCP-Enhanced**（作者：抖音 无趣）— 本项目的直接基础，
  提供常驻 Loader 与命名管道桥
  https://github.com/kongbaic/NX_MCP-Enhanced （MIT License）
- **Cai-aa/CAD-Agent-Hub** — 参考其已验证的 NXOpen 实现序列
  https://github.com/Cai-aa/CAD-Agent-Hub （MIT License）

感谢以上作者的开源贡献。

Siemens NX 是 Siemens 公司的注册商标。本项目仅通过公开的 NXOpen API 与 NX 交互，
**不包含任何 NX 专有代码或二进制文件** —— NX 相关程序集在构建时从本机 NX 安装目录引用。

---

## 许可证

本项目整体采用 **GNU General Public License v3.0**（见 `LICENSE`）。

上游组件以 MIT License 授权，其许可与版权声明保留在 `LICENSES/Upstream-MIT.txt`，
不因本项目采用 GPL 而改变；接收者仍可依据 MIT 条款单独使用那些上游组件。

如需商业授权（不适用 GPLv3 的场景），请联系仓库维护者。
