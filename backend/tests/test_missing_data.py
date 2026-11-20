"""领域服务测试：半开区间语义、登记/补录冲突、相邻区间、跨设备、并发、幂等。

运行（在 backend/ 下，无需第三方依赖）：
    python -m unittest discover -s tests -v
"""
import os
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from local.store_sqlite import SqliteStore
from src.services.missing_data import GapService, ServiceError, compute_empty_gaps, merge_intervals

T0, T1, T2, T3, T4, T5 = [f'2026-11-20T0{i}:00:00' for i in range(6)]


class IntervalAlgorithmTests(unittest.TestCase):
    def test_merge_touches_but_not_gap(self):
        # 相邻（前终点==后起点）应合并；相等端点不算重叠也不算空档
        self.assertEqual(merge_intervals([(T0, T1), (T1, T2)]), [(T0, T2)])
        self.assertEqual(merge_intervals([(T0, T2), (T1, T3)]), [(T0, T3)])

    def test_empty_gaps_half_open(self):
        # 读数点在 T1、T3：空段为边缘两段与点间一段，读数点本身不被任何空段包含
        gaps = compute_empty_gaps(T0, T5, [T1, T3], [])
        self.assertEqual([(g['start_at'], g['end_at']) for g in gaps],
                         [(T0, T1), (T1, T3), (T3, T5)])
        # 连续读数点 T1,T2,T3：点与点之间也是无数据的半开空段，窗口边缘两段同样保留
        gaps = compute_empty_gaps(T0, T5, [T1, T2, T3], [])
        self.assertEqual([(g['start_at'], g['end_at']) for g in gaps],
                         [(T0, T1), (T1, T2), (T2, T3), (T3, T5)])

    def test_empty_gap_endpoint_exclusion(self):
        # 读数落在窗口终点上：终点不含，该点不参与锚定；无锚点且无登记时不臆造空档
        gaps = compute_empty_gaps(T0, T1, [T1], [])
        self.assertEqual(gaps, [])
        # 读数落在窗口起点（含）与 T1：仅末点 T1 到窗口终点 T2 为空段
        gaps = compute_empty_gaps(T0, T2, [T0, T1], [])
        self.assertEqual([(g['start_at'], g['end_at']) for g in gaps],
                         [(T0, T1), (T1, T2)])

    def test_empty_gaps_include_registered_missing(self):
        # 读数点 T1 锚定的空档与登记区间 [T2,T3) 相邻：读数点 T1 把两段隔开
        gaps = compute_empty_gaps(T0, T4, [T1], [(T2, T3)])
        self.assertEqual([(g['start_at'], g['end_at']) for g in gaps],
                         [(T0, T1), (T1, T2), (T2, T3), (T3, T4)])
        # 登记区间 [T3,T4) 落在读数衍生空档 [T1,T5) 内：扣除后剩余 [T1,T3) 与 [T4,T5)
        gaps = compute_empty_gaps(T0, T5, [T1], [(T3, T4)])
        self.assertEqual([(g['start_at'], g['end_at']) for g in gaps],
                         [(T0, T1), (T1, T3), (T3, T4), (T4, T5)])
        # 登记区间夹住读数锚点，空段在读数点两侧断开
        gaps = compute_empty_gaps(T0, T5, [T1, T4], [(T2, T3)])
        self.assertEqual([(g['start_at'], g['end_at']) for g in gaps],
                         [(T0, T1), (T1, T2), (T2, T3), (T3, T4), (T4, T5)])


class ServiceFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix='.sqlite', delete=False)
        self.tmp.close()
        self.store = SqliteStore(self.tmp.name)
        self.svc = GapService(self.store)

    def tearDown(self):
        for suffix in ('', '-wal', '-shm'):
            try:
                os.unlink(self.tmp.name + suffix)
            except FileNotFoundError:
                pass

    def register(self, device, start, end, rid=None, note=''):
        return self.svc.register_missing({
            'request_id': rid or f'reg-{device}-{start}-{end}',
            'device_id': device, 'start_at': start, 'end_at': end, 'note': note,
        })

    def backfill(self, rid, items):
        return self.svc.backfill({'request_id': rid, 'items': items})


class RegisterTests(ServiceFixture):
    def test_register_empty_interval_ok_and_visible_in_gaps(self):
        result = self.register('dev-A', T1, T3, rid='reg-empty-001')
        self.assertEqual(result['status_code'], 201)
        # 双方都能查到这个空区间：gaps 接口同样返回它
        gaps = self.svc.query_gaps({'device_id': 'dev-A', 'start_at': T0, 'end_at': T5})
        self.assertIn({'start_at': T1, 'end_at': T3}, gaps['gaps'])

    def test_register_rejected_when_reading_exists(self):
        self.backfill('bf-before-001', [{'device_id': 'dev-A', 'recorded_at': T2, 'value': 1.5}])
        with self.assertRaises(ServiceError) as ctx:
            self.register('dev-A', T1, T3, rid='reg-conflict-01')
        self.assertEqual(ctx.exception.code, 409)
        conflict = ctx.exception.conflicts[0]
        self.assertEqual(conflict['device_id'], 'dev-A')      # 指出设备
        self.assertEqual(conflict['time'], T2)                # 指出时间
        self.assertEqual(conflict['reason'], 'reading_exists')

    def test_register_rejected_on_overlap_but_adjacent_allowed(self):
        self.register('dev-A', T2, T3, rid='reg-base-0001')
        # 重叠 -> 拒绝，并指明设备和时间
        with self.assertRaises(ServiceError) as ctx:
            self.register('dev-A', T1, '2026-11-20T02:30:00', rid='reg-overlap-01')
        self.assertEqual(ctx.exception.conflicts[0]['reason'], 'missing_overlap')
        self.assertEqual(ctx.exception.conflicts[0]['device_id'], 'dev-A')
        # 相邻（终点==起点、起点==终点）都允许，且不互相重叠
        self.assertEqual(self.register('dev-A', T1, T2, rid='reg-left-0001')['status_code'], 201)
        self.assertEqual(self.register('dev-A', T3, T4, rid='reg-right-001')['status_code'], 201)
        with self.store.transaction(['dev-A']) as c:
            self.assertEqual(len(self.store.list_missing(c, 'dev-A', T1, T4)), 3)


class BackfillTests(ServiceFixture):
    def test_backfill_writes_all_when_clean(self):
        result = self.backfill('bf-clean-0001', [
            {'device_id': 'dev-A', 'recorded_at': T1, 'value': 1.0},
            {'device_id': 'dev-B', 'recorded_at': T2, 'value': 2.0},
        ])
        self.assertEqual(result['body']['inserted'], 2)
        self.assertEqual(sorted(result['body']['devices']), ['dev-A', 'dev-B'])

    def test_one_item_inside_missing_rejects_whole_batch(self):
        self.register('dev-B', T2, T4, rid='reg-b-missing')
        with self.assertRaises(ServiceError) as ctx:
            self.backfill('bf-cross-0001', [
                {'device_id': 'dev-A', 'recorded_at': T1, 'value': 1.0},   # 合法
                {'device_id': 'dev-B', 'recorded_at': T3, 'value': 2.0},   # 撞缺测
                {'device_id': 'dev-C', 'recorded_at': T4, 'value': 3.0},   # 合法
            ])
        self.assertEqual(ctx.exception.code, 409)
        # 只撞一条：冲突精确指向 dev-B / T3
        self.assertEqual(len(ctx.exception.conflicts), 1)
        hit = ctx.exception.conflicts[0]
        self.assertEqual(hit['device_id'], 'dev-B')
        self.assertEqual(hit['time'], T3)
        self.assertEqual(hit['reason'], 'inside_missing_interval')
        # 整批回滚：dev-A、dev-C 的合法读数也不得落库
        with self.store.transaction(['dev-A']) as c:
            self.assertEqual(self.store.readings_in(c, 'dev-A', T0, T5), [])
        with self.store.transaction(['dev-C']) as c:
            self.assertEqual(self.store.readings_in(c, 'dev-C', T0, T5), [])

    def test_backfill_half_open_boundary(self):
        # 缺测 [T2,T3)：T2 含在内会撞，T3 不含、可写
        self.register('dev-A', T2, T3, rid='reg-half-open')
        with self.assertRaises(ServiceError):
            self.backfill('bf-edge-0001', [{'device_id': 'dev-A', 'recorded_at': T2, 'value': 1.0}])
        ok = self.backfill('bf-edge-0002', [{'device_id': 'dev-A', 'recorded_at': T3, 'value': 1.0}])
        self.assertEqual(ok['status_code'], 201)

    def test_duplicate_reading_rejected(self):
        self.backfill('bf-dup-0001', [{'device_id': 'dev-A', 'recorded_at': T1, 'value': 1.0}])
        with self.assertRaises(ServiceError) as ctx:
            self.backfill('bf-dup-0002', [{'device_id': 'dev-A', 'recorded_at': T1, 'value': 9.9}])
        self.assertEqual(ctx.exception.conflicts[0]['reason'], 'reading_exists')

    def test_duplicate_inside_batch_rejected(self):
        with self.assertRaises(ServiceError) as ctx:
            self.backfill('bf-inner-001', [
                {'device_id': 'dev-A', 'recorded_at': T1, 'value': 1.0},
                {'device_id': 'dev-A', 'recorded_at': T1, 'value': 2.0},
            ])
        self.assertEqual(ctx.exception.conflicts[0]['reason'], 'duplicate_in_batch')


class IdempotencyTests(ServiceFixture):
    def test_retry_same_request_does_not_duplicate(self):
        payload = {'request_id': 'idem-register-1', 'device_id': 'dev-A',
                   'start_at': T1, 'end_at': T2, 'note': ''}
        first = self.svc.register_missing(payload)
        second = self.svc.register_missing(payload)  # 模拟中断后重试
        self.assertEqual(first, second)
        with self.store.transaction(['dev-A']) as c:
            self.assertEqual(len(self.store.list_missing(c, 'dev-A', T0, T5)), 1)

    def test_backfill_retry_returns_cached_and_no_dup(self):
        items = [{'device_id': 'dev-A', 'recorded_at': T1, 'value': 1.0}]
        first = self.backfill('idem-backfill-1', items)
        second = self.backfill('idem-backfill-1', items)
        self.assertEqual(first, second)
        with self.store.transaction(['dev-A']) as c:
            self.assertEqual(len(self.store.readings_in(c, 'dev-A', T0, T5)), 1)

    def test_conflict_retry_is_stable(self):
        self.register('dev-A', T1, T3, rid='idem-conf-base')
        payload = {'request_id': 'idem-conf-retry', 'device_id': 'dev-A',
                   'start_at': T2, 'end_at': T4, 'note': ''}
        for _ in range(2):
            with self.assertRaises(ServiceError) as ctx:
                self.svc.register_missing(payload)
            self.assertEqual(ctx.exception.code, 409)
        with self.store.transaction(['dev-A']) as c:
            self.assertEqual(len(self.store.list_missing(c, 'dev-A', T0, T5)), 1)


class ConcurrencyTests(ServiceFixture):
    def test_same_device_first_commit_wins(self):
        barrier = threading.Barrier(2)
        outcomes = []

        def worker(rid):
            barrier.wait()
            try:
                self.svc.register_missing({'request_id': rid, 'device_id': 'dev-X',
                                           'start_at': T1, 'end_at': T3, 'note': ''})
                outcomes.append((rid, 201))
            except ServiceError as exc:
                outcomes.append((rid, exc.code))

        t1 = threading.Thread(target=worker, args=('cc-reg-0001',))
        t2 = threading.Thread(target=worker, args=('cc-reg-0002',))
        t1.start(); t2.start(); t1.join(); t2.join()
        statuses = [code for _, code in outcomes]
        self.assertEqual(sorted(statuses), [201, 409])  # 先提交者胜，后到者 409
        with self.store.transaction(['dev-X']) as c:
            self.assertEqual(len(self.store.list_missing(c, 'dev-X', T0, T5)), 1)

    def test_different_devices_use_independent_locks(self):
        # 设备锁按 device_id 分桶：同设备是同一把锁，不同设备互不阻塞
        lock_p = self.store._device_lock('dev-P')
        lock_q = self.store._device_lock('dev-Q')
        self.assertIs(lock_p, self.store._device_lock('dev-P'))
        self.assertIsNot(lock_p, lock_q)
        self.assertTrue(lock_p.acquire(blocking=False))
        try:
            self.assertTrue(lock_q.acquire(blocking=False), '不同设备的锁不应互相阻塞')
            lock_q.release()
        finally:
            lock_p.release()

    def test_different_devices_write_without_conflict(self):
        # 两台设备写相同时间窗也互不冲突（冲突检查按设备隔离）
        for device in ('dev-P', 'dev-Q'):
            result = self.svc.register_missing({'request_id': f'cc-dev-{device}', 'device_id': device,
                                                'start_at': T1, 'end_at': T2, 'note': ''})
            self.assertEqual(result['status_code'], 201)
        with self.store.transaction(['dev-P']) as c:
            self.assertEqual(len(self.store.list_missing(c, 'dev-P', T0, T5)), 1)
        with self.store.transaction(['dev-Q']) as c:
            self.assertEqual(len(self.store.list_missing(c, 'dev-Q', T0, T5)), 1)
        # 跨设备补录：一台设备撞缺测，另一台合法，整批拒绝且合法设备不落库
        with self.assertRaises(ServiceError) as ctx:
            self.backfill('bf-multi-dev-1', [
                {'device_id': 'dev-P', 'recorded_at': T1, 'value': 1.0},   # 撞 dev-P 缺测
                {'device_id': 'dev-R', 'recorded_at': T1, 'value': 2.0},   # dev-R 无登记，本可写
            ])
        self.assertEqual([c_['device_id'] for c_ in ctx.exception.conflicts], ['dev-P'])
        with self.store.transaction(['dev-R']) as c:
            self.assertEqual(self.store.readings_in(c, 'dev-R', T0, T5), [])


if __name__ == '__main__':
    unittest.main()
