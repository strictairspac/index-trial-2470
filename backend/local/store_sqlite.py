"""SQLite 存储适配（仅标准库），实现 services.missing_data.Store 端口。

生产环境使用 MySQL + SQLAlchemy（见 src/services/missing_data_sqlalchemy.py），
本地无 Docker/pip 时用本适配跑同一套领域服务与真实线程并发。

SQLite 同一时刻只允许一个写事务，因此跨设备物理写由 busy_timeout 短暂排队；
但设备级互斥锁仍按 device_id 分桶——同设备串行、不同设备可同时处于事务中。
MySQL 上对应 SELECT ... FOR UPDATE 行锁，可真正并行写不同设备。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager

from src.services.missing_data import Store

_DDL = [
    """CREATE TABLE IF NOT EXISTS device_readings(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        device_id TEXT NOT NULL,
        recorded_at TEXT NOT NULL,
        value REAL NOT NULL,
        source TEXT NOT NULL DEFAULT 'backfill',
        UNIQUE(device_id, recorded_at)
    )""",
    "CREATE INDEX IF NOT EXISTS ix_device_readings_device_time ON device_readings(device_id, recorded_at)",
    """CREATE TABLE IF NOT EXISTS missing_intervals(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        device_id TEXT NOT NULL,
        start_at TEXT NOT NULL,
        end_at TEXT NOT NULL,
        note TEXT NOT NULL DEFAULT ''
    )""",
    "CREATE INDEX IF NOT EXISTS ix_missing_device_time ON missing_intervals(device_id, start_at, end_at)",
    """CREATE TABLE IF NOT EXISTS idempotency_keys(
        request_id TEXT PRIMARY KEY,
        scope TEXT NOT NULL,
        status_code INTEGER NOT NULL,
        response_body TEXT NOT NULL
    )""",
]


class SqliteStore(Store):
    def __init__(self, path: str):
        self.path = path
        self._guard = threading.Lock()
        self._device_locks: dict[str, threading.Lock] = {}
        # SQLite 只有库级写锁：冲突检查可并行，真正的写事务在此串行，
        # 避免多连接互等 busy_timeout 造成 "database is locked"。
        # 设备级互斥仍由 _device_locks 分桶保证；MySQL 适配用行锁，无此串行点。
        self._write_guard = threading.Lock()
        self._init_schema()

    def _device_lock(self, device_id: str) -> threading.Lock:
        with self._guard:
            lock = self._device_locks.get(device_id)
            if lock is None:
                lock = threading.Lock()
                self._device_locks[device_id] = lock
            return lock

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA busy_timeout=10000')
        conn.execute('PRAGMA foreign_keys=ON')
        return conn

    def _init_schema(self) -> None:
        conn = self._connect()
        try:
            for stmt in _DDL:
                conn.execute(stmt)
        finally:
            conn.close()

    @contextmanager
    def transaction(self, device_ids):
        locks = [self._device_lock(d) for d in sorted(set(device_ids))]
        for lock in locks:
            lock.acquire()
        try:
            # SQLite 只有库级写锁：写事务在此串行，避免多连接互等 busy_timeout。
            # 设备级互斥仍由 _device_locks 分桶保证；MySQL 适配用行锁，无此串行点。
            with self._write_guard:
                conn = self._connect()
                try:
                    conn.execute('BEGIN IMMEDIATE')
                    yield conn
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
                finally:
                    conn.close()
        finally:
            # 设备锁必须在任何退出路径（含 yield 抛异常）下释放，否则自死锁
            for lock in reversed(locks):
                lock.release()

    def find_idempotent(self, conn, request_id: str):
        row = conn.execute(
            'SELECT status_code, response_body FROM idempotency_keys WHERE request_id=?',
            (request_id,),
        ).fetchone()
        if row is None:
            return None
        return {'status_code': row['status_code'], 'body': json.loads(row['response_body'])}

    def save_idempotent(self, conn, request_id: str, scope: str, status_code: int, body: dict):
        conn.execute(
            'INSERT INTO idempotency_keys(request_id, scope, status_code, response_body) VALUES(?,?,?,?)',
            (request_id, scope, status_code, json.dumps(body, ensure_ascii=False)),
        )

    def readings_in(self, conn, device_id: str, start: str, end: str) -> list[dict]:
        rows = conn.execute(
            'SELECT device_id, recorded_at, value, source FROM device_readings '
            'WHERE device_id=? AND recorded_at>=? AND recorded_at<? ORDER BY recorded_at',
            (device_id, start, end),
        ).fetchall()
        return [dict(r) for r in rows]

    def existing_reading_points(self, conn, device_id: str, timestamps: list[str]) -> list[str]:
        if not timestamps:
            return []
        marks = ','.join('?' for _ in timestamps)
        rows = conn.execute(
            f'SELECT recorded_at FROM device_readings WHERE device_id=? AND recorded_at IN ({marks}) ORDER BY recorded_at',
            [device_id, *timestamps],
        ).fetchall()
        return [r['recorded_at'] for r in rows]

    def missing_overlaps(self, conn, device_id: str, start: str, end: str) -> list[dict]:
        rows = conn.execute(
            'SELECT id, start_at, end_at, note FROM missing_intervals '
            'WHERE device_id=? AND start_at<? AND end_at>? ORDER BY start_at',
            (device_id, end, start),
        ).fetchall()
        return [dict(r) for r in rows]

    def missing_containing_point(self, conn, device_id: str, point: str) -> list[dict]:
        rows = conn.execute(
            'SELECT id, start_at, end_at, note FROM missing_intervals '
            'WHERE device_id=? AND start_at<=? AND end_at>? ORDER BY start_at',
            (device_id, point, point),
        ).fetchall()
        return [dict(r) for r in rows]

    def list_missing(self, conn, device_id: str, start: str, end: str) -> list[dict]:
        return self.missing_overlaps(conn, device_id, start, end)

    def insert_missing(self, conn, device_id: str, start: str, end: str, note: str) -> dict:
        cur = conn.execute(
            'INSERT INTO missing_intervals(device_id, start_at, end_at, note) VALUES(?,?,?,?)',
            (device_id, start, end, note),
        )
        return {'id': cur.lastrowid, 'device_id': device_id, 'start_at': start, 'end_at': end, 'note': note}

    def insert_readings(self, conn, items: list[dict]):
        conn.executemany(
            'INSERT INTO device_readings(device_id, recorded_at, value, source) VALUES(?,?,?,?)',
            [(it['device_id'], it['recorded_at'], it['value'], it.get('source', 'backfill')) for it in items],
        )
