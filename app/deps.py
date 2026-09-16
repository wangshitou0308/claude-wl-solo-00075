"""FastAPI 依赖：每请求一个 SQLite 连接。"""
from __future__ import annotations

from fastapi import Request

from .db import connect


def get_db(request: Request):
    conn = connect(request.app.state.db_path)
    try:
        yield conn
    finally:
        conn.close()
