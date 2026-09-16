"""计划/会话共用的存取与序列化辅助。"""
from __future__ import annotations

import json
import sqlite3

from .util import get_or_404, jdump, new_id


def fetch_steps(conn: sqlite3.Connection, plan_id: str) -> list[dict]:
    rows = conn.execute("SELECT * FROM steps WHERE plan_id = ? ORDER BY seq", (plan_id,)).fetchall()
    return [dict(r) for r in rows]


def step_out(step: dict) -> dict:
    out = dict(step)
    out["channels"] = json.loads(out["channels"])
    out["depends_on"] = json.loads(out["depends_on"])
    out["support_state"] = json.loads(out["support_state"])
    out["placement"] = json.loads(out["placement"]) if out["placement"] else None
    out["checkpoint"] = bool(out["checkpoint"])
    return out


def current_step(steps: list[dict]) -> dict | None:
    """执行指针：第一个未完成的步骤（done 始终是序列前缀）。"""
    for step in steps:
        if step["status"] != "done":
            return step
    return None


def progress_of(steps: list[dict]) -> dict:
    done = sum(1 for s in steps if s["status"] == "done")
    return {"done": done, "total": len(steps)}


def insert_steps(conn: sqlite3.Connection, plan_id: str, drafts: list[dict], start_seq: int) -> None:
    for offset, draft in enumerate(drafts):
        conn.execute(
            """
            INSERT INTO steps (id, plan_id, seq, kind, page_id, page_seq, page_label, side,
                               instruction, channels, placement, support_state, checkpoint,
                               depends_on, input_hash, status)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'pending')
            """,
            (
                new_id(), plan_id, start_seq + offset, draft["kind"], draft["page_id"],
                draft["page_seq"], draft["page_label"], draft["side"], draft["instruction"],
                jdump(draft["channels"]),
                jdump(draft["placement"]) if draft["placement"] else None,
                jdump(draft["support_state"]), 1 if draft["checkpoint"] else 0,
                jdump(draft["depends_on"]), draft["input_hash"],
            ),
        )


def load_plan_context(conn: sqlite3.Connection, plan: dict):
    """读取重排所需的最新资料（卷、档案、器材、册页、易碎区）。"""
    volume = get_or_404(conn, "volumes", plan["volume_id"], "标本册")
    profile = get_or_404(conn, "capability_profiles", plan["profile_id"], "能力档案")
    equipment: list[dict] = []
    for equipment_id in json.loads(plan["equipment_ids"]):
        row = conn.execute("SELECT * FROM equipment WHERE id = ?", (equipment_id,)).fetchone()
        if row is not None:
            item = dict(row)
            item["attributes"] = json.loads(item["attributes"])
            equipment.append(item)
    pages = [dict(r) for r in conn.execute(
        "SELECT * FROM pages WHERE volume_id = ? ORDER BY seq", (plan["volume_id"],)).fetchall()]
    zones_by_page: dict[str, list[dict]] = {}
    for zone in conn.execute("SELECT * FROM fragile_zones WHERE volume_id = ?",
                             (plan["volume_id"],)).fetchall():
        zones_by_page.setdefault(zone["page_id"], []).append(
            {"x": zone["x"], "y": zone["y"], "w": zone["w"], "h": zone["h"]})
    return volume, profile, equipment, pages, zones_by_page
