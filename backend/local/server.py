"""仅用 Python 标准库的本地 HTTP 适配，挂载同一套 GapService。

启动：python -m local.server <db_path> [port]
生产环境等价能力由 FastAPI 路由 src/routes/gaps.py 提供（MySQL + SQLAlchemy）。
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from local.store_sqlite import SqliteStore
from src.services.missing_data import GapService, ServiceError


def _iso(value: str) -> str:
    return datetime.fromisoformat(value).isoformat(timespec='seconds')


def _validate_window(device_id, start_at, end_at):
    if not isinstance(device_id, str) or not device_id:
        raise ServiceError(422, 'device_id 不能为空')
    start, end = _iso(start_at), _iso(end_at)
    if start >= end:
        raise ServiceError(422, '结束时间必须晚于开始时间（区间含起点不含终点）')
    return start, end


def _validate_register(payload: dict) -> dict:
    rid = payload.get('request_id', '')
    if not isinstance(rid, str) or len(rid) < 8:
        raise ServiceError(422, 'request_id 至少 8 个字符')
    start, end = _validate_window(payload.get('device_id', ''), payload.get('start_at', ''), payload.get('end_at', ''))
    return {
        'request_id': rid,
        'device_id': payload['device_id'],
        'start_at': start,
        'end_at': end,
        'note': str(payload.get('note', ''))[:255],
    }


def _validate_backfill(payload: dict) -> dict:
    rid = payload.get('request_id', '')
    if not isinstance(rid, str) or len(rid) < 8:
        raise ServiceError(422, 'request_id 至少 8 个字符')
    items = payload.get('items', [])
    if not isinstance(items, list) or not items:
        raise ServiceError(422, 'items 至少包含一条读数')
    clean = []
    for it in items:
        if not isinstance(it.get('device_id'), str) or not it['device_id']:
            raise ServiceError(422, '每条读数都必须包含 device_id')
        recorded_at = _iso(it['recorded_at'])
        clean.append({
            'device_id': it['device_id'],
            'recorded_at': recorded_at,
            'value': float(it['value']),
        })
    return {'request_id': rid, 'items': clean}


def build_service(db_path: str) -> GapService:
    return GapService(SqliteStore(db_path))


class Handler(BaseHTTPRequestHandler):
    service: GapService = None

    def _send(self, status: int, body: dict):
        raw = json.dumps(body, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _read_json(self) -> dict:
        length = int(self.headers.get('Content-Length') or 0)
        if not length:
            raise ServiceError(422, '请求体不能为空')
        try:
            return json.loads(self.rfile.read(length).decode('utf-8'))
        except json.JSONDecodeError:
            raise ServiceError(422, '请求体不是合法 JSON')

    def log_message(self, fmt, *args):
        return

    def do_GET(self):
        try:
            if self.path.startswith('/api/gaps?') or self.path == '/api/gaps':
                from urllib.parse import parse_qs, urlparse
                q = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
                missing = ('start_at', 'end_at', 'device_id') - q.keys()
                if missing:
                    raise ServiceError(422, f'缺少查询参数：{sorted(missing)}')
                start, end = _validate_window(q['device_id'], q['start_at'], q['end_at'])
                result = self.service.query_gaps({
                    'device_id': q['device_id'], 'start_at': start, 'end_at': end,
                    'limit': int(q.get('limit', 200)), 'cursor': q.get('cursor'),
                })
                return self._send(200, result)
            if self.path == '/health':
                return self._send(200, {'status': 'ok'})
            self._send(404, {'detail': '接口不存在'})
        except ServiceError as exc:
            self._send(exc.code, exc.body())
        except (ValueError, TypeError) as exc:
            self._send(422, {'detail': f'参数格式错误：{exc}'})
        except Exception:
            import logging
            logging.exception('request failure')
            self._send(500, {'detail': '服务暂时不可用'})

    def do_POST(self):
        try:
            payload = self._read_json()
            if self.path == '/api/missing-intervals':
                result = self.service.register_missing(_validate_register(payload))
                return self._send(result['status_code'], result['body'])
            if self.path == '/api/readings/backfill':
                result = self.service.backfill(_validate_backfill(payload))
                return self._send(result['status_code'], result['body'])
            self._send(404, {'detail': '接口不存在'})
        except ServiceError as exc:
            self._send(exc.code, exc.body())
        except (ValueError, TypeError) as exc:
            self._send(422, {'detail': f'参数格式错误：{exc}'})
        except Exception:
            import logging
            logging.exception('request failure')
            self._send(500, {'detail': '服务暂时不可用'})


def main():
    db_path = sys.argv[1] if len(sys.argv) > 1 else '/tmp/missing-data.sqlite'
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 18341
    Handler.service = build_service(db_path)
    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    print(f'local gap service on http://127.0.0.1:{port} db={db_path}')
    server.serve_forever()


if __name__ == '__main__':
    main()
