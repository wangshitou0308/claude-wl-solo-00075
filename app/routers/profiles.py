"""能力档案登记：可用手、脚踏开关、单键开关、语音确认，不假定能力相同。"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from ..deps import get_db
from ..schemas import CapabilityProfileIn, CapabilityProfilePatch
from ..util import get_or_404, mark_steps_stale, new_id, now_iso

router = APIRouter(prefix="/capability-profiles", tags=["能力档案"])


def _out(row: dict) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "usable_hand": row["usable_hand"],
        "foot_pedal": bool(row["foot_pedal"]),
        "single_key_switch": bool(row["single_key_switch"]),
        "voice_confirmation": bool(row["voice_confirmation"]),
        "created_at": row["created_at"],
    }


@router.post("", status_code=201)
def create_profile(body: CapabilityProfileIn, conn=Depends(get_db)):
    profile_id = new_id()
    conn.execute(
        "INSERT INTO capability_profiles"
        " (id, name, usable_hand, foot_pedal, single_key_switch, voice_confirmation, created_at)"
        " VALUES (?,?,?,?,?,?,?)",
        (profile_id, body.name, body.usable_hand, int(body.foot_pedal),
         int(body.single_key_switch), int(body.voice_confirmation), now_iso()),
    )
    conn.commit()
    return _out(get_or_404(conn, "capability_profiles", profile_id, "能力档案"))


@router.get("")
def list_profiles(conn=Depends(get_db)):
    rows = conn.execute("SELECT * FROM capability_profiles ORDER BY created_at").fetchall()
    return [_out(dict(r)) for r in rows]


@router.get("/{profile_id}")
def get_profile(profile_id: str, conn=Depends(get_db)):
    return _out(get_or_404(conn, "capability_profiles", profile_id, "能力档案"))


@router.patch("/{profile_id}")
def patch_profile(profile_id: str, body: CapabilityProfilePatch, conn=Depends(get_db)):
    row = get_or_404(conn, "capability_profiles", profile_id, "能力档案")
    data = body.model_dump(exclude_unset=True)
    if not data:
        return _out(row) | {"stale_steps": 0}
    merged = _out(row) | data
    conn.execute(
        "UPDATE capability_profiles"
        " SET name=?, usable_hand=?, foot_pedal=?, single_key_switch=?, voice_confirmation=?"
        " WHERE id=?",
        (merged["name"], merged["usable_hand"], int(merged["foot_pedal"]),
         int(merged["single_key_switch"]), int(merged["voice_confirmation"]), profile_id),
    )
    # 能力变化影响所有依赖该档案的待执行步骤
    stale = mark_steps_stale(conn, [profile_id])
    conn.commit()
    return _out(get_or_404(conn, "capability_profiles", profile_id, "能力档案")) | {"stale_steps": stale}
