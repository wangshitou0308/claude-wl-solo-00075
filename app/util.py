"""通用辅助：id、时间、404、步骤过期标记。"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException


def new_id() -> str:
    return uuid.uuid4().hex


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def jdump(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False)


def get_or_404(conn: sqlite3.Connection, table: str, row_id: str, what: str) -> dict:
    row = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (row_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"{what}不存在：{row_id}")
    return dict(row)


def mark_steps_stale(conn: sqlite3.Connection, entity_ids: list[str]) -> int:
    """把依赖指定资料的“待执行”步骤标记为过期。

    已完成步骤是历史记录，不受影响；已完成计划已冻结，同样不受影响。
    """
    if not entity_ids:
        return 0
    cur = conn.execute(
        """
        UPDATE steps SET status = 'stale'
        WHERE status = 'pending'
          AND plan_id IN (SELECT id FROM plans WHERE status <> 'completed')
          AND EXISTS (
                SELECT 1 FROM json_each(steps.depends_on) AS je
                WHERE je.value IN (SELECT value FROM json_each(?))
          )
        """,
        (jdump(sorted(set(entity_ids))),),
    )
    return cur.rowcount
