"""单手腊叶标本翻拍接力 API —— 应用入口。"""
from __future__ import annotations

import os

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .db import init_db
from .planner import PlanInfeasible
from .routers import equipment, plans, profiles, sessions, volumes


def create_app(db_path: str | None = None) -> FastAPI:
    db_path = db_path or os.environ.get("HERBARIUM_DB", "./herbarium.db")
    init_db(db_path)
    app = FastAPI(
        title="单手腊叶标本翻拍接力 API",
        version="1.0.0",
        summary="面向单手可操作志愿者的脆弱腊叶标本逐页翻拍编排与执行服务（纯后端，资料仅存本机）",
    )
    app.state.db_path = db_path

    @app.exception_handler(PlanInfeasible)
    async def _infeasible(_request: Request, exc: PlanInfeasible):
        return JSONResponse(status_code=422, content={"detail": {"reasons": exc.reasons}})

    app.include_router(profiles.router)
    app.include_router(volumes.router)
    app.include_router(equipment.router)
    app.include_router(plans.router)
    app.include_router(sessions.router)

    @app.get("/", tags=["元信息"])
    def root():
        return {
            "service": "单手腊叶标本翻拍接力 API",
            "version": "1.0.0",
            "docs": "/docs",
            "pipeline": ["固定", "揭隔页", "展平", "拍摄前核对", "拍摄", "拍摄后复核", "复位"],
        }

    return app


app = create_app()
