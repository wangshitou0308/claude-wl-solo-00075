"""翻拍计划：把册页、能力档案与器材编排为可执行的动作序列。"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException

from ..deps import get_db
from ..planner import generate_steps
from ..runtime import fetch_steps, insert_steps, load_plan_context, step_out
from ..schemas import PlanIn
from ..util import get_or_404, jdump, new_id, now_iso

router = APIRouter(prefix="/plans", tags=["翻拍计划"])


def _plan_out(conn, plan: dict) -> dict:
    steps = fetch_steps(conn, plan["id"])
    return {
        "id": plan["id"],
        "volume_id": plan["volume_id"],
        "profile_id": plan["profile_id"],
        "equipment_ids": json.loads(plan["equipment_ids"]),
        "status": plan["status"],
        "created_at": plan["created_at"],
        "steps": [step_out(s) for s in steps],
    }


@router.post("", status_code=201)
def create_plan(body: PlanIn, conn=Depends(get_db)):
    get_or_404(conn, "volumes", body.volume_id, "标本册")
    get_or_404(conn, "capability_profiles", body.profile_id, "能力档案")
    for equipment_id in body.equipment_ids:
        get_or_404(conn, "equipment", equipment_id, "器材")
    pages = conn.execute(
        "SELECT COUNT(*) AS n FROM pages WHERE volume_id = ?", (body.volume_id,)).fetchone()["n"]
    if not pages:
        raise HTTPException(status_code=422, detail="标本册尚未登记册页次序，无法编排")
    plan_id = new_id()
    conn.execute(
        "INSERT INTO plans (id, volume_id, profile_id, equipment_ids, status, created_at)"
        " VALUES (?,?,?,?, 'ready', ?)",
        (plan_id, body.volume_id, body.profile_id, jdump(body.equipment_ids), now_iso()),
    )
    plan = get_or_404(conn, "plans", plan_id, "翻拍计划")
    volume, profile, equipment, page_rows, zones = load_plan_context(conn, plan)
    # 能力/器材/易碎区约束不满足时，generate_steps 抛 PlanInfeasible（422）
    drafts = generate_steps(volume, profile, equipment, page_rows, zones)
    insert_steps(conn, plan_id, drafts, 0)
    conn.commit()
    return _plan_out(conn, get_or_404(conn, "plans", plan_id, "翻拍计划"))


@router.get("")
def list_plans(conn=Depends(get_db)):
    rows = conn.execute("SELECT * FROM plans ORDER BY created_at").fetchall()
    return [_plan_out(conn, dict(r)) for r in rows]


@router.get("/{plan_id}")
def get_plan(plan_id: str, conn=Depends(get_db)):
    return _plan_out(conn, get_or_404(conn, "plans", plan_id, "翻拍计划"))
