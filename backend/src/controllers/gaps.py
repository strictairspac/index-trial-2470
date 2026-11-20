"""缺测域控制器：把 FastAPI 模型翻译成领域服务入参，并写业务审计。"""
from fastapi import HTTPException

from src.schemas.gaps import GapQuery, MissingRegisterInput, BackfillInput
from src.services.missing_data import GapService, ServiceError
from src.services.missing_data_sqlalchemy import SqlAlchemyStore
from src.services.audit import log

service = GapService(SqlAlchemyStore())


def _call(action, fn, payload: dict, user) -> dict:
    try:
        result = fn(payload)
        log(user.email, action, payload.get('request_id', f'gaps:{payload.get("device_id", "")}'))
        return result['body']
    except ServiceError as exc:
        raise HTTPException(exc.code, exc.body())


def query_gaps(query: GapQuery) -> dict:
    try:
        return service.query_gaps(query.model_dump())
    except ServiceError as exc:
        raise HTTPException(exc.code, exc.body())


def register_missing(data: MissingRegisterInput, user) -> dict:
    return _call('missing.register', service.register_missing, data.model_dump(), user)


def backfill_readings(data: BackfillInput, user) -> dict:
    return _call('readings.backfill', service.backfill, data.model_dump(), user)
