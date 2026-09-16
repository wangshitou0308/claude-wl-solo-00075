"""端到端测试：登记 → 编排 → 执行 → 异常/过期重排 → 完成冻结。"""
from __future__ import annotations

CHECKS_OK = {"page_number": True, "side": True, "scale": True}


# ------------------------------------------------------------------ 构造辅助

def make_profile(client, **overrides) -> str:
    body = {
        "name": "默认档案",
        "usable_hand": "right",
        "foot_pedal": True,
        "single_key_switch": True,
        "voice_confirmation": True,
    }
    body.update(overrides)
    resp = client.post("/capability-profiles", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def make_volume(client, pages=None, binding="left") -> str:
    resp = client.post("/volumes", json={"name": "腊叶标本册·甲", "binding_direction": binding})
    assert resp.status_code == 201, resp.text
    volume_id = resp.json()["id"]
    pages = pages if pages is not None else [
        {"label": "第1页", "side": "recto", "has_interleaf": True},
        {"label": "第2页", "side": "recto", "has_interleaf": False},
        {"label": "第3页", "side": "verso", "has_interleaf": False},
    ]
    resp = client.put(f"/volumes/{volume_id}/pages", json={"pages": pages})
    assert resp.status_code == 200, resp.text
    return volume_id


def add_zone(client, volume_id, page_index, x, y, w, h, note=None) -> dict:
    volume = client.get(f"/volumes/{volume_id}").json()
    page_id = volume["pages"][page_index]["id"]
    resp = client.post(f"/volumes/{volume_id}/fragile-zones",
                       json={"page_id": page_id, "x": x, "y": y, "w": w, "h": h, "note": note})
    assert resp.status_code == 201, resp.text
    return resp.json()


def make_equipment(client, remote_trigger="foot_pedal", bars=2) -> list[str]:
    ids = []

    def add(kind, name, attributes=None):
        resp = client.post("/equipment",
                           json={"kind": kind, "name": name, "attributes": attributes or {}})
        assert resp.status_code == 201, resp.text
        ids.append(resp.json()["id"])

    add("clamp", "夹具·A")
    add("turning_pad", "翻页垫·A")
    for i in range(bars):
        add("pressure_bar", f"压条·{chr(65 + i)}")
    add("camera_remote", "相机遥控·A", {"trigger": remote_trigger})
    return ids


def make_plan(client, profile_id=None, volume_id=None, equipment_ids=None,
              remote_trigger="foot_pedal") -> dict:
    profile_id = profile_id or make_profile(client)
    volume_id = volume_id or make_volume(client)
    equipment_ids = equipment_ids or make_equipment(client, remote_trigger=remote_trigger)
    resp = client.post("/plans", json={
        "volume_id": volume_id, "profile_id": profile_id, "equipment_ids": equipment_ids})
    assert resp.status_code == 201, resp.text
    return resp.json()


def start_session(client, plan_id) -> dict:
    resp = client.post("/sessions", json={"plan_id": plan_id})
    assert resp.status_code == 201, resp.text
    return resp.json()


def confirm_current(client, session_id, source="foot_pedal") -> dict:
    state = client.get(f"/sessions/{session_id}").json()
    current = state["current_step"]
    assert current is not None, "会话已完成，没有可确认的步骤"
    body = {"type": "confirm", "source": source}
    if current["kind"] in ("pre_shoot_check", "post_shoot_check"):
        body["checks"] = dict(CHECKS_OK)
    resp = client.post(f"/sessions/{session_id}/events", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


def run_to_completion(client, session_id, source="foot_pedal"):
    for _ in range(500):
        if client.get(f"/sessions/{session_id}").json()["status"] == "completed":
            return
        confirm_current(client, session_id, source=source)
    raise AssertionError("会话未能在限定步数内完成")


def overlap(a, b) -> bool:
    return not (a["x"] + a["w"] <= b["x"] or b["x"] + b["w"] <= a["x"]
                or a["y"] + a["h"] <= b["y"] or b["y"] + b["h"] <= a["y"])


# ------------------------------------------------------------------ 编排

def test_plan_pipeline_and_constraints(client):
    volume_id = make_volume(client)
    zone = {"x": 0.08, "y": 0.10, "w": 0.84, "h": 0.10}
    add_zone(client, volume_id, 1, **zone)  # 挡住第2页水平 15% 候选位
    plan = make_plan(client, volume_id=volume_id)
    steps = plan["steps"]
    kinds = [s["kind"] for s in steps]

    # 第1页（有隔页）：固定—揭隔页—展平×2—拍摄前核对—拍摄—复核—复位
    assert kinds[:8] == ["secure", "lift_interleaf", "flatten", "flatten",
                         "pre_shoot_check", "shoot", "post_shoot_check", "reset"]
    # 第2页无隔页 → 不编排揭隔页
    page2 = [s for s in steps if s["page_label"] == "第2页"]
    assert "lift_interleaf" not in [s["kind"] for s in page2]
    # 安全落点只出现在稳定相位
    assert {s["kind"] for s in steps if s["checkpoint"]} <= {
        "secure", "flatten", "post_shoot_check", "reset"}
    # 拍摄通道与能力档案匹配（脚踏）
    shoot = next(s for s in steps if s["kind"] == "shoot")
    assert shoot["channels"] == ["foot_pedal"]
    # 压条落位避开易碎区
    for s in page2:
        if s["kind"] == "flatten":
            assert not overlap(s["placement"]["rect"], zone)
    # 拍摄/核对时必须已有夹具或压条支撑（未获得稳定支撑不得松手）
    for s in steps:
        if s["kind"] in ("pre_shoot_check", "shoot", "post_shoot_check"):
            assert any(h.startswith(("clamp:", "bar:"))
                       for h in s["support_state"]["holders"])
    # 每步都带输入指纹
    assert all(s["input_hash"] for s in steps)


def test_plan_infeasible_without_hand(client):
    profile_id = make_profile(client, usable_hand=None)
    volume_id = make_volume(client)
    equipment_ids = make_equipment(client)
    resp = client.post("/plans", json={
        "volume_id": volume_id, "profile_id": profile_id, "equipment_ids": equipment_ids})
    assert resp.status_code == 422
    assert any("可用手" in r for r in resp.json()["detail"]["reasons"])


def test_plan_infeasible_when_remote_trigger_unmatched(client):
    profile_id = make_profile(client, usable_hand="left", foot_pedal=False,
                              single_key_switch=False, voice_confirmation=False)
    volume_id = make_volume(client)
    equipment_ids = make_equipment(client, remote_trigger="foot_pedal")
    resp = client.post("/plans", json={
        "volume_id": volume_id, "profile_id": profile_id, "equipment_ids": equipment_ids})
    assert resp.status_code == 422
    assert any("触发方式" in r for r in resp.json()["detail"]["reasons"])


def test_plan_infeasible_when_fragile_zone_covers_page(client):
    profile_id = make_profile(client)
    volume_id = make_volume(client)
    add_zone(client, volume_id, 0, 0.0, 0.0, 1.0, 1.0)
    equipment_ids = make_equipment(client)
    resp = client.post("/plans", json={
        "volume_id": volume_id, "profile_id": profile_id, "equipment_ids": equipment_ids})
    assert resp.status_code == 422
    reasons = resp.json()["detail"]["reasons"]
    assert any("易碎区" in r and "第1页" in r for r in reasons)


def test_camera_remote_requires_trigger(client):
    resp = client.post("/equipment", json={"kind": "camera_remote", "name": "遥控", "attributes": {}})
    assert resp.status_code == 422


# ------------------------------------------------------------------ 执行

def test_full_session_freezes_path_and_hash(client):
    plan = make_plan(client)
    session = start_session(client, plan["id"])
    sid = session["id"]
    assert session["current_step"]["kind"] == "secure"
    run_to_completion(client, sid)

    final = client.get(f"/sessions/{sid}").json()
    assert final["status"] == "completed"
    assert final["progress"]["done"] == final["progress"]["total"]
    assert final["input_hash"]
    path = final["frozen_path"]
    assert len(path) == final["progress"]["total"]
    assert all(e["type"] == "confirm" for e in path)
    # 冻结后拒绝任何写入
    resp = client.post(f"/sessions/{sid}/events", json={"type": "confirm", "source": "foot_pedal"})
    assert resp.status_code == 409
    resp = client.post(f"/sessions/{sid}/exceptions", json={"kind": "late"})
    assert resp.status_code == 409
    resp = client.post(f"/sessions/{sid}/replan")
    assert resp.status_code == 409


def test_check_steps_require_all_three_checks(client):
    plan = make_plan(client)
    sid = start_session(client, plan["id"])["id"]
    while client.get(f"/sessions/{sid}").json()["current_step"]["kind"] != "pre_shoot_check":
        confirm_current(client, sid)

    resp = client.post(f"/sessions/{sid}/events", json={"type": "confirm", "source": "voice"})
    assert resp.status_code == 409
    resp = client.post(f"/sessions/{sid}/events", json={
        "type": "confirm", "source": "voice",
        "checks": {"page_number": True, "side": True, "scale": False}})
    assert resp.status_code == 409
    events = client.get(f"/sessions/{sid}/events").json()
    assert any(e["type"] == "check_failed" for e in events)
    # 步骤未被推进
    assert client.get(f"/sessions/{sid}").json()["current_step"]["kind"] == "pre_shoot_check"

    resp = client.post(f"/sessions/{sid}/events",
                       json={"type": "confirm", "source": "voice", "checks": dict(CHECKS_OK)})
    assert resp.status_code == 200
    assert resp.json()["next_step"]["kind"] == "shoot"


def test_withdraw_reopens_last_step(client):
    plan = make_plan(client)
    sid = start_session(client, plan["id"])["id"]
    first = confirm_current(client, sid)
    second_kind = first["next_step"]["kind"]

    resp = client.post(f"/sessions/{sid}/events", json={"type": "withdraw", "source": "single_key"})
    assert resp.status_code == 200
    state = client.get(f"/sessions/{sid}").json()
    assert state["current_step"]["kind"] == "secure"
    assert state["progress"]["done"] == 0

    confirm_current(client, sid)
    assert client.get(f"/sessions/{sid}").json()["current_step"]["kind"] == second_kind


def test_unified_interface_accepts_all_sources(client):
    plan = make_plan(client)  # 档案登记了全部通道
    sid = start_session(client, plan["id"])["id"]
    for source in ("single_key", "foot_pedal", "voice", "hand"):
        confirm_current(client, sid, source=source)
    assert client.get(f"/sessions/{sid}").json()["progress"]["done"] == 4
    events = client.get(f"/sessions/{sid}/events").json()
    assert [e["source"] for e in events] == ["single_key", "foot_pedal", "voice", "hand"]


def test_event_source_must_match_profile(client):
    profile_id = make_profile(client, foot_pedal=False, single_key_switch=False,
                              voice_confirmation=False)
    plan = make_plan(client, profile_id=profile_id, remote_trigger="hand")
    sid = start_session(client, plan["id"])["id"]
    resp = client.post(f"/sessions/{sid}/events", json={"type": "confirm", "source": "foot_pedal"})
    assert resp.status_code == 409
    resp = client.post(f"/sessions/{sid}/events", json={"type": "confirm", "source": "hand"})
    assert resp.status_code == 200


def test_idempotent_event_replay(client):
    plan = make_plan(client)
    sid = start_session(client, plan["id"])["id"]
    body = {"type": "confirm", "source": "foot_pedal", "client_event_id": "sw-0001"}
    first = client.post(f"/sessions/{sid}/events", json=body)
    assert first.status_code == 200
    replay = client.post(f"/sessions/{sid}/events", json=body)
    assert replay.status_code == 200
    assert replay.json()["duplicate"] is True
    assert client.get(f"/sessions/{sid}").json()["progress"]["done"] == 1


# ------------------------------------------------------------------ 异常重排

def test_exception_replans_from_nearest_checkpoint(client):
    plan = make_plan(client)
    sid = start_session(client, plan["id"])["id"]
    for _ in range(4):  # secure / lift_interleaf / flatten ×2（第1页）
        confirm_current(client, sid)
    assert client.get(f"/sessions/{sid}").json()["current_step"]["kind"] == "pre_shoot_check"

    before = client.get(f"/plans/{plan['id']}").json()["steps"]
    resp = client.post(f"/sessions/{sid}/exceptions",
                       json={"kind": "page_slipped", "detail": "压条滑脱"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # 最近安全落点 = 最后一个 flatten；从那里重排
    assert body["checkpoint_step"]["kind"] == "flatten"
    assert body["next_step"]["kind"] == "pre_shoot_check"
    anchor_seq = body["checkpoint_step"]["seq"]

    after = client.get(f"/plans/{plan['id']}").json()["steps"]
    assert [s["id"] for s in after if s["seq"] <= anchor_seq] == \
           [s["id"] for s in before if s["seq"] <= anchor_seq]
    assert {s["id"] for s in after if s["seq"] > anchor_seq}.isdisjoint(
        {s["id"] for s in before if s["seq"] > anchor_seq})
    # 事件流留下异常与重排痕迹
    types = [e["type"] for e in client.get(f"/sessions/{sid}/events").json()]
    assert "exception" in types and "replan" in types
    run_to_completion(client, sid)


def test_exception_before_any_step_replans_everything(client):
    plan = make_plan(client)
    sid = start_session(client, plan["id"])["id"]
    resp = client.post(f"/sessions/{sid}/exceptions", json={"kind": "bump"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["checkpoint_step"] is None
    assert body["next_step"]["kind"] == "secure"
    run_to_completion(client, sid)


# ------------------------------------------------------------------ 资料变更与过期

def test_data_change_expires_only_affected_steps(client):
    plan = make_plan(client)
    sid = start_session(client, plan["id"])["id"]
    confirm_current(client, sid)  # 第1页 secure 完成

    zone = {"x": 0.0, "y": 0.0, "w": 0.5, "h": 0.5}
    resp = add_zone(client, plan["volume_id"], 1, **zone)  # 第2页新增易碎区
    assert resp["stale_steps"] > 0

    steps = client.get(f"/plans/{plan['id']}").json()["steps"]
    page1_pending = [s for s in steps if s["page_label"] == "第1页" and s["status"] != "done"]
    page2_steps = [s for s in steps if s["page_label"] == "第2页"]
    # 只波及第2页；第1页待执行步骤与已完成步骤都不受影响
    assert page1_pending and all(s["status"] == "pending" for s in page1_pending)
    assert page2_steps and all(s["status"] == "stale" for s in page2_steps)
    secure = next(s for s in steps if s["kind"] == "secure")
    assert secure["status"] == "done"

    # 推进到第2页：过期步骤挡下确认 → 重排 → 新落位避开新易碎区 → 走完
    while client.get(f"/sessions/{sid}").json()["current_step"]["page_label"] != "第2页":
        confirm_current(client, sid)
    resp = client.post(f"/sessions/{sid}/events", json={"type": "confirm", "source": "hand"})
    assert resp.status_code == 409
    resp = client.post(f"/sessions/{sid}/replan")
    assert resp.status_code == 200, resp.text

    steps = client.get(f"/plans/{plan['id']}").json()["steps"]
    for s in steps:
        if s["kind"] == "flatten" and s["page_label"] == "第2页":
            assert not overlap(s["placement"]["rect"], zone)
    run_to_completion(client, sid)


def test_page_content_change_expires_only_that_page(client):
    plan = make_plan(client)
    start_session(client, plan["id"])
    volume = client.get(f"/volumes/{plan['volume_id']}").json()
    pages = [{"label": p["label"], "side": p["side"], "has_interleaf": p["has_interleaf"]}
             for p in volume["pages"]]
    pages[2]["label"] = "第3页（修订）"
    resp = client.put(f"/volumes/{plan['volume_id']}/pages", json={"pages": pages})
    assert resp.json()["stale_steps"] > 0

    steps = client.get(f"/plans/{plan['id']}").json()["steps"]
    assert all(s["status"] == "pending" for s in steps if s["page_label"] == "第1页")
    page3 = [s for s in steps if s["page_label"] == "第3页"]
    assert page3 and all(s["status"] == "stale" for s in page3)


def test_profile_change_expires_dependent_steps(client):
    plan = make_plan(client)
    start_session(client, plan["id"])
    resp = client.patch(f"/capability-profiles/{plan['profile_id']}",
                        json={"voice_confirmation": False})
    assert resp.json()["stale_steps"] > 0
    steps = client.get(f"/plans/{plan['id']}").json()["steps"]
    assert all(s["status"] == "stale" for s in steps)  # 档案影响全部待执行步骤


def test_session_start_autoreplans_stale_plan(client):
    plan = make_plan(client)
    # 会话开始前资料变化 → 步骤过期；开启会话时自动用最新资料重排
    add_zone(client, plan["volume_id"], 0, 0.0, 0.0, 0.4, 0.4)
    steps = client.get(f"/plans/{plan['id']}").json()["steps"]
    assert any(s["status"] == "stale" for s in steps)
    session = start_session(client, plan["id"])
    assert session["current_step"]["kind"] == "secure"
    steps = client.get(f"/plans/{plan['id']}").json()["steps"]
    assert all(s["status"] == "pending" for s in steps)
    run_to_completion(client, session["id"])
