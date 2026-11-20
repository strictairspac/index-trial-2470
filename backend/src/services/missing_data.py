"""缺测登记与批量补录核心领域服务。

区间语义统一为半开区间 [start, end)：含起点、不含终点。
本模块只依赖 Python 标准库与一个存储端口（Store），不绑定 Web 框架，
FastAPI 路由与本地标准库适配层共用同一份业务逻辑。

并发约定：
- 同一台设备的登记/补录串行化（设备锁），先提交者胜，后到者收到 409；
- 不同设备互不阻塞，可并发写入（锁按 device_id 分桶，只在事务期间持有）；
- 写请求必须携带 request_id，服务端持久化响应，中断后同 request_id 重试不重复写入。
"""
from __future__ import annotations

import json
import threading
from contextlib import contextmanager


class ServiceError(Exception):
    """业务失败。code 映射 HTTP 状态，conflicts 携带设备与时间定位。"""

    def __init__(self, code: int, message: str, conflicts=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.conflicts = conflicts or []

    def body(self) -> dict:
        payload = {'detail': self.message}
        if self.conflicts:
            payload['conflicts'] = self.conflicts
        return payload


class Store:
    """存储端口。所有方法在一个事务连接上执行；连接由 transaction() 提供。"""

    @contextmanager
    def transaction(self, device_ids):
        """按设备集合加锁并开启事务，yield 一个连接/会话对象。

        不同设备集合互不阻塞；同设备互斥。提交/回滚与锁释放由实现负责。
        """
        raise NotImplementedError

    # —— 以下方法均接收 transaction 产出的会话 ——
    def find_idempotent(self, conn, request_id: str):
        raise NotImplementedError

    def save_idempotent(self, conn, request_id: str, scope: str, status_code: int, body: dict):
        raise NotImplementedError

    def readings_in(self, conn, device_id: str, start: str, end: str) -> list[dict]:
        """含起点、不含终点内的读数，按 recorded_at 升序。"""
        raise NotImplementedError

    def existing_reading_points(self, conn, device_id: str, timestamps: list[str]) -> list[str]:
        """返回 timestamps 中库内已存在读数的时间点子集。"""
        raise NotImplementedError

    def insert_readings(self, conn, items: list[dict]):
        raise NotImplementedError

    def missing_overlaps(self, conn, device_id: str, start: str, end: str) -> list[dict]:
        """与半开 [start,end) 真正重叠（相邻不算）的已登记缺测区间。"""
        raise NotImplementedError

    def missing_containing_point(self, conn, device_id: str, point: str) -> list[dict]:
        """包含时刻 point 的缺测区间，按半开语义 start <= point < end。"""
        raise NotImplementedError

    def list_missing(self, conn, device_id: str, start: str, end: str) -> list[dict]:
        raise NotImplementedError

    def insert_missing(self, conn, device_id: str, start: str, end: str, note: str) -> dict:
        raise NotImplementedError


def merge_intervals(intervals: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """合并相交或相邻（前区间终点 == 后区间起点）的半开区间。"""
    if not intervals:
        return []
    ordered = sorted(intervals)
    merged: list[list[str]] = []
    for start, end in ordered:
        if merged and start <= merged[-1][1]:
            if end > merged[-1][1]:
                merged[-1][1] = end
        else:
            merged.append([start, end])
    return [(a, b) for a, b in merged]


def compute_empty_gaps(
    window_start: str,
    window_end: str,
    reading_points: list[str],
    missing_intervals: list[tuple[str, str]],
) -> list[dict]:
    """计算巡检窗口内确实没有数据的空区间。

    两类证据，均为半开 [start, end)：
    1. 已登记缺测区间：权威证据，重叠/相邻的登记先合并，原样输出；
    2. 读数点锚定的空档：相邻读数点之间、窗口边缘到首/末读数点之间。
       读数点是有数据的时刻，它两侧的空档即使在时间轴上相邻，也不跨越该点合并。
    读数衍生空档若与登记区间重叠，重叠部分归登记区间，剩余部分仍输出；
    窗口内一个读数都没有时不臆造边缘空档，没有锚点时只有登记区间可确认缺测。
    """
    registered = merge_intervals(list(missing_intervals))
    points = sorted({p for p in reading_points if window_start <= p < window_end})
    derived: list[tuple[str, str]] = []
    if points:
        if points[0] > window_start:
            derived.append((window_start, points[0]))
        for left, right in zip(points, points[1:]):
            if left < right:
                derived.append((left, right))
        if points[-1] < window_end:
            derived.append((points[-1], window_end))

    # 从读数衍生空档中扣除已登记缺测区间，保证同一时刻只出现在一个空段里
    for reg_start, reg_end in registered:
        remaining: list[tuple[str, str]] = []
        for start, end in derived:
            if end <= reg_start or start >= reg_end:
                remaining.append((start, end))
            else:
                if start < reg_start:
                    remaining.append((start, reg_start))
                if reg_end < end:
                    remaining.append((reg_end, end))
        derived = remaining

    intervals = sorted(registered + derived)
    out = []
    for start, end in intervals:
        start = max(start, window_start)
        end = min(end, window_end)
        if start < end:
            out.append({'start_at': start, 'end_at': end})
    return out


class GapService:
    def __init__(self, store: Store):
        self.store = store

    # ---------------- 查询（只读，无锁、无幂等） ----------------
    def query_gaps(self, payload: dict) -> dict:
        device_id = payload['device_id']
        start = payload['start_at']
        end = payload['end_at']
        limit = int(payload.get('limit') or 200)
        cursor = payload.get('cursor')
        with self.store.transaction([device_id]) as conn:
            readings = self.store.readings_in(conn, device_id, start, end)
            missing = self.store.list_missing(conn, device_id, start, end)
        gaps = compute_empty_gaps(
            start,
            end,
            [r['recorded_at'] for r in readings],
            [(m['start_at'], m['end_at']) for m in missing],
        )
        if cursor:
            gaps = [g for g in gaps if g['start_at'] >= cursor and g['start_at'] != cursor]
        page = gaps[:limit]
        next_cursor = page[-1]['end_at'] if len(gaps) > limit else None
        return {
            'device_id': device_id,
            'start_at': start,
            'end_at': end,
            'gaps': page,
            'next_cursor': next_cursor,
        }

    # ---------------- 缺测登记 ----------------
    def register_missing(self, payload: dict) -> dict:
        request_id = payload['request_id']
        device_id = payload['device_id']
        start, end = payload['start_at'], payload['end_at']
        note = payload.get('note', '')

        with self.store.transaction([device_id]) as conn:
            cached = self.store.find_idempotent(conn, request_id)
            if cached is not None:
                if cached['status_code'] != 201:
                    raise ServiceError(cached['status_code'], cached['body']['detail'],
                                       cached['body'].get('conflicts', []))
                return cached
            pending_error = None
            try:
                conflicts = self._register_conflicts(conn, device_id, start, end)
                if conflicts:
                    raise ServiceError(409, '缺测登记被拒绝：区间内已有读数或与已有缺测区间重叠', conflicts)
                row = self.store.insert_missing(conn, device_id, start, end, note)
                body = {'registered': row}
                self.store.save_idempotent(conn, request_id, 'missing.register', 201, body)
                outcome = {'status_code': 201, 'body': body}
            except ServiceError as exc:
                # 冲突响应也入幂等账本，随事务提交后再向调用方抛出
                self.store.save_idempotent(conn, request_id, 'missing.register', exc.code, exc.body())
                pending_error = exc
        if pending_error is not None:
            raise pending_error
        return outcome

    def _register_conflicts(self, conn, device_id, start, end) -> list[dict]:
        conflicts: list[dict] = []
        for row in self.store.readings_in(conn, device_id, start, end):
            conflicts.append({
                'device_id': device_id,
                'time': row['recorded_at'],
                'reason': 'reading_exists',
                'message': f"设备 {device_id} 在 {row['recorded_at']} 已有读数，不能登记为缺测",
            })
        for row in self.store.missing_overlaps(conn, device_id, start, end):
            conflicts.append({
                'device_id': device_id,
                'time': f"[{row['start_at']}, {row['end_at']})",
                'reason': 'missing_overlap',
                'message': f"设备 {device_id} 已登记缺测区间 [{row['start_at']}, {row['end_at']})，与本次区间重叠",
            })
        return conflicts

    # ---------------- 批量补录 ----------------
    def backfill(self, payload: dict) -> dict:
        request_id = payload['request_id']
        items = payload['items']
        devices = sorted({it['device_id'] for it in items})

        with self.store.transaction(devices) as conn:
            cached = self.store.find_idempotent(conn, request_id)
            if cached is not None:
                if cached['status_code'] != 201:
                    raise ServiceError(cached['status_code'], cached['body']['detail'],
                                       cached['body'].get('conflicts', []))
                return cached
            pending_error = None
            try:
                conflicts = self._backfill_conflicts(conn, items)
                if conflicts:
                    raise ServiceError(409, '补录被整批拒绝：至少一条读数落在缺测区间或与已有读数重复', conflicts)
                self.store.insert_readings(conn, items)
                body = {'inserted': len(items), 'devices': devices}
                self.store.save_idempotent(conn, request_id, 'readings.backfill', 201, body)
                outcome = {'status_code': 201, 'body': body}
            except ServiceError as exc:
                # 整批拒绝：读数写入与幂等记录在同一事务，冲突时读数不落库，仅缓存响应
                self.store.save_idempotent(conn, request_id, 'readings.backfill', exc.code, exc.body())
                pending_error = exc
        if pending_error is not None:
            raise pending_error
        return outcome

    def _backfill_conflicts(self, conn, items: list[dict]) -> list[dict]:
        conflicts: list[dict] = []
        # 批次内部同一设备同一时刻重复，也按冲突处理，保证重试安全。
        seen: set[tuple[str, str]] = set()
        internal_dup: set[tuple[str, str]] = set()
        for it in items:
            key = (it['device_id'], it['recorded_at'])
            if key in seen:
                internal_dup.add(key)
            seen.add(key)
        for device_id, recorded_at in sorted(internal_dup):
            conflicts.append({
                'device_id': device_id,
                'time': recorded_at,
                'reason': 'duplicate_in_batch',
                'message': f'设备 {device_id} 在 {recorded_at} 的读数在本次批次中重复',
            })

        by_device: dict[str, list[str]] = {}
        for device_id, recorded_at in seen:
            by_device.setdefault(device_id, []).append(recorded_at)
        for device_id, timestamps in sorted(by_device.items()):
            for hit in self.store.existing_reading_points(conn, device_id, timestamps):
                conflicts.append({
                    'device_id': device_id,
                    'time': hit,
                    'reason': 'reading_exists',
                    'message': f'设备 {device_id} 在 {hit} 已有读数，补录不得覆盖',
                })
            for ts in sorted(timestamps):
                for row in self.store.missing_containing_point(conn, device_id, ts):
                    conflicts.append({
                        'device_id': device_id,
                        'time': ts,
                        'reason': 'inside_missing_interval',
                        'message': (
                            f"设备 {device_id} 在 {ts} 的读数落在已登记缺测区间 "
                            f"[{row['start_at']}, {row['end_at']}) 内"
                        ),
                    })
        return conflicts
