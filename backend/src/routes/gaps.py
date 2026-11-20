from fastapi import APIRouter, Depends

from src.controllers.gaps import query_gaps, register_missing, backfill_readings
from src.middlewares.auth import current, writer
from src.schemas.gaps import GapQuery, MissingRegisterInput, BackfillInput

router = APIRouter(prefix='/api', tags=['missing-data'])


@router.get('/gaps')
def gaps(query: GapQuery = Depends(), user=Depends(current)):
    """巡检窗口内确实没有数据的空区间（合并缺测登记与读数空档），支持游标分页。"""
    return query_gaps(query)


@router.post('/missing-intervals', status_code=201)
def register(data: MissingRegisterInput, user=Depends(writer)):
    """登记缺测区间 [start_at, end_at)；遇已有读数或重叠缺测返回 409 并指明设备与时间。"""
    return register_missing(data, user)


@router.post('/readings/backfill', status_code=201)
def backfill(data: BackfillInput, user=Depends(writer)):
    """批量补录；任一条落入缺测区间或与已有读数重复则整批拒绝。"""
    return backfill_readings(data, user)
