---
name: nx-modeling
description: 作者：抖音 无趣。用于通过现有 NX_MCP 工具创建或修改原生 Siemens NX 零件，包括草图/拉伸建模、结果检查，以及过期对象引用或执行状态不确定时的安全恢复。不用于任意 Journal 执行或 CAD-to-USD 转换。
---

# NX 建模

使用当前已发现并可用的 NX_MCP 工具完成用户的建模任务。

**工具调用返回成功，并不等于目标模型已经正确完成。** 必须结合对象查询、特征结果和用户要求进行验证。

## 修改零件前

1. 先确认服务器实际提供的工具列表和输入/输出 schema。禁止臆造工具，也不要假设实验性工具可用。先调用 `nx_status`；如果连接不可用，应报告缺少的前置条件，而不是尝试直接附加 NXOpen。只有用户明确要求安装、验收或环境排查时，才读取 [真实 NX 验证指南](../../docs/real-nx-validation.md)。
2. 明确目标零件、需要修改的内容、单位和预期结果。`nx_status` 可以识别活动零件，但不会报告零件单位。对于已有零件，必须从已验证上下文获取单位；如果无法确认，在使用数值尺寸前先询问。禁止默认假设为毫米。
3. 路径必须位于已配置的 `NX_MCP_WORKSPACE` 中。创建新零件时使用新的相对路径，并显式传入 `units`。一次性验收任务要求没有无关活动零件；普通编辑可使用用户明确选中的零件。禁止为了让流程继续而关闭无关零件或覆盖用户文件。
4. 所有修改必须属于用户当前要求。禁止为了绕过能力缺失而开启实验性开关或 Journal 路径。当前支持能力以仓库 README 和 certified tool 契约为准。

## 建模流程

必须使用工具真实返回的对象 ID，禁止猜测对象名称。修改前查询相关对象，并持续确认当前活动零件身份。如果活动零件意外发生变化，立即停止并重新确认用户希望修改哪个零件。

例如，在新零件中创建一个 **20 × 10 × 12.5 mm** 的长方体：

1. 调用 `nx_create_part(path=<新的工作区相对 .prt 路径>, units="mm")`，记录零件 ID。
2. 调用 `nx_list_bodies`，记录初始 body 数量。
3. 调用 `nx_create_sketch(plane="XY")`，把返回的 `object.id` 记录为 `sketch_id`。
4. 调用 `nx_sketch_rectangle(sketch_id, corner1={x: 0, y: 0}, corner2={x: 20, y: 10})`。
5. 先调用 `nx_finish_sketch(sketch_id)`，再调用 `nx_extrude(sketch_id, distance=12.5)`。
6. 查询 bodies / features，确认预期的新 body 和特征确实存在。这类查询用于验证模型结构，不等同于精密尺寸测量。
7. 仅在用户要求时保存或导出 STEP。检查工具返回的导出路径；如果具有文件系统访问能力，再确认文件为新生成且非空。无法验证的项目必须明确说明，禁止假装已经验证通过。

普通建模任务不要照搬验收 Runner 的 undo/cleanup 流程。验收流程可能会先导出实体、再撤销、最后保存 `.prt`；正常建模保存时应保留用户需要的最终模型。

保存和关闭只针对当前工作零件，不代表整个装配树。`nx_close_part` 默认会保存，因此关闭零件时必须显式决定 `save` 参数。

## 扩展建模工具

除草图和拉伸外，已认证工具还包括：

- `nx_sketch_circle(sketch_id, center, diameter)`：在活动草图中创建圆。
- `nx_sketch_arc(sketch_id, center, radius, start_angle, end_angle)`：在活动草图中创建圆弧，角度单位为度。
- `nx_extrude(..., operation="create"|"subtract", target_body_id=...)`：创建实体或执行拉伸切除。
- `nx_hole(body_id, center, diameter, depth, start_offset=0)`：通过圆草图 + Boolean subtract 创建孔。
- `nx_edge_blend(body_id, radius, edge_indices=None)`：对全部或指定边执行圆角；NX 拒绝的边会被跳过并报告。
- `nx_chamfer(body_id, offset, edge_indices=None)`：对全部或指定边执行等距倒角。
- `nx_release()`：释放当前任务状态并恢复正常 NX 交互；resident Loader 保持就绪。

批处理兼容流程 `examples/batch_build_gui.py` 可读取 `batch_task.json`，支持 `rect_extrude`、`hole`、`edge_blend`、`chamfer` 等特征，并输出 `.prt` + `.step`。该路径属于兼容模式，正常交互建模优先使用 resident Loader。

## 恢复与停止条件

- 参数校验错误：先修正输入，禁止原样重复同一请求。
- 执行 undo 或写操作回滚失败后：丢弃缓存对象引用，包括 `nx_status.active_part.id`；重新调用 `nx_status` 并重新查询对象 ID。对象 ID 是会话引用，不是永久资产标识。
- 当 `details.execution_state="unknown"`（例如超时或断开导致状态不确定）时：禁止自动重放写操作。必要时重新连接，查询活动零件和相关对象，再把实际状态与用户目标进行比对。如果查询仍无法消除不确定性，应停止并报告，而不是猜测操作失败。
- `not_started` 本身不代表可以重试。只有已经明确原因、且原因已解决的可重试失败才能再次执行。禁止错误循环。
- 出现 `NX_ROLLBACK_FAILED` 时立即停止写操作。读取 [恢复契约](../../docs/architecture.md#object-and-operation-lifecycle)。只有对 Runner 自己创建的一次性零件，或用户明确允许放弃未保存工作时，才允许丢弃并重新打开。重启连接不能修复一个状态不确定的模型。

最终回复应包含：零件/产物路径、实际执行过的验证，以及仍然存在的不确定项。不要向用户暴露 bridge token 或 descriptor。批处理验收成功也不能证明交互式 NX GUI 当前一定响应正常。
