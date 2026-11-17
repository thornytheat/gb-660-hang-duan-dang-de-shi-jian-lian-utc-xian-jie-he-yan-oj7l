# 深度登记 航班时刻运行台

- 规模：中型
- 核心规则：用机场 IANA 时区解析 DST 重叠与跳时，所有航链在 UTC 上校验先后和衔接。
- 使用者与工作流：运行数据员导入航段，确认机场时区，处理当地时刻歧义，构建 UTC 航链并核对转机。

## 功能模块

| 模块 | 使用者动作 | 入口 | 核心文件 | 测试 |
|---|---|---|---|---|
| M1 航段采信 | 导入航段并隔离重复冲突 | POST /api/legs/import | `app/modules/m1_legs/parser.py` `app/modules/m1_legs/keys.py` | `tests/test_m1_legs.py` |
| M2 机场时区 | 查询日期对应的时区版本，登记变更双人复核与限时应急接管 | POST /api/airports | `app/modules/m2_airports/registry.py` `app/modules/m2_airports/contracts.py` `app/modules/m2_airports/governance.py` `app/modules/m2_airports/governance_store.py` | `tests/test_m2_airports.py` `tests/test_m2_governance.py` `tests/test_m2_governance_http.py` |
| M3 当地时刻 | 解析偏移、重叠与跳时 | POST /api/local-time/resolve | `app/modules/m3_localtime/resolve.py` `app/modules/m3_localtime/evidence.py` | `tests/test_m3_localtime.py` |
| M4 UTC 航链 | 换算航段并校验先后顺序 | POST /api/itineraries/build | `app/modules/m4_itinerary/builder.py` `app/modules/m4_itinerary/validate.py` | `tests/test_m4_itinerary.py` |
| M5 衔接判定 | 核对航站楼换乘与等待 | POST /api/connections/check | `app/modules/m5_connections/rules.py` `app/modules/m5_connections/windows.py` | `tests/test_m5_connections.py` |
| M6 批次导出 | 核对航段守恒并导出时刻 | POST /api/reconcile | `app/modules/m6_batches/compare.py` `app/modules/m6_batches/exporter.py` | `tests/test_m6_batches.py` |

## 陷阱登记

| TRAP | 现象 | 朴素实现会怎样 | 本项目处理 | 处理位置 | 测试 |
|---|---|---|---|---|---|
| TRAP-03 | 尾行记录不完整 | 半条航段进入换算 | 完整边界检查后隔离 | `app/modules/m1_legs/parser.py` | `tests/test_m1_legs.py::test_truncated_tail_detected` |
| TRAP-05 | 必填时刻缺失 | 空时间在深层报错 | 采信阶段给原因码和行号 | `app/modules/m1_legs/parser.py` | `tests/test_m1_legs.py::test_missing_time_quarantined` |
| TRAP-07 | 空当地时刻 | 被转换成午夜 | 空值不参与航链 | `app/modules/m1_legs/parser.py` | `tests/test_m1_legs.py::test_missing_time_quarantined` |
| TRAP-09 | 超大外部行程号 | JSON 数字往返丢精度 | 始终按字符串保形 | `app/modules/m1_legs/keys.py` | `tests/test_m1_legs.py::test_external_id_keeps_large_digits` |
| TRAP-11 | 偏移与 Z 混用 | 同一时刻先后次序错误 | 统一到 UTC 后输出 Z | `app/modules/m3_localtime/resolve.py` | `tests/test_m3_localtime.py::test_utc_serializes_with_z` |
| TRAP-12 | DST 重叠与跳时 | 重复小时覆盖或虚构时刻 | 往返验证并显式 fold | `app/modules/m3_localtime/resolve.py` | `tests/test_m3_localtime.py::test_dst_overlap_has_two_candidates` |
| TRAP-13 | 航段乱序 | 输入顺序被当成航程顺序 | 按 UTC 起飞时刻稳定排序 | `app/modules/m4_itinerary/builder.py` | `tests/test_m4_itinerary.py::test_order_by_utc_is_stable` |
| TRAP-14 | 同航段时刻冲突 | 后写覆盖原计划 | 冲突版本进入隔离区 | `app/modules/m1_legs/parser.py` | `tests/test_m1_legs.py::test_conflicting_duplicate_detected` |
| TRAP-23 | 衔接窗口端点 | 结束时刻被重复计入 | 使用半开区间 | `app/modules/m5_connections/windows.py` | `tests/test_m5_connections.py::test_half_open_window_excludes_end` |
| TRAP-25 | 时区版本重叠 | 随机选中一版偏移 | 多个生效版本立即拒绝 | `app/modules/m2_airports/registry.py` | `tests/test_m2_airports.py::test_overlapping_timezone_versions_rejected` |
| TRAP-26 | 应急权限长期改写正式登记 | 接管接口直接 UPDATE 正式表 | 接管只写独立应急表，正式登记仅由“申请+他人复核”写入 | `app/modules/m2_airports/governance_store.py` | `tests/test_m2_governance.py::test_takeover_overrides_only_scoped_airport_and_dates` |
| TRAP-27 | 交接写一半后重试产生重复接管 | 重试 INSERT 出第二条接管 | 两步交接带 handoff 进度，按接管标识幂等续跑 | `app/modules/m2_airports/governance.py` | `tests/test_m2_governance.py::test_handoff_resumes_after_partial_failure` |
| TRAP-28 | 接管关闭后回退到接管前旧值 | 撤销即恢复历史快照 | 应急从不改正式表，关闭后实时回读当前获批登记（可能已更新） | `app/modules/m2_airports/governance.py` | `tests/test_m2_governance.py::test_expired_takeover_falls_back_to_latest_formal_not_old_value` |
| TRAP-29 | 批准与接管并发被静默覆盖 | 后写赢，查询来源不明 | 批准遇有效接管必须显式裁决：搁置或批准为最新正式登记 | `app/modules/m2_airports/governance.py` | `tests/test_m2_governance.py::test_approval_under_takeover_requires_explicit_adjudication` |
| TRAP-30 | 航链查询用默认日期取时区 | 按日期限定的接管无法纠正在用查询 | M4 按航段本地日期解析生效时区 | `app/modules/m4_itinerary/builder.py` | `tests/test_m2_governance.py::test_takeover_corrects_active_itinerary_query_and_restores` |

## M2 时区治理接口

| 接口 | 方法 | 角色 | 说明 |
|---|---|---|---|
| `/api/airports/change-requests` | POST | OPERATOR | 提交登记变更申请 |
| `/api/airports/change-requests/review` | POST | REVIEWER（须与申请人不同） | 复核批准/驳回；批准遇接管须带 adjudication |
| `/api/airports/change-requests` | GET | - | 申请列表，可按 airport 过滤 |
| `/api/airports/takeovers` | POST | EMERGENCY_OPERATOR | 限时应急接管，两步交接、幂等续跑 |
| `/api/airports/takeovers/revoke` | POST | EMERGENCY_OPERATOR | 撤销接管 |
| `/api/airports/takeovers/sweep-expired` | POST | - | 显式清理到期接管（读取时也会惰性清理） |
| `/api/airports/effective` | POST | - | 有效时区查询：应急优先、正式兜底 |
| `/api/airports/readback?airport=&at=` | GET | - | 回读正式/应急来源、期限与交接状态 |

裁决取值：`BLOCK_UNTIL_TAKEOVER_CLOSES`（搁置，申请保持 PENDING）、`APPROVE_AS_LATEST_FORMAL`（写入最新正式登记，接管期内应急仍优先）。
接管覆盖 `[cover_start, cover_end)` 半开日期区间，并受 `expires_at` 硬期限约束；航段时刻与出票规则不在治理改动范围内。

## 对账规则

- 输入航段 = 有效航段 + 冲突航段 + 隔离航段。
- 行程航段 = 可衔接航段 + 不可衔接航段。
- 到达 UTC 必须晚于起飞 UTC，飞行时长不得超过 18 小时。

## 已知不覆盖

- 不计算天气和空管延误，输入只描述计划时刻。

