"""执行会话：统一确认/撤回接口、异常重排、过期重排、完成版冻结。"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException

from ..deps import get_db
from ..hashing import sha256
from ..planner import CHECK_KINDS, generate_steps, profile_channels, resume_position
from ..runtime import (current_step, fetch_steps, insert_steps, load_plan_context,
                       progress_of, step_out)
from ..schemas import EventIn, ExceptionIn, SessionIn
from ..util import get_or_404, jdump, new_id, now_iso

router = APIRouter(prefix="/sessions", tags=["执行会话"])

# 事件来源 -> 能力通道：单键顺序扫描、脚踏、语音与徒手走同一确认/撤回接口
SOURCE_CHANNEL = {
    "single_key": "single_key",
    "foot_pedal": "foot_pedal",
    "voice": "voice",
    "hand": "hand",
}


# ---------------------------------------------------------------- 辅助

def _session_state(conn, session_id: str) -> dict:
    session = get_or_404(conn, "sessions", session_id, "执行会话")
    steps = fetch_steps(conn, session["plan_id"])
    current = current_step(steps)
    out = {
        "id": session["id"],
        "plan_id": session["plan_id"],
        "status": session["status"],
        "created_at": session["created_at"],
        "completed_at": session["completed_at"],
        "progress": progress_of(steps),
        "current_step": step_out(current) if current else None,
        "available_actions": ["confirm", "withdraw"] if session["status"] == "active" else [],
    }
    if session["status"] == "completed":
        out["frozen_path"] = json.loads(session["frozen_path"])
        out["input_hash"] = session["input_hash"]
    return out


def _record_event(conn, session_id: str, event_type: str, *, source: str | None = None,
                  step: dict | None = None, payload: dict | None = None,
                  client_event_id: str | None = None) -> dict:
    seq = conn.execute(
        "SELECT COALESCE(MAX(seq), 0) + 1 AS n FROM events WHERE session_id = ?",
        (session_id,)).fetchone()["n"]
    event_id = new_id()
    conn.execute(
        "INSERT INTO events (id, session_id, seq, type, source, step_id, step_kind, page_label,"
        "                    payload, client_event_id, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (event_id, session_id, seq, event_type, source,
         step["id"] if step else None, step["kind"] if step else None,
         step["page_label"] if step else None,
         jdump(payload) if payload is not None else None, client_event_id, now_iso()),
    )
    return {"id": event_id, "seq": seq}


def _freeze_session(conn, session: dict) -> None:
    """完成版冻结：实际路径（含撤回/异常/重排痕迹）与全部输入哈希定稿，之后不可再写。"""
    events = conn.execute(
        "SELECT * FROM events WHERE session_id = ? ORDER BY seq", (session["id"],)).fetchall()
    steps = conn.execute(
        "SELECT * FROM steps WHERE plan_id = ? ORDER BY seq", (session["plan_id"],)).fetchall()
    path = [
        {
            "seq": e["seq"], "type": e["type"], "source": e["source"],
            "step_id": e["step_id"], "step_kind": e["step_kind"],
            "page_label": e["page_label"], "at": e["created_at"],
        }
        for e in events
    ]
    digest = sha256({
        "plan_id": session["plan_id"],
        "steps": [
            {"id": s["id"], "kind": s["kind"], "input_hash": s["input_hash"], "status": s["status"]}
            for s in steps
        ],
        "events": [
            {"seq": e["seq"], "type": e["type"], "source": e["source"], "step_id": e["step_id"]}
            for e in events
        ],
    })
    conn.execute(
        "UPDATE sessions SET status = 'completed', completed_at = ?, frozen_path = ?, input_hash = ?"
        " WHERE id = ?",
        (now_iso(), jdump(path), digest, session["id"]),
    )
    conn.execute("UPDATE plans SET status = 'completed' WHERE id = ?", (session["plan_id"],))


def _active_session(conn, session_id: str) -> dict:
    session = get_or_404(conn, "sessions", session_id, "执行会话")
    if session["status"] != "active":
        raise HTTPException(status_code=409, detail="执行会话已完成并冻结，实际路径与输入哈希不可再变更")
    return session


# ---------------------------------------------------------------- 会话生命周期

@router.post("", status_code=201)
def create_session(body: SessionIn, conn=Depends(get_db)):
    plan = get_or_404(conn, "plans", body.plan_id, "翻拍计划")
    if plan["status"] == "completed":
        raise HTTPException(status_code=409, detail="该计划已完成并冻结，请重新创建计划")
    active = conn.execute(
        "SELECT id FROM sessions WHERE plan_id = ? AND status = 'active'", (plan["id"],)).fetchone()
    if active:
        raise HTTPException(status_code=409, detail="该计划已有进行中的执行会话")
    steps = fetch_steps(conn, plan["id"])
    if any(s["status"] == "stale" for s in steps):
        # 会话尚未开始、资料已变化：直接用最新资料整体重排
        volume, profile, equipment, pages, zones = load_plan_context(conn, plan)
        drafts = generate_steps(volume, profile, equipment, pages, zones)
        conn.execute("DELETE FROM steps WHERE plan_id = ?", (plan["id"],))
        insert_steps(conn, plan["id"], drafts, 0)
    session_id = new_id()
    conn.execute(
        "INSERT INTO sessions (id, plan_id, status, created_at) VALUES (?,?, 'active', ?)",
        (session_id, plan["id"], now_iso()),
    )
    conn.execute("UPDATE plans SET status = 'active' WHERE id = ?", (plan["id"],))
    conn.commit()
    return _session_state(conn, session_id)


@router.get("/{session_id}")
def get_session(session_id: str, conn=Depends(get_db)):
    return _session_state(conn, session_id)


@router.get("/{session_id}/events")
def list_events(session_id: str, conn=Depends(get_db)):
    get_or_404(conn, "sessions", session_id, "执行会话")
    rows = conn.execute(
        "SELECT * FROM events WHERE session_id = ? ORDER BY seq", (session_id,)).fetchall()
    return [
        {
            "seq": r["seq"], "type": r["type"], "source": r["source"],
            "step_id": r["step_id"], "step_kind": r["step_kind"], "page_label": r["page_label"],
            "payload": json.loads(r["payload"]) if r["payload"] else None,
            "client_event_id": r["client_event_id"], "created_at": r["created_at"],
        }
        for r in rows
    ]


# ---------------------------------------------------------------- 统一确认/撤回

@router.post("/{session_id}/events")
def post_event(session_id: str, body: EventIn, conn=Depends(get_db)):
    session = _active_session(conn, session_id)
    if body.client_event_id:
        dup = conn.execute(
            "SELECT result FROM events WHERE session_id = ? AND client_event_id = ?",
            (session_id, body.client_event_id)).fetchone()
        if dup is not None and dup["result"]:
            result = json.loads(dup["result"])
            result["duplicate"] = True
            return result
    plan = get_or_404(conn, "plans", session["plan_id"], "翻拍计划")
    profile = get_or_404(conn, "capability_profiles", plan["profile_id"], "能力档案")
    channel = SOURCE_CHANNEL[body.source]
    if channel not in profile_channels(profile):
        raise HTTPException(
            status_code=409,
            detail=f"能力档案「{profile['name']}」未登记 {body.source} 输入方式")
    steps = fetch_steps(conn, plan["id"])
    if body.type == "confirm":
        result = _confirm(conn, session, steps, body)
    else:
        result = _withdraw(conn, session, steps, body)
    conn.commit()
    return result


def _confirm(conn, session: dict, steps: list[dict], body: EventIn) -> dict:
    current = current_step(steps)
    if current is None:
        raise HTTPException(status_code=409, detail="没有待执行的步骤")
    if current["status"] == "stale":
        raise HTTPException(
            status_code=409,
            detail="当前步骤已因资料变更过期，请先调用 POST /sessions/{id}/replan 重排")
    if current["kind"] in CHECK_KINDS:
        checks = body.checks
        if checks is None or not (checks.page_number and checks.side and checks.scale):
            _record_event(conn, session["id"], "check_failed", source=body.source, step=current,
                          payload={"checks": checks.model_dump() if checks else None})
            conn.commit()  # 核对失败同样入档
            raise HTTPException(
                status_code=409,
                detail="核对未通过：页码、正反面、比例尺须全部符合；"
                       "若现场状态异常请调用 /exceptions 从最近安全落点重排")
    conn.execute("UPDATE steps SET status = 'done' WHERE id = ?", (current["id"],))
    event = _record_event(
        conn, session["id"], "confirm", source=body.source, step=current,
        payload={"checks": body.checks.model_dump()} if body.checks else None,
        client_event_id=body.client_event_id)
    remaining = fetch_steps(conn, session["plan_id"])
    nxt = current_step(remaining)
    completed = nxt is None
    if completed:
        _freeze_session(conn, session)
    done_step = step_out(current)
    done_step["status"] = "done"
    result = {
        "session_id": session["id"],
        "session_status": "completed" if completed else "active",
        "event": {"seq": event["seq"], "type": "confirm", "source": body.source},
        "completed_step": done_step,
        "next_step": step_out(nxt) if nxt else None,
        "progress": progress_of(remaining),
    }
    if completed:
        row = conn.execute("SELECT input_hash FROM sessions WHERE id = ?",
                           (session["id"],)).fetchone()
        result["input_hash"] = row["input_hash"]
    conn.execute("UPDATE events SET result = ? WHERE id = ?", (jdump(result), event["id"]))
    return result


def _withdraw(conn, session: dict, steps: list[dict], body: EventIn) -> dict:
    done = [s for s in steps if s["status"] == "done"]
    if not done:
        raise HTTPException(status_code=409, detail="没有可撤回的已完成步骤")
    last = done[-1]
    conn.execute("UPDATE steps SET status = 'pending' WHERE id = ?", (last["id"],))
    event = _record_event(conn, session["id"], "withdraw", source=body.source, step=last,
                          client_event_id=body.client_event_id)
    remaining = fetch_steps(conn, session["plan_id"])
    reopened = step_out(last)
    reopened["status"] = "pending"
    result = {
        "session_id": session["id"],
        "session_status": "active",
        "event": {"seq": event["seq"], "type": "withdraw", "source": body.source},
        "reopened_step": reopened,
        "next_step": reopened,
        "progress": progress_of(remaining),
    }
    conn.execute("UPDATE events SET result = ? WHERE id = ?", (jdump(result), event["id"]))
    return result


# ---------------------------------------------------------------- 异常与重排

@router.post("/{session_id}/exceptions")
def raise_exception(session_id: str, body: ExceptionIn, conn=Depends(get_db)):
    """异常上报：回退到最近安全落点，从那里用最新资料重排后续步骤。"""
    session = _active_session(conn, session_id)
    plan = get_or_404(conn, "plans", session["plan_id"], "翻拍计划")
    steps = fetch_steps(conn, plan["id"])
    _record_event(conn, session_id, "exception",
                  payload={"kind": body.kind, "detail": body.detail})
    conn.commit()  # 异常先入档；重排失败（如资料已不可行）也不丢现场记录

    done = [s for s in steps if s["status"] == "done"]
    checkpoints = [s for s in done if s["checkpoint"]]
    volume, profile, equipment, pages, zones = load_plan_context(conn, plan)
    if checkpoints:
        anchor = checkpoints[-1]
        prefix = done[: done.index(anchor) + 1]
        start_index, start_phase = resume_position(prefix)
        drafts = generate_steps(volume, profile, equipment, pages, zones,
                                start_index=start_index, start_phase=start_phase,
                                initial_state=json.loads(anchor["support_state"]))
        removed = conn.execute("SELECT COUNT(*) AS n FROM steps WHERE plan_id = ? AND seq > ?",
                               (plan["id"], anchor["seq"])).fetchone()["n"]
        conn.execute("DELETE FROM steps WHERE plan_id = ? AND seq > ?", (plan["id"], anchor["seq"]))
        start_seq = anchor["seq"] + 1
        anchor_out = step_out(anchor)
    else:
        anchor = None
        drafts = generate_steps(volume, profile, equipment, pages, zones)
        removed = conn.execute("SELECT COUNT(*) AS n FROM steps WHERE plan_id = ?",
                               (plan["id"],)).fetchone()["n"]
        conn.execute("DELETE FROM steps WHERE plan_id = ?", (plan["id"],))
        start_seq = 0
        anchor_out = None
    insert_steps(conn, plan["id"], drafts, start_seq)
    _record_event(conn, session_id, "replan", step=anchor,
                  payload={"trigger": "exception",
                           "checkpoint_step_id": anchor["id"] if anchor else None,
                           "removed_steps": removed, "added_steps": len(drafts)})
    remaining = fetch_steps(conn, plan["id"])
    nxt = current_step(remaining)
    if nxt is None:
        _freeze_session(conn, session)
    conn.commit()
    return {
        "session_id": session_id,
        "session_status": "completed" if nxt is None else "active",
        "exception": {"kind": body.kind, "detail": body.detail},
        "checkpoint_step": anchor_out,
        "removed_steps": removed,
        "added_steps": len(drafts),
        "next_step": step_out(nxt) if nxt else None,
        "progress": progress_of(remaining),
    }


@router.post("/{session_id}/replan")
def replan(session_id: str, conn=Depends(get_db)):
    """过期重排：资料变更后，从当前位置用最新资料重排未执行步骤（已完成步骤保留）。"""
    session = _active_session(conn, session_id)
    plan = get_or_404(conn, "plans", session["plan_id"], "翻拍计划")
    steps = fetch_steps(conn, plan["id"])
    if not any(s["status"] == "stale" for s in steps):
        raise HTTPException(status_code=409, detail="没有过期步骤需要重排")
    done = [s for s in steps if s["status"] == "done"]
    volume, profile, equipment, pages, zones = load_plan_context(conn, plan)
    start_index, start_phase = resume_position(done)
    initial_state = json.loads(done[-1]["support_state"]) if done else None
    drafts = generate_steps(volume, profile, equipment, pages, zones,
                            start_index=start_index, start_phase=start_phase,
                            initial_state=initial_state)
    cut = done[-1]["seq"] + 1 if done else 0
    removed = conn.execute("SELECT COUNT(*) AS n FROM steps WHERE plan_id = ? AND seq >= ?",
                           (plan["id"], cut)).fetchone()["n"]
    conn.execute("DELETE FROM steps WHERE plan_id = ? AND seq >= ?", (plan["id"], cut))
    insert_steps(conn, plan["id"], drafts, cut)
    _record_event(conn, session_id, "replan",
                  step=done[-1] if done else None,
                  payload={"trigger": "stale", "removed_steps": removed, "added_steps": len(drafts)})
    remaining = fetch_steps(conn, plan["id"])
    nxt = current_step(remaining)
    if nxt is None:
        _freeze_session(conn, session)
    conn.commit()
    return {
        "session_id": session_id,
        "session_status": "completed" if nxt is None else "active",
        "removed_steps": removed,
        "added_steps": len(drafts),
        "next_step": step_out(nxt) if nxt else None,
        "progress": progress_of(remaining),
    }
