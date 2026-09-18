---
name: nx-mcp-modeling-planner
description: Use when a complete, unambiguous set of 3D modeling dimensions or structured JSON must be turned into a reliable, pre-planned call sequence for the existing NX_MCP tools (Siemens NX). Plans the full feature order up front (base solid → additive features → unite → shell → pattern/mirror → holes → blends/chamfers → validation → save/STEP), enforces edge/face index freshness, avoids boolean retries and deep STEP audits, and outputs a FAST/DIAGNOSTIC operation plan. Does NOT do image recognition, OCR, or drawing reading; does NOT modify NX_MCP-Enhanced.
---

# NX MCP Modeling Planner

## 1. 适用范围与边界

**输入**：已经明确、完整、无需 OCR、无需推测的三维建模尺寸或结构化 JSON
（必须包含：坐标系约定、全部尺寸与坐标、最终实体要求、输出文件名）。

**输出**：一份可直接执行的 NX_MCP 建模计划 JSON（`mode` + `operations` +
`final_validation` + `fallbacks`），以及执行阶段的硬性规则。

**本 Skill 不做**：
- 图片识别、OCR、工程图读取（由其他 Skill 负责，完成后把结构化 JSON 交给本 Skill）
- 按比例推测、重新计算或修改用户已明确给出的尺寸
- 修改 NX_MCP-Enhanced（冻结 v2.1.1）、C# Loader，或重装环境
- 新增 NX_MCP Tool；只调用现有 32 个 certified 工具（见 `references/nx-mcp-rules.md`）

## 2. 工作流程（先规划，后执行）

1. **解析输入**：提取特征清单，逐项登记（基础实体 / 加料特征 / Shell / Pattern /
   Mirror / 孔类 / 布尔 / 圆角 / 倒角 / 验证 / 输出）。
2. **排定 Feature 顺序**：按 §3 排序，一次定稿。**不允许边做边重新设计建模方案**。
3. **选择路径**：每个特征优先选择当前 NX_MCP 已验证稳定的路径（见规则文档）。
4. **标记拓扑**：为每个操作填写 `topology_changes` / `refresh_edges_after` /
   `refresh_faces_after`（见 §4）。
5. **编写验证与兜底**：默认 FAST 轻量验证；对已识别的风险写 `fallbacks`。
6. **输出计划 JSON**（结构见 §8），示例见 `examples/modeling-plan-example.json`。
7. **按计划执行**：执行阶段遵守 §5–§7 的规则，不得临时更改整体方案；
   失败按 §6 处理，只允许针对原因做一次最小修正。

## 3. 建模顺序规则

### 3.0 连续主轮廓优先（Profile-First Rule，最高优先级）

**当零件主要由某一正视图/侧视图的连续二维外轮廓沿厚度方向拉伸形成时
（典型：厚度基本恒定的支架/托架件），主体必须用单一连续闭合轮廓一次拉伸，
禁止拆成多个局部实体再 Unite。**

1. 优先路径：
   ```
   连续闭合草图（完整外轮廓）
   → 一次 Extrude 形成主体厚度
   → 后续 Cut / Hole / Fillet / Chamfer
   ```
   而不是：
   ```
   多个局部实体 → Unite 形成主体
   ```
2. 若两个局部轮廓/实体之间仅满足：**单点相切 / 单线相切 / 零面积接触**，
   **禁止依赖 Boolean Unite 形成主体**（NX 布尔对零体积接触不可靠，合并后
   工具体可能不并入，后续孔/切除会落在体外，报"工具体完全在目标体外"）。
3. Planner 决定拆分实体前必须判断：
   - 是否存在**真实体积重叠**（面/体相交区域体积 > 0）；
   - 是否存在**共享二维面积**（接触面面积 > 0）；
   - 是否只是**几何相切**（点/线/零面积）；
   - 原工程图是否表达为**一个连续外轮廓**（主视图/侧视图中轮廓线连续闭合）。
   若只是相切且工程图表示连续实体，**必须改为单一 profile 建模**。
4. 恒定厚度支架件参数约定：主轮廓所在平面 = **XY**，厚度方向 = **Z**；
   将整个主轮廓作为闭合草图（直线 + 圆弧组合），一次 `nx_extrude` 创建
   厚度主体；随后依据剖视图做局部减料（extrude subtract）得到局部厚度，
   再开孔，最后 Fillet / Chamfer。
5. 通用规划顺序（此类零件）：
   ```
   主视图完整连续外轮廓（底部轮廓 + 各圆弧 R + 顶部叉形/细节 + R TYP 圆角）
   → 一次拉伸基础厚度
   → 按剖视图局部减料（得到 7/5 等局部厚度关系）
   → 创建孔（Ø 通孔等）
   → 最后 Fillet / Chamfer
   ```
6. **禁止**为了让 Unite 成功而人为增加重叠尺寸（会改变工程图几何）。
   若输入数据（A JSON）未提供完整外轮廓的衔接几何（如过渡圆角、叉形角度、
   剖面减料区域），**只报告数据缺口，不得擅自补尺寸或改输入**；在 plan 的
   notes 中明确标注缺口与影响。
7. 拆分实体建模只允许用于**真实分离且面积接触**的特征（凸台坐落于板面、
   耳板与主体面接触等），且 Unite 前确认接触面积 > 0。

推荐总体顺序（具体任务可调整，但必须优先减少拓扑反复变化）：

```
基础实体
→ 主要加料特征
→ Unite
→ Shell
→ Pattern / Mirror
→ 孔 / 沉孔 / 沉头
→ 再次 Unite / Subtract
→ 圆角
→ 倒角
→ 最终验证
→ Save / STEP
```

细化规则：
- **Shell 必须在被抽壳体还是独立 body 时执行**（抽壳开面、壁厚、底厚才正确），
  之后再做与主体的 Unite。禁止先 Unite 再对合并体 Shell（会把底座也掏空）。
- **Shell 前必须**：`nx_list_faces` → 按 centroid / area / normal / topology
  确认 remove face → `nx_shell`。禁止猜 face index。
- **Pattern/Mirror 类特征**：先创建单个，再 pattern / mirror 生成全部，
  然后**一次性 Unite**（一个 `nx_unite` 传入全部 tool bodies）。
- **所有孔（hole / counterbore / countersink）**放在最后一次大 Boolean 之后、
  圆角倒角之前，集中完成。
- **圆角 / 倒角一律放最后**：完成主体几何 → 完成孔 → 完成 Boolean →
  重新 `nx_list_edges` 选边 → 操作 → 再次 `nx_list_edges` → 下一组。

## 4. 拓扑安全（核心规则）

**所有 edge index / face index 都是临时数据。** 以下任一操作完成后，之前获得的
edge index / face index 一律立即失效：

```
Unite / Subtract / Hole / Counterbore / Countersink / Shell /
Pattern / Mirror / Edge Blend / Chamfer / 任何改变实体拓扑的操作
```

执行硬性规则：
- 需要连续多个边操作时，必须逐次执行：
  `nx_list_edges` → 按几何定位第一个目标 → 操作 →
  再次 `nx_list_edges` → 定位下一个 → 操作。**禁止一次 list_edges 后保存
  多组 index（如 R6、R8、C2）再连续使用。**
- face 操作同理；Shell 前必须重新 `nx_list_faces`。
- 边/面识别优先使用几何信息，**禁止仅根据 index 数字判断**：
  - 边：`curve_type` / `start` / `end` / `midpoint` / `length` /
    `bbox_min` / `bbox_max` / `direction` / `adjacent_faces`
  - 面：`face_type` / `centroid` / `area` / `normal`（仅 planar）/ 邻接边数
  - 这些筛选条件写入计划步骤的 `selection_criteria`（见 §8），**不放入
    `tool_args`**。
- 详见 `references/topology-safety.md`。

## 5. Pattern / Mirror / Boolean 规则

- **Circular Pattern**：`count` 包含原始实体；`angle=360` 时按完整圆均匀分布；
  不重复最后一个实例。
- **Linear Pattern**：`count` 包含原始实体；`spacing` 是相邻实例距离。
- **Pattern 后如需 Unite**：先完成 Pattern，再一次 Unite。
- **Mirror**：对称结构优先创建一侧，再 Mirror；Mirror 后若最终要求单实体，
  再 Unite。
- **Boolean**：减少无意义的多次 Unite；能批量合并时一个 `nx_unite` 传入
  全部 tool bodies，不要一个实体一次 Unite。
- 若两个实体只是理论上刚好接触而导致 Boolean 不稳定，允许使用**不改变最终
  外形尺寸**的微小内部重叠建模方式；最终几何尺寸不能改变。

## 6. 失败处理

一个操作失败后**禁止立刻无脑重复同一调用**。必须先判断错误属于：
- **参数错误** → 修正参数后重试一次；
- **body 选择错误** → `nx_list_bodies` 取新 id 后重试一次；
- **edge/face index 失效** → 重新 `nx_list_edges` / `nx_list_faces` 后操作一次；
- **Boolean 不相交** → 检查几何是否真实相交、该 body 是否已被消费
  （Unite 后 tool body 会消失），做最小修正后重试一次；
- **NX 拓扑改变** → 重新识别后继续；
- **工具当前能力不支持** → 换当前 NX_MCP 已支持的等价建模顺序（仅一次），
  或停止并上报。

只允许针对原因做**一次最小修正**；禁止进入长时间无限自我修复循环。

## 7. 验证：FAST / DIAGNOSTIC

- **FAST（默认，用于视频和正常任务）**：只保留以下项目：
  - **A. Body 数量**：`nx_list_bodies` → 最终 Body 数量 = 1
  - **B. Bounding Box / 极值**：X / Y / Z（底面与顶面由 face centroid 定 Z 范围；
    X/Y 由线性边 bbox 定极值）
  - **C. 少量关键结构确认**：法兰顶部 Z、凸台数量、沉头锥面数量、关键孔数量合理
  - **D. Save 成功**
  - **E. STEP 导出成功且文件非空**
- **面积只作辅助判断，不作为单点失败条件。** 禁止因为单个 face area 与理论值
  存在细微差异直接进入 DIAGNOSTIC（例如 C2 后法兰顶环面 ≈3782.48、Ø6.6 孔后
  凸台顶环面 ≈220.26，这些值随加工顺序变化，只用于人工核对）。
- **只有以下情况才进入 DIAGNOSTIC**：
  - Body 数量错误
  - Bounding Box 明显错误
  - 关键特征缺失（法兰顶 / 凸台 / 沉头锥面 / 关键孔）
  - Save / STEP 失败

**Face Type 语义（Loader 实测契约，见 `references/topology-safety.md`）**：
- 布尔切孔侧面（hole / counterbore / countersink）在 `nx_list_faces` 中
  通常报告为 **`Swept`**，不是 `Cylindrical`；验证孔时 `face_type` 使用
  `["Swept", "Cylindrical"]` 候选，以 `centroid_radius` + `centroid_z` +
  `count` 为主要判据，禁止只凭 `Cylindrical` 判断孔。
- 后续 Loader 版本若改变孔侧面类型，以契约更新为准，**不允许每张 plan
  自行猜 face_type**。

**最终拓扑验证几何（强制）**：FAST 验证几何必须基于最终实体中**真实存在
的材料区间**与最终拓扑，**禁止直接复制建模命令输入参数**（如 hole depth）。
必须考虑：布尔减后哪些面保留、counterbore/countersink 是否截断较小孔侧壁
（Ø9 被 Ø16 沉孔截断后 centroid_z≈9.5 而非 7.0）、hole depth 超过局部材料
高度时侧壁只存在于实际区间（depth=36 但实体仅 Z=0..24 → centroid_z≈12.0
而非 18.0）、后续 Unite/subtract/blend/chamfer 是否改变面范围。
推算：孔侧壁 centroid_z = 该位置实际材料 Z 区间中点；centroid_radius =
孔中心到轴心距离。
- **默认禁止**：
  - 大规模解析 STEP 文本
  - 搜索 AXIS2_PLACEMENT_3D
  - 遍历 STEP 所有圆弧
  - 为已经确认的尺寸再次做深度几何审计
- **DIAGNOSTIC（仅当出现上述失败或用户明确要求深度验证时）**：允许额外
  `nx_list_edges` / `nx_list_faces` / STEP 检查。默认必须使用 FAST。

## 8. 最终建模计划输出结构

```json
{
  "mode": "FAST",
  "operations": [
    {
      "step": 1,
      "goal": "",
      "tool": "",
      "target": "",
      "tool_args": {},
      "selection_criteria": {},
      "expectation": {},
      "topology_changes": true,
      "refresh_edges_after": false,
      "refresh_faces_after": false
    }
  ],
  "final_validation": [],
  "fallbacks": []
}
```

约定：
- `mode`：`"FAST"` 或 `"DIAGNOSTIC"`，默认 `"FAST"`。
- `tool`：必须是 32 个 certified 工具名之一（`references/nx-mcp-rules.md`）。
- **`tool_args` 只能包含该 certified tool 真正支持的参数**（按
  `references/nx-mcp-rules.md` 的参数表逐工具核对）；**禁止把 planner 自定义
  字段（match / expected_count / purpose / checks / expect_extent 等）放入
  `tool_args`**，禁止因自定义字段导致 MCP 参数错误。
- **`selection_criteria`**：Agent 在工具返回结果中**自行筛选**的几何/逻辑条件
  （curve_type / bbox / direction / length / midpoint / centroid / area /
  normal / 数量等）。只用于筛选，**不传给工具**。
- **`expectation`**：只用于判断结果（期望值、容差），**不传给工具**。
- 查询类工具（`nx_list_edges` / `nx_list_faces` / `nx_list_bodies`）：
  `tool_args` 传真实参数（如仅 `body_id`），`selection_criteria` 筛选，
  `expectation` 判定；其他工具一般只有 `tool_args`。
- `target` / `tool_args` 中的 `body_id` / `sketch_id` 使用**逻辑名**
  （如 `body_main`），执行时替换为工具实际返回的 id；`edge_indices` /
  `remove_face_index` 占位符在执行时由 `selection_criteria` 匹配结果填充。
- `topology_changes: true` 的操作必须同时把 refresh 标志置 true（见 §4 清单）。
- `final_validation`：FAST 轻量检查清单（§7 A–E）；`fallbacks`：已识别风险的
  trigger → 一次最小修正方案。

## 9. 尺寸与输入纪律

- **不得修改用户给出的尺寸**。
- 已知尺寸完整时禁止：搜索网络、OCR、根据比例推测、重新计算用户已明确给出的尺寸。

## 10. 性能规则（执行前准备，强制）

**Planner→Runner 接口已冻结，生成 plan 前禁止研究源码。**

1. 正常生成 plan 时**禁止重新读取**：`runner.py`、Runner README、
   `plan_schema.json`、`certified.py`、NX_MCP 源码。
2. 正常情况下**直接使用**本地冻结契约：
   - `references/runner-contract.md`（接口契约，含 schema 版本、字段、引用语法、
     build/check 约定）
   - `references/certified-tool-contract.json`（32 工具参数签名）
3. **只有以下情况才允许重新检查外部文件**：
   - Runner schema 版本发生变化
   - build/check 明确返回 schema incompatibility
   - certified tool contract 版本变化
   - 用户明确要求重新校验接口
4. **禁止**为了"确认没变化"而每次重新读取源代码。

## 11. 版本标识

- 本 Skill 冻结契约版本：
  - `runner_contract_version = "1.0"`（对应 plan_schema.json `schema_version = 1.1`）
  - `certified_tool_contract_version = "1.0"`
- 生成 plan 时在 `notes` 中记录这两个版本号。
- 版本一致 → 直接生成，不做外部源码检查；版本不一致 → 按 §10.3 重新校验接口后
  更新契约并升级版本号。

## 12. 生成流程（FAST 正常路径）

```
读取输入 A JSON
→ 检查 unresolved / dimension closure
→ 读取本地冻结契约（runner-contract.md + certified-tool-contract.json）
→ 生成 FAST plan（含版本号）
→ 调用 runner build
→ 调用 runner check
→ 输出结果
```

**禁止**在正常路径中插入源码研究步骤。

## 13. 输出格式（默认 FAST，强制）

- 成功时最终回复**只允许**输出：

```
status: success
planner_elapsed_s: ...
build_elapsed_ms: ...
check: passed
operations: ...
executable_plan: ...
total_elapsed_s: ...
```

- 失败时最终回复**只允许**输出：

```
status: failed
stage: ...
reason: ...
```

- 禁止：生成长表格、逐项解释、计划摘要、过程复盘、大段自然语言说明。
- 禁止：重复解释已写入 plan JSON 的内容、逐项总结 operation、
  生成"计划设计要点"、为最终回复再次遍历完整 plan。
- build/check 已通过时，**不再进行额外人工抽查**。
- 最终汇报目标耗时 < 10 秒。
