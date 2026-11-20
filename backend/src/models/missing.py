from sqlalchemy import Column, Integer, String, Float, Text, UniqueConstraint, Index
from src.config.database import Base


class DeviceReading(Base):
    """设备遥测读数（补录写入目标）。区间语义：所有时间区间均为含起点、不含终点。"""
    __tablename__ = 'device_readings'
    id = Column(Integer, primary_key=True)
    device_id = Column(String(64), nullable=False)
    recorded_at = Column(String(30), nullable=False)
    value = Column(Float, nullable=False)
    source = Column(String(20), nullable=False, default='backfill')
    __table_args__ = (
        UniqueConstraint('device_id', 'recorded_at', name='uq_device_readings_point'),
        Index('ix_device_readings_device_time', 'device_id', 'recorded_at'),
    )


class MissingInterval(Base):
    """缺测登记：声明设备在 [start_at, end_at) 内确实没有采到数据。"""
    __tablename__ = 'missing_intervals'
    id = Column(Integer, primary_key=True)
    device_id = Column(String(64), nullable=False)
    start_at = Column(String(30), nullable=False)
    end_at = Column(String(30), nullable=False)
    note = Column(String(255), nullable=False, default='')
    __table_args__ = (
        Index('ix_missing_device_time', 'device_id', 'start_at', 'end_at'),
    )


class IdempotencyKey(Base):
    """请求中断重试的去重账本：同一 request_id 只生效一次。"""
    __tablename__ = 'idempotency_keys'
    request_id = Column(String(80), primary_key=True)
    scope = Column(String(40), nullable=False)
    status_code = Column(Integer, nullable=False)
    response_body = Column(Text, nullable=False)
