# 用户可见输出规范（全部中文）

> 用户可见输出的**唯一权威模板**。所有对用户的汇报都必须按本文件输出。
> 内部记录的英文键（`status`、`elapsed_seconds`、`operations`、`failed_step`、
> `body_count`、`model_bbox` 等）**严禁**原样展示给用户，必须翻译成自然中文。

## 1. 成功输出（唯一模板）

```
状态：成功
图纸解析耗时：XX 秒
建模规划耗时：XX 秒
NX 建模耗时：XX 秒
总耗时：XX 秒
最终实体数量：1
模型尺寸：XXX × XXX × XXX mm
PRT 文件：
<PRT 文件路径>
STEP 文件：
<STEP 文件路径>
```

字段来源：
- 图纸解析耗时 ← `A_ELAPSED_SECONDS`
- 建模规划耗时 ← `B_PLANNER_SECONDS` + `B_BUILD_SECONDS`（两值单位均为秒；若 runner build 原始为毫秒，先换算 `B_BUILD_SECONDS = build_ms / 1000`，**禁止秒与毫秒直接相加**）
- NX 建模耗时 ← `C_RUNNER_SECONDS`
- 总耗时 ← `total_elapsed_seconds`（**真实墙钟时间**：从接收到"开始建模"并正式开始执行，到 C Runner 完成返回最终报告为止；不得用 A+B+C 内部耗时之和代替）
- 最终实体数量 ← `BODY_COUNT`
- 模型尺寸 ← 由 `MODEL_BBOX`（min/max 结构）计算：长度 = `x_max−x_min`，宽度 = `y_max−y_min`，高度 = `z_max−z_min`（单位 mm）。例如 `x_min=-90,x_max=90,y_min=-55,y_max=55,z_min=0,z_max=36` → 输出 `180 × 110 × 36 mm`。禁止输出 `-90..90 × -55..55 × 0..36` 这类区间表示
- PRT 文件 ← `PRT_PATH`
- STEP 文件 ← `STEP_PATH`

## 2. 失败输出（按阶段）

### 2.1 A 失败（图纸解析）

```
状态：失败
失败阶段：图纸解析
失败原因：……
```

（如需要用户补充信息，追加一行 `需要确认：……`）

### 2.2 B 失败（建模规划）

```
状态：失败
失败阶段：建模规划
失败原因：……
```

### 2.3 C 失败（NX 建模）

```
状态：失败
失败阶段：NX 建模
失败步骤：……
失败原因：……
```

## 3. 失败后行为

- 失败后**立即结束**，不得自动跨阶段修复。
- 不得输出英文键名，不得附上原始 JSON 状态文件内容。

## 4. 禁止输出的英文键

以下键名及其值不得以英文形式出现在任何用户可见输出中：
`status`、`elapsed_seconds`、`operations`、`failed_step`、`body_count`、
`model_bbox`、`prt_path`、`step_path`、`pipeline_status`、`current_stage`、
`input_image`、`stage_a`、`stage_b`、`stage_c`、`total_elapsed_seconds`。

## 5. 内部记录与用户输出的对应关系

| 内部记录（英文，仅内部） | 用户可见中文 |
|---|---|
| `A_ELAPSED_SECONDS` | 图纸解析耗时 |
| `B_PLANNER_SECONDS` + `B_BUILD_SECONDS` | 建模规划耗时（两值均以秒计；build 原始毫秒先除以 1000） |
| `C_RUNNER_SECONDS` | NX 建模耗时 |
| `total_elapsed_seconds` | 总耗时（真实墙钟时间，非 A+B+C 之和） |
| `BODY_COUNT` | 最终实体数量 |
| `MODEL_BBOX` | 模型尺寸（由 min/max 差值计算，如 180 × 110 × 36 mm） |
| `PRT_PATH` | PRT 文件 |
| `STEP_PATH` | STEP 文件 |
| `failed_step` | 失败步骤 |
| `status` / `check` 等 | 状态 / 校验结果（翻译为中文词） |
