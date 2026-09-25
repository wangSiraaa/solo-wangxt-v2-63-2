"""应用工厂：装配数据库、注入时钟、注册路由与异常处理。"""
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from .api import router
from .clock import Clock, SystemClock
from .models import Base
from .services import DEFAULT_POLICY, DomainError, EscalationPolicy

DESCRIPTION = """\
逾期升级 SLA 暂停服务。

- 暂停申请绑定事件 / 原因 / 证据，状态机：申请 → 批准生效 / 拒绝 → 结束 / 撤销；
- 升级计算基于注入时钟，扣除已批准暂停区间的**并集**（重叠不重复延长）；
- 结案后时钟冻结，不再升级，暂停不得重新启动；
- 暂停不删除已产生升级、不改写已锁定处罚；迟到批准只产生待复核补偿建议，
  确认后追加更正并保留原处罚链。
"""


def _make_engine(db_url: str):
    if db_url in ("sqlite://", "sqlite:///:memory:"):
        return create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
    connect_args = {"check_same_thread": False} if db_url.startswith("sqlite") else {}
    return create_engine(db_url, connect_args=connect_args)


def create_app(
    db_url: str = "sqlite:///./sla.db",
    clock: Clock | None = None,
    policy: EscalationPolicy = DEFAULT_POLICY,
) -> FastAPI:
    app = FastAPI(
        title="SLA Pause & Escalation Service",
        version="1.0.0",
        description=DESCRIPTION,
    )

    engine = _make_engine(db_url)
    Base.metadata.create_all(engine)

    app.state.engine = engine
    app.state.session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    app.state.clock = clock or SystemClock()
    app.state.policy = policy

    @app.exception_handler(DomainError)
    async def domain_error_handler(_: Request, exc: DomainError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    app.include_router(router)
    return app


app = create_app()
