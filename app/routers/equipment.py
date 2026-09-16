"""器材登记：夹具、压条、翻页垫、相机遥控。"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException

from ..deps import get_db
from ..planner import DEFAULT_BAR_WIDTH, TRIGGER_CHANNELS
from ..schemas import EquipmentIn, EquipmentPatch
from ..util import get_or_404, jdump, mark_steps_stale, new_id, now_iso

router = APIRouter(prefix="/equipment", tags=["器材"])


def _normalize_attributes(kind: str, attributes: dict) -> dict:
    attrs = dict(attributes or {})
    if kind == "camera_remote":
        trigger = attrs.get("trigger")
        if trigger not in TRIGGER_CHANNELS:
            raise HTTPException(
                status_code=422,
                detail="相机遥控须在 attributes.trigger 登记触发方式：hand / foot_pedal / single_key / voice",
            )
    elif kind == "pressure_bar":
        width = attrs.get("width", DEFAULT_BAR_WIDTH)
        if not isinstance(width, (int, float)) or isinstance(width, bool) or not 0 < float(width) <= 0.5:
            raise HTTPException(status_code=422, detail="压条 attributes.width 须在 (0, 0.5] 区间（页面归一化宽度）")
        attrs["width"] = float(width)
    return attrs


def _out(row: dict) -> dict:
    return {**row, "attributes": json.loads(row["attributes"])}


@router.post("", status_code=201)
def create_equipment(body: EquipmentIn, conn=Depends(get_db)):
    equipment_id = new_id()
    attrs = _normalize_attributes(body.kind, body.attributes)
    conn.execute(
        "INSERT INTO equipment (id, kind, name, attributes, created_at) VALUES (?,?,?,?,?)",
        (equipment_id, body.kind, body.name, jdump(attrs), now_iso()),
    )
    conn.commit()
    return _out(get_or_404(conn, "equipment", equipment_id, "器材"))


@router.get("")
def list_equipment(conn=Depends(get_db)):
    rows = conn.execute("SELECT * FROM equipment ORDER BY created_at").fetchall()
    return [_out(dict(r)) for r in rows]


@router.get("/{equipment_id}")
def get_equipment(equipment_id: str, conn=Depends(get_db)):
    return _out(get_or_404(conn, "equipment", equipment_id, "器材"))


@router.patch("/{equipment_id}")
def patch_equipment(equipment_id: str, body: EquipmentPatch, conn=Depends(get_db)):
    row = get_or_404(conn, "equipment", equipment_id, "器材")
    data = body.model_dump(exclude_unset=True)
    stale = 0
    if data:
        name = data.get("name", row["name"])
        if "attributes" in data:
            attrs = _normalize_attributes(row["kind"], data["attributes"])
        else:
            attrs = json.loads(row["attributes"])
        conn.execute("UPDATE equipment SET name = ?, attributes = ? WHERE id = ?",
                     (name, jdump(attrs), equipment_id))
        # 只使依赖该器材的待执行步骤过期
        stale = mark_steps_stale(conn, [equipment_id])
        conn.commit()
    return _out(get_or_404(conn, "equipment", equipment_id, "器材")) | {"stale_steps": stale}
