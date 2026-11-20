from datetime import datetime
from pydantic import BaseModel, Field, model_validator


def _iso(value: str) -> str:
    return datetime.fromisoformat(value).isoformat(timespec='seconds')


class GapQuery(BaseModel):
    device_id: str = Field(min_length=1, max_length=64)
    start_at: str
    end_at: str
    limit: int = Field(default=200, ge=1, le=2000)
    cursor: str | None = None

    @model_validator(mode='after')
    def _valid(self):
        start = datetime.fromisoformat(self.start_at)
        end = datetime.fromisoformat(self.end_at)
        if start >= end:
            raise ValueError('结束时间必须晚于开始时间（区间含起点不含终点）')
        if self.cursor is not None:
            datetime.fromisoformat(self.cursor)
        self.start_at = start.isoformat(timespec='seconds')
        self.end_at = end.isoformat(timespec='seconds')
        return self


class MissingRegisterInput(BaseModel):
    request_id: str = Field(min_length=8, max_length=80)
    device_id: str = Field(min_length=1, max_length=64)
    start_at: str
    end_at: str
    note: str = Field(default='', max_length=255)

    @model_validator(mode='after')
    def _valid(self):
        start = datetime.fromisoformat(self.start_at)
        end = datetime.fromisoformat(self.end_at)
        if start >= end:
            raise ValueError('结束时间必须晚于开始时间（区间含起点不含终点）')
        self.start_at = start.isoformat(timespec='seconds')
        self.end_at = end.isoformat(timespec='seconds')
        return self


class BackfillItem(BaseModel):
    device_id: str = Field(min_length=1, max_length=64)
    recorded_at: str
    value: float

    @model_validator(mode='after')
    def _valid(self):
        self.recorded_at = _iso(self.recorded_at)
        return self


class BackfillInput(BaseModel):
    request_id: str = Field(min_length=8, max_length=80)
    items: list[BackfillItem] = Field(min_length=1, max_length=5000)
