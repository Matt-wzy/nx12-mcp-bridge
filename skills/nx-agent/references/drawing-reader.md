---
name: nx-engineering-drawing-reader
description: 作者：抖音 无趣。Use when a 2D mechanical engineering drawing must be converted quickly into structured JSON for 3D CAD modeling (e.g. NX) — extract overall dimensions, thickness, holes, counterbores, countersinks, PCD, fillets, chamfers, counts, symmetry, mirror, and patterns in a single pass, without pixel measurement or scale guessing.
---

# NX Engineering Drawing Reader Skill

## Overview
This skill provides a fast, single-pass procedure for converting 2D mechanical engineering drawings into structured JSON that feeds 3D CAD modeling. It extracts only information that changes the 3D geometry result, deliberately ignores manufacturing/administrative content, and never estimates dimensions from pixels or drawing scale.

---

## Role & Goal
You act as a **Mechanical CAD Modeling Engineer**. In one look at the whole drawing, identify the views, read every printed callout that affects 3D geometry, merge duplicate dimensions across views, run a dimension closure check using only stated values, and emit a single structured JSON. Speed and structured output take priority over manufacturing-report completeness.

---

## Step-by-Step Procedure

### Step 1: Whole-Drawing Observation (one pass)
1. Look at the ENTIRE drawing once before zooming into anything.
2. Identify which views are present: Main view (主视图), Top view (俯视图), Side view (侧视图), Section view (剖视图), Detail view (局部放大图).
3. In that single pass, read every dimension and callout you can see. Trust clear printed text immediately — never re-measure it.
4. Record which view each callout belongs to, for later cross-view merging.

### Step 2: Extract Modeling Features Only
Extract only information that affects the 3D model:
- 总长 overall length, 总宽 overall width, 总高 overall height
- 厚度 thickness, 壳体壁厚 shell wall thickness
- 线性尺寸 linear dimensions, 中心距 center distance
- Ø 直径, R 圆角, C 倒角
- 通孔 through holes, 沉孔 counterbores, 沉头孔 countersinks
- 孔位 hole positions, PCD 分布圆
- 数量 quantities (2×, 4×, 6× …)
- 对称 symmetry, 镜像 mirror, 线性阵列 linear pattern, 矩形阵列 rectangular array, 圆周阵列 circular pattern

### Step 3: Default Ignore List
Skip without analysis:
- 标题栏 title block, 材料 material, 表面粗糙度 surface roughness
- 普通技术要求 general technical notes, 加工工艺说明 process/manufacturing notes
- 与三维几何无关的 GD&T (GD&T that does not affect the 3D geometry result)

### Step 4: Strict Rules
1. When a clear numeric callout exists, NEVER estimate it again via pixel ratio, outline measurement, Hough, OpenCV, or any other method.
2. NEVER guess a dimension from drawing scale (禁止按比例猜尺寸).
3. NEVER repeatedly crop / zoom / OCR / loop to confirm non-critical information.
4. When the same geometric feature shows the same dimension/callout in multiple views (top / main / section), treat it as cross-confirmation: merge it into ONE feature entry, include ALL source views in `source_views`, RAISE its confidence, and NEVER create an `unresolved` entry from cross-view duplication (e.g. one boss-top chamfer `C2×45°` appearing in two views → exactly one chamfer feature). Only when two callouts clearly point to DIFFERENT geometric positions and the target cannot be judged is `unresolved` allowed.
5. Information the drawing does not define, but whose absence does not change the unique 3D modeling result → set the field to `null`, mark `required_for_modeling: false`, and stop analyzing it. Exception — circular-pattern orientation: when the drawing DOES express orientation via centerlines/symmetry lines, it must be output (see "Circular pattern orientation"); `start_angle_deg: null` is allowed only when the drawing expresses no orientation AND the direction does not affect the model.
6. Only information that genuinely affects modeling AND cannot be read is marked `unresolved`.
7. Run ONE dimension closure check, based only on values already printed on the drawing; never invent dimensions to close it.
8. Generate nothing else: no bbox annotation images, no HTML, no legends, no quality reports, no extra documents.
9. The final output is a single structured JSON.

### Step 5: Fixed Output Coordinate System
Use ONE fixed coordinate system for every part, and convert ALL hole centers, boss positions, and feature coordinates into it before output:
- XY origin = center of the part's overall outline (零件整体外形中心)
- Z = 0 = the part's bottom face (零件底面)
- +X = right, +Y = up (top view), +Z = up

The final JSON MUST include:
```json
"coordinate_system": {
  "origin": "part_center_xy_bottom_z0",
  "x_positive": "right",
  "y_positive": "up",
  "z_positive": "up",
  "unit": "mm"
}
```
Never leave the downstream modeling side to guess the origin.

Example: on a 160×100 base plate with hole centers 20 mm from all edges, the four hole centers must be output as `[-60,-30] [-60,30] [60,-30] [60,30]`.

### Step 6: Dimension Closure Check
- Verify stated dimensions are mutually consistent (e.g. 总高 = 底板厚度 + 凸台高度; 总宽 = 2 × 孔中心距 + 直径).
- Use ONLY numbers printed on the drawing. Do not add dimensions.
- Status is one of: `"closed"` (consistent) | `"incomplete"` (stated values insufficient to verify) | `"conflict"` (stated values contradict each other).

### Step 7: Output Schema
Always format the final result as a single JSON object wrapped in ```json ... ```:

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

Every feature SHOULD carry:

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

Every pattern entry:

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

**2D array semantics (strict):** a hole group with spacing in BOTH X and Y must NEVER be written as a single `linear` pattern. For a regular rectangular array output:
```json
{
  "type": "rectangular",
  "count_x": 2,
  "count_y": 2,
  "spacing_x": 120,
  "spacing_y": 60
}
```
If the array form cannot be clearly determined, keep `explicit_centers` with explicit coordinates instead of forcing a classification. `circular` keeps the existing format.

**Circular pattern orientation (strict):** besides `count` / `pcd` / `angle`, the array orientation relative to the part coordinate system MUST be determined. When the drawing expresses hole orientation through centerlines, symmetry lines, or explicit geometric relations, read that alignment directly — this is NOT scale measurement and NOT dimension guessing. Example: 4×Ø6.6 on PCD Ø44 lying on the X/Y centerlines → output:
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
Rules:
1. When the drawing clearly aligns the holes with a centerline / datum direction, MUST output the direction or `explicit_centers`.
2. Do NOT drop a centerline alignment explicitly expressed in the drawing merely because there is no separate "0°" callout.
3. Only when the drawing truly does not express the array rotation direction AND that direction does not affect the model may `start_angle_deg` be `null`.
4. If the array rotation changes the final 3D model, `required_for_modeling` MUST be `true`.
5. Never compute angles from pixel distances or ratios — read only explicit centerlines, symmetry lines, and horizontal/vertical geometric relations.

---

## Complex Contour & Detail Rules (mandatory for profile-first parts)

### Rule 1: Complex Contour Completeness Gate
When the main view expresses a CONTINUOUS outer profile, it is FORBIDDEN to replace it with bounding primitives (e.g. rectangle + circle). The extracted geometry MUST uniquely reconstruct the contour:
- Straight line segments
- Arcs (radius, start/end angles, center)
- Fillets / blends (R values)
- Angled edges (degree, reference)
- Tangency relations
- Endpoint / intersection / tangent point locations

If any segment cannot be uniquely determined, that segment MUST go into `unresolved` with `required_for_modeling: true`. Never auto-simplify a complex profile into primitives.

### Rule 2: DETAIL / SECTION Priority
DETAIL views and section views are HIGH-PRIORITY evidence for local geometry. When overview and DETAIL/SECTION describe the same location:
- DETAIL/SECTION supplies: local contour, hole positions, depths, thicknesses, fillets/chamfers, local cutouts
- Never ignore DETAIL/SECTION and guess local geometry from the main view scale

### Rule 3: Cross-View Topology Consistency Check
Before setting `dimension_closure.status = "closed"`, MUST verify:
- Every hole center lies within the final solid material region
- Every cutout does NOT remove a hole/boss that another view explicitly confirms exists
- Main view contour, DETAIL, and SECTION do not contradict each other
- Any contradiction goes into `unresolved` and status MUST be `"conflict"` or `"incomplete"` — never `"closed"` with unresolved conflicts

### Rule 4: Profile-First Output Schema
For profile-first parts, JSON MUST include a `profile` object describing the continuous outer contour:
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
Each segment must carry enough to reconstruct it. If exact segment coordinates cannot be determined from printed callouts, mark `required_for_modeling: true` + add to `unresolved`.

---

## Performance Rules
1. Default: view the whole drawing exactly once.
2. The first round MUST capture all modeling information possible.
3. No per-dimension repeated cropping or confirmation.
4. When text is clearly legible, trust the callout — do not perform pixel measurement.
5. Only re-view a local region when it affects the unique modeling result AND genuinely cannot be read.
6. When a second view confirms the same feature dimension, treat it as cross-confirmation — do not re-parse from scratch.
7. No image beautification, redraw, HTML output, or bbox visualization.
8. No manufacturing-quality analysis unrelated to 3D modeling.
9. Prioritize speed and structured results; do not chase CTQ-report completeness.

---

## Reference
See `references/nx-drawing-rules.md` for quick recognition rules of view layout, callout symbols, patterns, and dimension closure. See `examples/example-output.json` for a complete worked example.
