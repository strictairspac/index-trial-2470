"""SQLAlchemy/MySQL 存储适配，实现 services.missing_data.Store 端口。

并发策略与 SqliteStore 对称：
- 进程内按 device_id 分桶互斥（多 worker 部署时由 MySQL 行锁兜底）；
- 事务内先对涉及设备取行锁（MySQL: SELECT ... FOR UPDATE），
  同设备先提交者胜，不同设备锁不同行，可并行提交；
- request_id 主键天然兜底重复写入。
"""
from __future__ import annotations

import json
import threading
from contextlib import contextmanager

from sqlalchemy import text

from src.config.database import Session
from src.models.missing import DeviceReading, MissingInterval, IdempotencyKey
from src.services.missing_data import Store


class SqlAlchemyStore(Store):
    def __init__(self):
        self._guard = threading.Lock()
        self._device_locks: dict[str, threading.Lock] = {}

    def _device_lock(self, device_id: str) -> threading.Lock:
        with self._guard:
            lock = self._device_locks.get(device_id)
            if lock is None:
                lock = threading.Lock()
                self._device_locks[device_id] = lock
            return lock

    @contextmanager
    def transaction(self, device_ids):
        locks = [self._device_lock(d) for d in sorted(set(device_ids))]
        for lock in locks:
            lock.acquire()
        session = Session()
        try:
            # 设备行锁：同设备串行，不同设备不互斥（MySQL 8 行级锁）。
            marks = ','.join(f':d{i}' for i in range(len(set(device_ids))))
            params = {f'd{i}': d for i, d in enumerate(sorted(set(device_ids)))}
            session.execute(
                text(
                    'SELECT device_id FROM device_readings WHERE device_id IN '
                    f'({marks}) ORDER BY device_id FOR UPDATE'
                ),
                params,
            )
            session.execute(
                text(
                    'SELECT device_id FROM missing_intervals WHERE device_id IN '
                    f'({marks}) ORDER BY device_id FOR UPDATE'
                ),
                params,
            )
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
            for lock in reversed(locks):
                lock.release()

    def find_idempotent(self, session, request_id: str):
        row = session.get(IdempotencyKey, request_id)
        if row is None:
            return None
        return {'status_code': row.status_code, 'body': json.loads(row.response_body)}

    def save_idempotent(self, session, request_id: str, scope: str, status_code: int, body: dict):
        session.add(IdempotencyKey(
            request_id=request_id,
            scope=scope,
            status_code=status_code,
            response_body=json.dumps(body, ensure_ascii=False),
        ))

    def readings_in(self, session, device_id: str, start: str, end: str) -> list[dict]:
        rows = (
            session.query(DeviceReading)
            .filter(DeviceReading.device_id == device_id,
                    DeviceReading.recorded_at >= start,
                    DeviceReading.recorded_at < end)
            .order_by(DeviceReading.recorded_at)
            .all()
        )
        return [{'device_id': r.device_id, 'recorded_at': r.recorded_at,
                 'value': r.value, 'source': r.source} for r in rows]

    def existing_reading_points(self, session, device_id: str, timestamps: list[str]) -> list[str]:
        if not timestamps:
            return []
        rows = (
            session.query(DeviceReading.recorded_at)
            .filter(DeviceReading.device_id == device_id,
                    DeviceReading.recorded_at.in_(timestamps))
            .order_by(DeviceReading.recorded_at)
            .all()
        )
        return [r[0] for r in rows]

    def missing_overlaps(self, session, device_id: str, start: str, end: str) -> list[dict]:
        rows = (
            session.query(MissingInterval)
            .filter(MissingInterval.device_id == device_id,
                    MissingInterval.start_at < end,
                    MissingInterval.end_at > start)
            .order_by(MissingInterval.start_at)
            .all()
        )
        return [{'id': r.id, 'start_at': r.start_at, 'end_at': r.end_at, 'note': r.note} for r in rows]

    def missing_containing_point(self, session, device_id: str, point: str) -> list[dict]:
        rows = (
            session.query(MissingInterval)
            .filter(MissingInterval.device_id == device_id,
                    MissingInterval.start_at <= point,
                    MissingInterval.end_at > point)
            .order_by(MissingInterval.start_at)
            .all()
        )
        return [{'id': r.id, 'start_at': r.start_at, 'end_at': r.end_at, 'note': r.note} for r in rows]

    def list_missing(self, session, device_id: str, start: str, end: str) -> list[dict]:
        return self.missing_overlaps(session, device_id, start, end)

    def insert_missing(self, session, device_id: str, start: str, end: str, note: str) -> dict:
        row = MissingInterval(device_id=device_id, start_at=start, end_at=end, note=note)
        session.add(row)
        session.flush()
        return {'id': row.id, 'device_id': device_id, 'start_at': start, 'end_at': end, 'note': note}

    def insert_readings(self, session, items: list[dict]):
        session.bulk_insert_mappings(DeviceReading, [
            {'device_id': it['device_id'], 'recorded_at': it['recorded_at'],
             'value': it['value'], 'source': it.get('source', 'backfill')}
            for it in items
        ])
