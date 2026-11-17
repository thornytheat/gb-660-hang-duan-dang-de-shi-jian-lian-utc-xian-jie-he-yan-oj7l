# 数据字典

- run_id：规则版本与输入 SHA-256 前 12 位组成的稳定批次标识。
- line_no：从 1 开始的物理来源行号，不因隔离或排序改变。
- business_key：本项目声明的业务唯一键，始终按字符串存储。
- reason_code：隔离原因的稳定英文编码。
- accepted：进入业务计算的记录数。
- quarantined：保留证据但不参与业务计算的记录数。
- 数值：金额、税额、用量和权重使用 Decimal；对外序列化为字符串。
- 时间：存储为带偏移的 ISO 8601，分桶使用半开区间。
- 空值：缺失键、空串和业务哨兵分别记录。
- 幂等：完整输入字节与规则版本相同则复用 run_id，不新增结果。
- 时区治理：变更申请（tz_change_requests）与正式版本（tz_formal_versions）分离；应急接管（tz_takeovers）独立成表，绝不 UPDATE 正式登记。
- 交接状态：tz_handoff_progress 记录 RECORDED/COMPLETED 两步；同一 takeover_id 重跑只续跑不新增。
- 接管期限：覆盖日期用半开区间 [cover_start, cover_end)，另有带偏移的 expires_at 硬到期时间；到期或撤销后回读当前获批正式登记。
- 裁决码：BLOCK_UNTIL_TAKEOVER_CLOSES / APPROVE_AS_LATEST_FORMAL，仅在批准与有效接管并发时出现。
- 角色：OPERATOR（申请）、REVIEWER（复核，且不得为申请人本人）、EMERGENCY_OPERATOR（接管/撤销）；越权返回 403 与稳定原因码。

