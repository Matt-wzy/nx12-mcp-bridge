# 二维工程图读取模块

本文件是 `nx-agent` 的内部工程图解析规则，不是独立 Skill。

## 1. 目标

把二维机械工程图一次性转换为三维 CAD 建模所需的结构化 JSON。只提取会改变最终三维几何结果的信息。

核心原则：
- 清晰标注直接采信。
- 禁止根据像素、轮廓比例或图纸比例反推尺寸。
- **整图视觉读取优先，禁止 OCR 驱动的逐区扫描。** 对清晰工程图不得为了“提高置信度”
  使用 Python / PIL / OpenCV 反复裁剪、放大、OCR 或生成中间图片。
- 第一轮整图读取后，若仍有**单个关键建模标注**确实看不清，只允许最多 **2 个**
  定点局部复核；每个区域最多看 1 次。禁止“裁剪→OCR→再裁剪→再 OCR”的循环。
- 超过上述局部复核预算仍无法确定的关键尺寸，直接加入 `unresolved` 并结束 Gate A；
  **宁可 BLOCKED，也不得继续耗时追图。**
- 多视图重复表达同一特征时合并，不重复计数。
- 最终只输出一份结构化 JSON。

## 2. 读取流程

1. **一次整图读取**：识别主视图、俯视图、侧视图、剖视图、局部放大图，并尽可能读取
   所有影响三维建模的尺寸与符号。
2. 记录每条标注来自哪个视图，跨视图合并同一特征。
3. 只对最多 2 个真正阻塞建模的模糊标注做定点局部复核；不得启动批量 OCR、全图分块、
   多轮裁剪或“为了确认箭头指向”不断放大。
4. 只用图中明确数值进行尺寸闭合。
5. 仍不确定的关键项直接 `unresolved`；不要继续图像处理。
6. 输出结构化 JSON。

**性能门禁（Mode B / Gate A）**：
- 清晰单张工程图的 Gate A 应是一次读图任务，不是图像处理流水线。
- 禁止为了追求“100% 置信度”穷举所有区域。
- 图像辅助操作预算：整图读取之外，最多 2 次定点局部复核。
- 一旦超预算，必须停止继续读图并基于当前证据输出 PASS/BLOCKED。

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
2. 禁止把 OCR 当作主读取方式；禁止使用 Python / PIL / OpenCV 做批量切图、分块放大、
   OCR 补全、锐化/二值化后再 OCR 等循环。
3. 禁止为了让尺寸链闭合而自行补尺寸。
4. 禁止把多个分离特征误合并成一个 feature。
5. 禁止忽略 DETAIL / SECTION 后根据主视图外观猜局部结构。
6. 禁止额外生成 HTML、bbox 标注图、重绘图或制造质量报告。

## 5. 固定坐标系

```json
{
  "origin": "part_center_xy_bottom_z0",
  "x_positive": "right",
  "y_positive": "up",
  "z_positive": "up",
  "unit": "mm"
}
```

XY 原点为零件整体外形中心，Z=0 为零件底面，+X 向右，+Y 为俯视图向上，+Z 向上。禁止把原点留给下游猜测。

## 6. 尺寸闭合

`dimension_closure.status` 只允许：
- `closed`：已给尺寸足够且互相一致
- `incomplete`：缺失尺寸导致不能唯一建模
- `conflict`：图上已给尺寸互相矛盾

禁止为了闭合而补尺寸。

## 7. DETAIL / SECTION 与跨视图一致性

DETAIL / SECTION 是局部几何高优先级证据。出现矛盾必须进入 `unresolved`，不得自行选择一个“看起来更合理”的解释。

输出 `closed` 前必须确认：
- 每个孔中心位于最终材料区域；
- 每个切除不会删除其它视图明确存在的关键特征；
- 主视图、DETAIL、SECTION 的拓扑一致。

## 8. Profile-First 复杂轮廓

如果图纸表达一个连续外轮廓，禁止用“矩形 + 圆”等包络图元替代真实轮廓。JSON 必须提供足以唯一重建的 `profile.segments`：直线端点、圆弧圆心/半径/角度、圆角 R、斜边角度及参考方向、相切/连接关系。任一轮廓段不能唯一确定时，加入 `unresolved`。

## 9. Pattern 语义

二维孔阵列同时存在 X/Y 间距时必须输出 `rectangular`。圆周阵列除 count / PCD / angle 外，还必须输出 `start_angle_deg`、`angle_reference` 或 `explicit_centers`。中心线已经明确方向时可直接输出坐标，这不属于比例测量。

## 10. 输出结构

```json
{
  "views": [],
  "overall_dimensions": {},
  "coordinate_system": {},
  "profile": {},
  "features": [],
  "patterns": [],
  "symmetry": [],
  "unresolved": [],
  "dimension_closure": {"status": "closed"}
}
```

完整示例：`examples/example-output.json`。
快速视图/标注识别规则：`references/nx-drawing-rules.md`。
