"""端到端 HTTP 验证：真实起线程 HTTP 服务，覆盖需求点名场景。

运行（backend/ 下）：python tests/http_verification.py
"""
import json
import os
import sys
import tempfile
import threading
import time
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from local.server import Handler, build_service

T0, T1, T2, T3, T4, T5 = [f'2026-11-20T0{i}:00:00' for i in range(6)]


def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f'http://127.0.0.1:{PORT}{path}', data=data, method=method,
                                 headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def check(name, cond, detail=''):
    print(('  PASS' if cond else '  FAIL'), name, detail)
    if not cond:
        raise SystemExit(f'场景失败：{name} {detail}')


if __name__ == '__main__':
    tmp = tempfile.NamedTemporaryFile(suffix='.sqlite', delete=False)
    tmp.close()
    Handler.service = build_service(tmp.name)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    PORT = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    time.sleep(0.1)

    print('场景1 双方都查到空区间（登记 + 读数空档，半开边界）')
    status, body = call('POST', '/api/missing-intervals', {
        'request_id': 'http-reg-0001', 'device_id': 'dev-7',
        'start_at': T2, 'end_at': T4, 'note': '泵站17巡检缺测'})
    check('登记 201', status == 201, body)
    status, body = call('GET', f'/api/gaps?device_id=dev-7&start_at={T0}&end_at={T5}')
    check('无读数锚点时只返回登记区间', status == 200 and body['gaps'] == [{'start_at': T2, 'end_at': T4}], body)
    # 补一条读数制造锚点，再查
    call('POST', '/api/readings/backfill', {'request_id': 'http-bf-0001', 'items': [
        {'device_id': 'dev-7', 'recorded_at': T1, 'value': 30.5}]})
    status, body = call('GET', f'/api/gaps?device_id=dev-7&start_at={T0}&end_at={T5}')
    expect = [{'start_at': T0, 'end_at': T1}, {'start_at': T1, 'end_at': T2},
              {'start_at': T2, 'end_at': T4}, {'start_at': T4, 'end_at': T5}]
    check('读数锚点 + 登记区间拼出完整空段且读数点不被包含', body['gaps'] == expect, body)

    print('场景2 相邻区间允许、重叠拒绝，冲突指出设备和时间')
    # dev-7 的 T1 已有读数，相邻登记只取读数点之外的空档
    status, body = call('POST', '/api/missing-intervals', {
        'request_id': 'http-reg-0002', 'device_id': 'dev-7', 'start_at': T4, 'end_at': T5})
    check('右侧相邻登记成功（终点 T4 == 已有缺测起点 T4）', status == 201, body)
    status, body = call('POST', '/api/missing-intervals', {
        'request_id': 'http-reg-0003', 'device_id': 'dev-7', 'start_at': T3, 'end_at': T5})
    check('重叠登记 409', status == 409, body)
    check('冲突指明设备与时间', body['conflicts'][0]['device_id'] == 'dev-7'
          and T4 in body['conflicts'][0]['time'], body)
    # 区间内已有读数
    status, body = call('POST', '/api/missing-intervals', {
        'request_id': 'http-reg-0004', 'device_id': 'dev-7', 'start_at': T0, 'end_at': T2})
    check('含已有读数 409 且定位读数时刻', status == 409
          and body['conflicts'][0]['reason'] == 'reading_exists'
          and body['conflicts'][0]['time'] == T1, body)

    print('场景3 跨设备补录只撞一条 -> 整批拒绝、合法条目不落库')
    status, body = call('POST', '/api/readings/backfill', {'request_id': 'http-bf-0002', 'items': [
        {'device_id': 'dev-8', 'recorded_at': T1, 'value': 1.0},
        {'device_id': 'dev-7', 'recorded_at': T3, 'value': 2.0},   # 落在 dev-7 缺测 [T2,T4)
        {'device_id': 'dev-9', 'recorded_at': T1, 'value': 3.0}]})
    check('整批 409', status == 409, body)
    check('只撞一条且定位 dev-7/T3', len(body['conflicts']) == 1
          and body['conflicts'][0]['device_id'] == 'dev-7'
          and body['conflicts'][0]['time'] == T3, body)
    status, body = call('GET', f'/api/gaps?device_id=dev-8&start_at={T0}&end_at={T5}')
    check('dev-8 无读数无登记，不臆造空区间', body['gaps'] == [], body)
    status, body = call('GET', f'/api/gaps?device_id=dev-9&start_at={T0}&end_at={T5}')
    check('dev-9 同样无落库', body['gaps'] == [], body)

    print('场景4 半开边界：另一台设备登记 [T2,T4)，T2 撞、T4 可写')
    call('POST', '/api/missing-intervals', {
        'request_id': 'http-reg-edge-1', 'device_id': 'dev-12',
        'start_at': T2, 'end_at': T4, 'note': '半开边界验证'})
    status, body = call('POST', '/api/readings/backfill', {'request_id': 'http-bf-0003', 'items': [
        {'device_id': 'dev-12', 'recorded_at': T2, 'value': 1.0}]})
    check('起点时刻 T2 落入缺测被拒', status == 409, body)
    status, body = call('POST', '/api/readings/backfill', {'request_id': 'http-bf-0004', 'items': [
        {'device_id': 'dev-12', 'recorded_at': T4, 'value': 1.0}]})
    check('终点时刻 T4 不含于缺测，可写', status == 201, body)

    print('场景5 中断重试：同 request_id 重复提交不重复写入')
    payload = {'request_id': 'http-idem-001', 'items': [
        {'device_id': 'dev-10', 'recorded_at': T1, 'value': 9.9}]}
    s1, b1 = call('POST', '/api/readings/backfill', payload)
    s2, b2 = call('POST', '/api/readings/backfill', payload)
    s3, b3 = call('POST', '/api/readings/backfill', payload)
    check('重试返回同一结果', (s1, b1) == (s2, b2) == (s3, b3) == (201, {'inserted': 1, 'devices': ['dev-10']}), (s1, b1, s2, b2))
    status, body = call('GET', f'/api/gaps?device_id=dev-10&start_at={T0}&end_at={T5}')
    check('只有一条读数锚点', len(body['gaps']) == 2, body)

    print('场景6 同设备并发登记：先提交者胜，后到者 409')
    import concurrent.futures
    def reg(rid):
        return call('POST', '/api/missing-intervals', {
            'request_id': rid, 'device_id': 'dev-11', 'start_at': T1, 'end_at': T2})
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(reg, [f'http-cc-{i:04d}' for i in range(8)]))
    wins = [r for r in results if r[0] == 201]
    loses = [r for r in results if r[0] == 409]
    check('恰好一个成功', len(wins) == 1 and len(loses) == 7, results)
    check('败者冲突指明设备', all(r[1]['conflicts'][0]['device_id'] == 'dev-11' for r in loses))

    print('场景7 不同设备并发补录：全部成功')
    def bf(rid, device):
        return call('POST', '/api/readings/backfill', {'request_id': rid, 'items': [
            {'device_id': device, 'recorded_at': T1, 'value': 1.0}]})
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda i: bf(f'http-multi-{i:04d}', f'dev-m{i}'), range(8)))
    check('八台设备全部 201', all(r[0] == 201 for r in results), results)

    server.shutdown()
    print('\n全部 HTTP 场景验证通过')
