# 二维工程图读取模块

本文件是 `nx-agent` 的内部工程图解析规则，不是独立 Skill。

## 1. 目标

把二维机械工程图一次性转换为三维 CAD 建模所需的结构化 JSON。只提取会改变最终三维几何结果的信息。

核心原则：
- 清晰标注直接采信。
- 禁止根据像素、轮廓比例或图纸比例反推尺寸。
- 优先整图一次读取，禁止为非关键内容反复 OCR / 裁剪 / 放大。
- 多视图重复表达同一特征时合并，不重复计数。
- 最终只输出一份结构化 JSON。

## 2. 读取流程

1. 先观察整张图，识别主视图、俯视图、侧视图、剖视图、局部放大图。
2. 第一轮尽可能读取所有会影响三维建模的尺寸与符号。
3. 记录每条标注来自哪个视图。
4. 跨视图合并同一特征。
5. 只用图中明确数值进行尺寸闭合。
6. 输出结构化 JSON。

## 3. 必须提取

- 总长、总宽、总高、板厚、壳体壁厚
- 线性尺寸、中心距
- Ø 直径、R 圆角、C 倒角
- 通孔、Counterbore、Countersink
- 孔中心、PCD、数量
- 对称、镜像、linear / rectangular / circular pattern
- Shell 开口面和壁厚
- Profile-First 所需的连续轮廓段、圆弧、斜线、圆角、DETAIL / SECTION

## 4. 禁止事项

1. 禁止像素测量、OpenCV/Hough 比例估算或按图纸比例猜尺寸。
2. 禁止为了让尺寸链闭合而自行补尺寸。
3. 禁止把多个分离特征误合并成一个 feature。
4. 禁止忽略 DETAIL / SECTION 后根据主视图外观猜局部结构。
5. 禁止额外生成 HTML、bbox 标注图、重绘图或制造质量报告。

## 5. 固定坐标系与正投影视图方向

```json
{
  "origin": "part_center_xy_bottom_z0",
  "x_positive": "right",
  "y_positive": "up",
  "z_positive": "up",
  "unit": "mm"
}
```

XY 原点为零件整体外形中心，Z=0 为零件底面，+X 向右、+Y 为俯视图向上、+Z 向上。禁止把原点留给下游猜测。

在该固定坐标系下，标准正投影视图与全局轴固定对应：
- 正视图 / Front：位于 **XZ** 平面，视图法向轴为 **Y**；
- 侧视图 / Side：位于 **YZ** 平面，视图法向轴为 **X**；
- 俯视图 / Top：位于 **XY** 平面，视图法向轴为 **Z**。

左右侧视图只改变观察正负方向，不改变“法向轴 = X”的轴语义。第一角/第三角投影只改变视图在图纸上的排布，不改变上述平面/法向轴对应。

方向读取硬规则：
1. 某孔/沉孔/圆柱特征在一个已确定方向的正投影视图中显示为圆时，孔轴/圆柱轴 = 该视图法向轴；若其它视图明确表达不同轴向则进入 `unresolved`/conflict，不得自行选择。
2. 槽/开缝/切口在某视图中显示为两条平行轮廓线时，两线间的明确尺寸只定义该视图平面内的 **width_axis**；**不能由这两条线直接推断 through_axis**。
3. 对 slot/cut 必须分别记录 `width_axis` 与 `through_axis`。若贯穿方向会改变三维结果而图纸不能唯一确定，必须 `unresolved`。
4. DETAIL / 局部放大图若明确由某母视图引出，则继承母视图的平面/法向；若无法确认母视图方向，不得只凭局部图朝向推断全局轴。

## 6. 尺寸归属与特征语义

- **明确箭头/引线优先绑定其实际指向的几何特征。** 同一区域出现其它孤立数值、表格字段或邻近标注时，禁止因为“位置接近”就覆盖明确箭头标注。
- 对开缝/槽：直接跨两侧边界的线性尺寸才可作为 `width`；例如两条槽边之间明确标注 `2`，则槽宽就是 2。附近未通过尺寸线/引线绑定到该槽的 `1.6` 不得替代它。
- `depth` 只能来自明确的深度语义（如 `深12` / `DEPTH 12`）、剖视图中明确的起止面，或其它能够唯一限定切除深度的标注。**普通线性位置尺寸不得因为数值合适就被改解释成 slot/cut depth。**
- 因此像 `E=18` 这类线性/位置参数，除非图纸明确把它绑定为槽深，否则只能保留其原始位置尺寸语义，不能自动写成 `slot.depth=18`。
- 孔类 feature 必须输出 `axis`；slot/cut 必须输出 `width_axis`、`through_axis`（若非贯穿则再输出有明确证据的 `depth`）。任何会改变三维结果的方向字段不能唯一确定时，Gate A 不得 closed。
- **同轴复合孔必须先归组，再输出特征**：当通孔、沉孔、盲孔、螺纹孔等标注满足“同一 `axis` + 同一横向中心线坐标 + 图纸有明确共中心线/同心/同一轴线证据”时，输出一个 `type:"coaxial_hole_group"` 的 feature，成员放入 `members`，不得把成员拆成不同中心位置的独立孔。成员可以有不同直径、深度、轴向起止侧或加工语义，但必须共享同一个 `centerline`。
- 同轴归组的证据必须来自中心线、同心圆、跨视图投影对应、明确中心距链或等价确定性关系；**仅仅 axis 相同、数值接近或位于同一区域不足以归组**。证据不足且归组与否会改变实体时，进入 blocking unresolved。
- 对同轴组，位置尺寸（例如两条轴线之间的 `E`）绑定到**组 centerline**，不能只绑定到其中一个 member 后再给其它 member 另猜中心高度。若主孔中心高为 40、夹紧轴与主孔中心明确相距 18，则该夹紧组 centerline 为 58；同组所有 member 继承这一中心线。
- 推荐结构：
  ```json
  {
    "type": "coaxial_hole_group",
    "axis": "X",
    "centerline": {"y": -8, "z": 58},
    "members": [
      {"kind": "threaded_hole", "spec": "M6", "depth": 12, "side": "one_side"},
      {"kind": "through_hole", "diameter": 6.6, "side": "opposite_side"},
      {"kind": "counterbore", "diameter": 11, "depth": 6.5, "side": "opposite_side"}
    ]
  }
  ```
  `side` / 轴向起止范围若会改变实体且无法由图纸唯一确定，仍属于 HARD unresolved；不得用示例中的 side 文本代替真实方向。
- **倒角必须有明确边绑定**：只有图中实际出现并通过尺寸线/引线/局部细节绑定到某条边的 `C2`、`C2×45°` 等，才可输出 chamfer feature。参数表中的字段名 `C` 与数值 `2`（即 `C=2`）不能仅因拼起来像 “C2” 就自动解释为 2 mm 倒角。

## 7. 最低充分建模闭合：HARD / DERIVED / SOFT

工程图读取必须把“不确定信息”按是否影响最终三维实体分层，禁止把所有未识别内容都塞进 blocking `unresolved`。

### 7.1 HARD：阻塞建模
只有会改变最终三维结果的项目才允许 `required_for_modeling=true`，例如：
- 总体/主体尺寸、关键轮廓段；
- 特征位置、数量、直径/半径；
- `axis` / `width_axis` / `through_axis`；
- 是否贯穿；
- 非贯穿特征真正需要的 `depth`；
- 会改变实体的圆角/倒角尺寸。

这些项目若无法由图纸唯一确定 → 写入 `unresolved` 且 `required_for_modeling=true`，Gate A BLOCKED。

### 7.2 DERIVED：可确定推导
若目标值没有直接单独标出，但能仅凭**图中明确尺寸、中心线、相切/共线/对称等明确拓扑关系**通过唯一算式得到，则：
- 写入 `derived`；
- 记录 `value`、`derivation`、`source_dimensions`；
- 视为已闭合，不进入 `unresolved`。

允许示例：
- 已知中心高 `H=40` 和明确中心距 `E=18` → 另一轴线高度 `Z=40+18=58`；
- 已知总高 66、主孔中心高 40、主孔 Ø20，且图形明确开缝止于主孔上切点 → 深度 `66-(40+10)=16`。

禁止把以下情况伪装成 derived：
- 需要像素比例；
- 需要“看起来大概如此”的经验判断；
- 存在两种以上同样合理的解释；
- 需要修改/忽略图上冲突尺寸。

### 7.3 SOFT：不阻塞建模
不影响最终实体唯一性的内容使用 `required_for_modeling=false`，并进入 `warnings` 或 soft unresolved，例如：
- 表面粗糙度；
- 普通公差/材料/热处理/加工顺序说明；
- 未参与实体生成的表格字段；
- 与几何无关且 OCR 不清的文字。

SOFT 项允许保留未知，**不得让 Gate A BLOCKED，也不得向用户强制追问**。

### 7.4 dimension_closure

`dimension_closure.status` 只允许：
- `closed`：所有 `required_for_modeling=true` 的几何已由 explicit 或 derived 唯一确定，且无 dimension conflict；
- `incomplete`：仍有至少 1 个 blocking unresolved；
- `conflict`：图上会影响最终实体的已给尺寸/拓扑互相矛盾。

禁止为了闭合而补尺寸。Gate A 只关心 blocking unresolved，不要求 warnings 为空。

## 8. DETAIL / SECTION 与跨视图一致性

DETAIL / SECTION 是局部几何高优先级证据。出现矛盾必须进入 `unresolved`，不得自行选择一个“看起来更合理”的解释。

输出 `closed` 前必须确认：
- 每个孔中心位于最终材料区域；
- 每个切除不会删除其它视图明确存在的关键特征；
- 主视图、DETAIL、SECTION 的拓扑一致。

## 9. Profile-First 复杂轮廓

如果图纸表达一个连续外轮廓，禁止用“矩形 + 圆”等包络图元替代真实轮廓。JSON 必须提供足以唯一重建的 `profile.segments`：直线端点、圆弧圆心/半径/角度、圆角 R、斜边角度及参考方向、相切/连接关系。任一轮廓段不能唯一确定时，加入 `unresolved`。

## 10. Pattern 语义

二维孔阵列同时存在 X/Y 间距时必须输出 `rectangular`。圆周阵列除 count / PCD / angle 外，还必须输出 `start_angle_deg`、`angle_reference` 或 `explicit_centers`。中心线已经明确方向时可直接输出坐标，这不属于比例测量。

## 11. 输出结构

```json
{
  "views": [],
  "overall_dimensions": {},
  "coordinate_system": {},
  "profile": {},
  "features": [],
  "patterns": [],
  "symmetry": [],
  "derived": [
    {
      "name": "",
      "value": null,
      "derivation": "",
      "source_dimensions": []
    }
  ],
  "unresolved": [
    {
      "item": "",
      "reason": "",
      "required_for_modeling": true
    }
  ],
  "warnings": [],
  "dimension_conflicts": [],
  "dimension_closure": {"status": "closed"}
}
```

约定：
- `blocking_unresolved` = `unresolved` 中 `required_for_modeling=true` 的数量；
- `warnings` 与 `required_for_modeling=false` 的 soft unresolved 不计入 blocking；
- 规格表字段不要求“逐字段解释完成”才能建模；只要最终三维实体所需的尺寸/位置/数量/方向/轮廓已经 explicit 或 derived 唯一确定即可。

完整示例：`examples/example-output.json`。
快速视图/标注识别规则：`references/nx-drawing-rules.md`。
