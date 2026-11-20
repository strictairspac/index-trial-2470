# Schema v2 — 缺测登记与批量补录

新增三张表，由 SQLAlchemy metadata 在启动时 `create_all` 建立（与 v1 一致的建表方式）：

## device_readings（设备读数，补录写入目标）
- `id` PK 自增
- `device_id` VARCHAR(64) NOT NULL
- `recorded_at` VARCHAR(30) NOT NULL，ISO8601 秒级时间字符串，字典序即时间序
- `value` DOUBLE NOT NULL
- `source` VARCHAR(20) NOT NULL DEFAULT 'backfill'
- 唯一约束 `uq_device_readings_point (device_id, recorded_at)`：同设备同时刻只能有一条读数，数据库层面兜底重复补录
- 索引 `(device_id, recorded_at)`

## missing_intervals（缺测登记）
- `id` PK 自增
- `device_id` VARCHAR(64) NOT NULL
- `start_at` / `end_at` VARCHAR(30) NOT NULL，区间统一半开 `[start_at, end_at)`，含起点不含终点
- `note` VARCHAR(255) NOT NULL DEFAULT ''
- 索引 `(device_id, start_at, end_at)`
- 不设数据库排他约束：重叠/相邻判定由服务层在设备锁 + 事务内完成（相邻允许、重叠拒绝）

## idempotency_keys（幂等账本）
- `request_id` VARCHAR(80) PK：客户端为每个写请求生成，重试沿用同一值
- `scope` VARCHAR(40)：`missing.register` / `readings.backfill`
- `status_code` INT、`response_body` TEXT：首次请求的状态与响应体，重试原样返回
- 成功与 409 冲突响应都落账；冲突时业务写入与账本在同一事务，读数不会落库

## 并发与锁
- 应用进程内按 `device_id` 分桶互斥；多进程/多 worker 部署由 MySQL 行级锁兜底：
  事务内对本请求涉及设备在 `device_readings`、`missing_intervals` 上
  `SELECT ... WHERE device_id IN (...) FOR UPDATE`，同设备串行、不同设备锁不同行可并行。
- 同设备先提交者胜，后到者收到 409，冲突体逐条给出 `device_id`、`time`、`reason`。
- 本地 SQLite 适配（`backend/local`）只有库级写锁，写事务经 `_write_guard` 串行，
  设备分桶锁语义不变；MySQL 上无此串行点。

## 接口（前缀 /api，写接口需 operator/admin）
- `GET /api/gaps?device_id=&start_at=&end_at=&limit=&cursor=`：巡检窗口内确认无数据的空区间
- `POST /api/missing-intervals`：登记缺测，请求体含 `request_id/device_id/start_at/end_at/note`
- `POST /api/readings/backfill`：批量补录，请求体含 `request_id/items[]`，任一条撞缺测或重复则整批回滚
