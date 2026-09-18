---
name: nx-engineering-drawing-reader
description: 作者：抖音 无趣。用于把二维机械工程图快速转换为三维 CAD 建模所需的结构化 JSON；一次提取总尺寸、厚度、孔、沉孔、沉头孔、PCD、圆角、倒角、数量、对称、镜像和阵列信息，禁止像素测量和按比例猜尺寸。
---

# NX 工程图读取器

## 1. 作用

本 Skill 用于把二维机械工程图快速转换为可供三维 CAD 建模使用的结构化 JSON。

只提取**会改变最终三维几何结果**的信息；制造、行政或与三维形状无关的内容主动忽略。

核心原则：

- 清晰标注直接采信。
- 禁止根据像素、轮廓比例或图纸比例反推尺寸。
- 优先一次整图读取，不为非关键内容反复裁剪、放大或 OCR。
- 最终只输出一份结构化 JSON。

## 2. 角色与目标

按**机械 CAD 建模工程师**的方式读取图纸：

1. 先观察整张图，识别各视图。
2. 读取所有会影响三维几何的印刷标注。
3. 把多个视图中重复表达的同一特征合并。
4. 只使用图纸明确给出的数值执行尺寸闭合检查。
5. 输出唯一的一份结构化 JSON。

速度、结构化和建模可执行性优先于制造质量报告的完整度。

## 3. 读取流程

### 3.1 整图观察

1. 在局部放大前，先完整观察整张工程图一次。
2. 识别存在的视图：主视图、俯视图、侧视图、剖视图、局部放大图。
3. 第一轮尽可能读取所有清晰的尺寸和标注；清晰印刷数值直接采信，不重新测量。
4. 记录每条标注来自哪个视图，便于后续跨视图合并。

### 3.2 只提取建模信息

只提取会影响三维模型的信息，包括：

- 总长、总宽、总高
- 厚度、壳体壁厚
- 线性尺寸、中心距
- Ø 直径、R 圆角、C 倒角
- 通孔、沉孔、沉头孔
- 孔位、PCD 分布圆
- 数量（2×、4×、6×……）
- 对称、镜像、线性阵列、矩形阵列、圆周阵列

### 3.3 默认忽略

以下内容默认跳过，不做额外分析：

- 标题栏
- 材料
- 表面粗糙度
- 普通技术要求
- 加工工艺说明
- 不会改变三维几何结果的 GD&T

## 4. 严格规则

1. 图纸已有清晰数值标注时，**禁止**使用像素比例、轮廓测量、Hough、OpenCV 或其它方式重新估算。
2. **禁止按图纸比例猜尺寸。**
3. 禁止为了确认非关键内容反复裁剪、放大、OCR 或循环检查。
4. 同一几何特征在多个视图中出现相同尺寸/标注时，视为**交叉确认**：
   - 合并为一个 feature；
   - `source_views` 记录全部来源视图；
   - 提升 `confidence`；
   - 不得因为重复标注产生 `unresolved`。
   例如同一个凸台顶外圆 `C2×45°` 在两个视图出现，只输出一个 chamfer feature。
5. 图纸未定义某项信息，但缺失该项**不会影响唯一三维建模结果**时：
   - 对应字段设为 `null`；
   - `required_for_modeling: false`；
   - 停止继续分析。
   例外：圆周阵列方向。如果图纸通过中心线、对称线等明确表达方向，必须输出该方向。
6. 只有真正影响建模、且确实无法读取的信息才进入 `unresolved`。
7. 尺寸闭合只允许使用图纸已经印刷的数值；禁止为了闭合而补尺寸。
8. 禁止额外生成 bbox 标注图、HTML、图例、质量报告或其它文档。
9. 最终输出只有一个结构化 JSON。

## 5. 固定输出坐标系

所有零件统一使用以下坐标系，并在输出前把孔中心、凸台位置和其它特征坐标全部转换进去：

- XY 原点 = 零件整体外形中心
- Z = 0 = 零件底面
- +X = 向右
- +Y = 俯视图向上
- +Z = 向上

最终 JSON 必须包含：

```json
"coordinate_system": {
  "origin": "part_center_xy_bottom_z0",
  "x_positive": "right",
  "y_positive": "up",
  "z_positive": "up",
  "unit": "mm"
}
```

禁止把原点定义留给下游建模阶段猜测。

示例：160×100 底板，四个孔中心距四周边缘均为 20 mm，则四个孔中心必须输出为：

```text
[-60,-30] [-60,30] [60,-30] [60,30]
```

## 6. 尺寸闭合检查

只验证图纸已给数值之间是否自洽，例如：

- 总高 = 底板厚度 + 凸台高度
- 总宽 = 已标注的各宽度链之和
- 孔中心距、对称关系、数量关系是否互相一致

`dimension_closure.status` 只允许：

- `"closed"`：已给数值足够且互相一致
- `"incomplete"`：已给数值不足以确认闭合
- `"conflict"`：图纸已给数值互相矛盾

禁止自行添加尺寸来把状态变成 `closed`。

## 7. 输出结构

最终结果必须是一个 ```json ... ``` 包裹的 JSON 对象：

```json
{
  "views": ["主视图", "俯视图", "剖视图 A-A"],
  "overall_dimensions": {},
  "coordinate_system": {
    "origin": "part_center_xy_bottom_z0",
    "x_positive": "right",
    "y_positive": "up",
    "z_positive": "up",
    "unit": "mm"
  },
  "features": [],
  "patterns": [],
  "symmetry": [],
  "unresolved": [],
  "dimension_closure": {
    "status": "closed | incomplete | conflict"
  }
}
```

每个 feature 建议包含：

```json
{
  "name": "",
  "type": "",
  "dimensions": {},
  "position": {},
  "count": 1,
  "source_views": [],
  "confidence": "high | medium | low",
  "required_for_modeling": true
}
```

每个 pattern 建议包含：

```json
{
  "type": "linear | rectangular | circular",
  "feature": "",
  "count": 1,
  "spacing": null,
  "pcd": null,
  "angle": null,
  "start_angle": null,
  "required_for_modeling": true
}
```

### 7.1 二维阵列语义（强制）

一个孔组如果同时在 X 和 Y 两个方向存在间距，禁止写成单一 `linear` pattern。

规则矩形阵列应输出：

```json
{
  "type": "rectangular",
  "count_x": 2,
  "count_y": 2,
  "spacing_x": 120,
  "spacing_y": 60
}
```

如果无法明确判断阵列类型，保留 `explicit_centers` 的明确坐标，不强行分类。

### 7.2 圆周阵列方向（强制）

除了 `count` / `pcd` / `angle`，必须确定阵列相对零件坐标系的方向。

如果图纸通过中心线、对称线或明确几何关系表达孔位方向，应直接读取这种对齐关系；这不属于比例测量，也不属于尺寸猜测。

例如：4×Ø6.6，PCD Ø44，孔位落在 X/Y 中心线上：

```json
{
  "type": "circular",
  "feature": "凸台 PCD 孔（4×Ø6.6）",
  "count": 4,
  "pcd": 44,
  "angle": 90,
  "start_angle_deg": 0,
  "angle_reference": "+X axis",
  "explicit_centers": [[22, 0], [0, 22], [-22, 0], [0, -22]],
  "required_for_modeling": true
}
```

规则：

1. 图纸明确把孔与中心线/基准方向对齐时，必须输出方向或 `explicit_centers`。
2. 不得因为图上没有单独标注“0°”，就丢弃已经明确表达的中心线对齐关系。
3. 只有图纸确实没有表达阵列旋转方向，且方向不会影响最终模型时，`start_angle_deg` 才允许为 `null`。
4. 如果阵列旋转方向会改变最终三维模型，则 `required_for_modeling` 必须为 `true`。
5. 禁止根据像素距离或比例计算角度；只能读取明确的中心线、对称线和水平/垂直几何关系。

## 8. 复杂轮廓与 DETAIL / SECTION 规则

### 8.1 复杂轮廓完整性门禁

当主视图表达一个**连续外轮廓**时，禁止用包络基本图元（例如“矩形 + 圆”）替代真实轮廓。

输出必须足以唯一重建：

- 直线段
- 圆弧（圆心、半径、起止角）
- 圆角 / blend（R）
- 斜边（角度、参考方向）
- 相切关系
- 端点 / 交点 / 切点位置

任一轮廓段无法唯一确定时，必须加入 `unresolved`，并设置 `required_for_modeling: true`。禁止自动把复杂 profile 简化成基本图元。

### 8.2 DETAIL / SECTION 优先

DETAIL 和 SECTION 是局部几何的高优先级证据。

当总览视图与 DETAIL / SECTION 描述同一位置时：

- DETAIL / SECTION 用于确定局部轮廓、孔位、深度、厚度、圆角/倒角和局部切除；
- 禁止忽略 DETAIL / SECTION，再根据主视图比例猜局部结构。

### 8.3 跨视图拓扑一致性

在把 `dimension_closure.status` 设为 `"closed"` 之前，必须确认：

- 每个孔中心都位于最终实体材料区域内；
- 每个切除不会把其它视图明确确认存在的孔/凸台完全删除；
- 主视图轮廓、DETAIL 和 SECTION 之间没有拓扑矛盾；
- 任何矛盾都必须加入 `unresolved`，状态只能是 `"conflict"` 或 `"incomplete"`，禁止带着矛盾输出 `"closed"`。

### 8.4 Profile-First 输出结构

对 Profile-First 零件，JSON 必须包含 `profile`：

```json
"profile": {
  "plane": "XY",
  "closed": true,
  "segments": [
    {"id": "s1", "type": "line", "start": [x1, y1], "end": [x2, y2]},
    {"id": "s2", "type": "arc", "center": [cx, cy], "radius": r, "start_angle_deg": a1, "end_angle_deg": a2, "clockwise": false, "tangent_to": "s1"},
    {"id": "s3", "type": "fillet", "radius": r, "at": "corner_label", "connects": ["s1", "s2"]}
  ]
}
```

每个 segment 必须提供足够信息用于重建。如果印刷标注不足以确定精确坐标，则把该段设为建模必需，并加入 `unresolved`。

## 9. 性能规则

1. 默认整张图只完整观察一次。
2. 第一轮必须尽可能提取全部建模信息。
3. 禁止针对每个尺寸反复裁剪或确认。
4. 文本清晰可读时直接信任标注，禁止像素测量。
5. 只有某个局部确实影响唯一建模结果、且第一轮无法读取时，才允许重新查看该局部。
6. 第二个视图确认同一特征尺寸时，只做交叉确认，不从头重新解析。
7. 不做图片美化、重绘、HTML 输出或 bbox 可视化。
8. 不做与三维建模无关的制造质量分析。
9. 优先保证速度和结构化结果，不追求与建模无关的 CTQ 报告完整度。

## 10. 参考

快速识别视图布局、标注符号、阵列和尺寸闭合规则见：

`references/nx-drawing-rules.md`

完整输出示例见：

`examples/example-output.json`
