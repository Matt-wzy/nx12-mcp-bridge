# 用户可见输出规范（全部中文）

> 用户可见输出的唯一权威模板。内部英文键不得原样展示。

## 0. 状态权威来源

- 阶段 C 的最终状态以 **Runner report 的 `status`** 为唯一权威来源。
- 只有 `report.status == "success"` 才允许向用户输出“状态：成功”。
- 若 `report.status == "failed"` 或存在 `failed_step`，即使 PRT/STEP 已经生成、模型肉眼看起来正确，也必须按“失败”汇报；文件存在不能覆盖 Runner 失败状态。
- 首次失败只有在符合 Controlled Self-Healing 且第二次 Runner report 明确 `status == "success"` 后，才允许输出“成功（自动修复后）”。
- 禁止把“建模动作已完成但最终验证失败”包装成成功。


## 1. 一次通过成功

```
状态：成功
图纸解析耗时：XX 秒
建模规划耗时：XX 秒
NX 建模耗时：XX 秒
自动修复次数：0
总耗时：XX 秒
最终实体数量：1
模型尺寸：XXX × XXX × XXX mm
PRT 文件：
<路径>
STEP 文件：
<路径>
```

## 2. 自动修复后成功

```
状态：成功（自动修复后）
首次失败步骤：第 N 步
首次失败原因：……
自动修复：已完成 1 次
修复内容：……
图纸解析耗时：XX 秒
建模规划耗时：XX 秒
NX 建模耗时：XX 秒
总耗时：XX 秒
最终实体数量：1
模型尺寸：XXX × XXX × XXX mm
PRT 文件：
<路径>
STEP 文件：
<路径>
```

不得把“自动修复后成功”伪装成“一次通过”。

## 3. 最终失败

A/B 失败：
```
状态：失败
失败阶段：图纸解析 / 建模规划
失败原因：……
```

B 阶段若因能力边界失败：
- 明确写“失败阶段：建模规划”，不得进入 Runner；
- 说明哪个 required feature 当前 32 个 certified tools 无法等价建模；
- 禁止把 unsupported feature 描述成“已由普通孔/其它特征等价表达”；
- 输入 surrogate 或机器参数化 resolver 都不可用时，必须等待输入补充后才可重新规划。

使用机器 thread surrogate 成功时，最终结果必须列出 nominal diameter、pitch、pitch source、统一计算方法与 surrogate diameter，并明确未生成真实螺纹牙型。

C 首次失败且不可修复：
```
状态：失败
失败阶段：NX 建模
失败步骤：……
失败原因：……
自动修复次数：0
```

C 修复后再次失败：
```
状态：失败
失败阶段：NX 建模
首次失败步骤：……
首次失败原因：……
自动修复次数：1
第二次失败步骤：……
第二次失败原因：……
```

## 4. 耗时口径

- 图纸解析耗时 ← A 阶段真实耗时
- 建模规划耗时 ← B Planner + build/check；若发生 repair plan，修复规划/build/check 时间也计入
- NX 建模耗时 ← 各 Runner report 的 `nx_modeling_elapsed` 之和，只表示几何建模 operations
- Runner 总耗时 ← 各 Runner report 的 `total_runner_elapsed` 之和；包含 preflight、保存、STEP 导出/落盘等待和最终验证
- 最终验证耗时 ← `validation_ops_elapsed`；不得把 STEP export settle 算成验证耗时
- 总耗时 ← **真实墙钟时间**：从“开始建模”正式执行，到最终成功/失败报告返回
- 若发生自动修复，总耗时必须包含：首次失败、诊断、repair plan、build/check、失败零件清理、第二次执行、验证和汇报
- 禁止只报告最终成功那一轮的耗时

## 5. 模型与文件字段

- 最终实体数量 ← BODY_COUNT
- 模型尺寸 ← MODEL_BBOX 的 max-min 差值
- PRT 文件 ← PRT_PATH
- STEP 文件 ← STEP_PATH

## 6. 禁止行为

- 不得输出原始内部 JSON 状态文件
- 不得展示长 plan
- 不得把内部英文键名直接展示给用户
- 不得把 repair 后的成功描述为 0 次失败

## 工程图 Gate A 用户输出

- Gate A BLOCKED 时，只列出 `required_for_modeling=true` 的 blocking unresolved，并只询问继续建模所需的**最少问题**。
- `required_for_modeling=false` 的粗糙度、普通工艺说明、非建模字段等只可作为简短 warning；不得混入“必须确认”的问题列表。
- 能 deterministic derived 的尺寸直接使用，并可在需要时简短注明算式；不得要求用户再次确认已经唯一推导出的值。
- Gate A PASS 且只有 soft warnings 时，继续 Planner，不得仅因 warning 停止。
