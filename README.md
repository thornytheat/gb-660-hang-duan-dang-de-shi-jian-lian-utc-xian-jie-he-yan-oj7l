# 航班时刻运行台

面向真实数据复核的中型数据工程工具，包含六个可独立调用的业务模块。规则、失败原因与证据字段见 docs/DATA-DICTIONARY.md，陷阱和测试映射见 docs/DEPTH.md。

## 启动

docker compose up --build

浏览器访问 http://localhost:18219。测试：docker compose exec -T app pytest -q

## 模块

- M1 航段采信：页面 /legs，接口 /api/legs/import
- M2 机场时区：页面 /airports，查询 /api/airports，治理接口前缀 /api/airports（change-requests、takeovers、readback）
- M3 当地时刻：页面 /local-time，接口 /api/local-time/resolve
- M4 UTC 航链：页面 /itineraries，接口 /api/itineraries/build
- M5 衔接判定：页面 /connections，接口 /api/connections/check
- M6 批次导出：页面 /batches，接口 /api/reconcile

## 运行规则

同一输入内容生成相同运行标识，重复执行复用已有批次。隔离记录包含原因码、原始行号与原值；业务导出只保留一份证据清单。

## 机场时区治理

正式登记变更走“申请 + 双人复核”（申请人与复核人必须不同，越权返回 403 原因码）；突发错配使用限时应急接管，仅覆盖指定机场与半开日期区间且有硬到期时间，不改写正式登记。接管交接分两步落库并记录进度，失败后同参续跑、重复执行不新增接管记录。批准正式申请时若与有效接管并发，必须显式裁决（搁置或批准为最新正式登记）；接管到期或撤销后恢复当前获批登记，不回退旧值。详见 docs/DEPTH.md 的 M2 时区治理接口。

