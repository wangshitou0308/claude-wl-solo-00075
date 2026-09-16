"""标本册登记：册页次序、装订方向、易碎区。资料变更只使受影响步骤过期。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from ..deps import get_db
from ..schemas import FragileZoneIn, PagesReplace, VolumeIn, VolumePatch
from ..util import get_or_404, mark_steps_stale, new_id, now_iso

router = APIRouter(prefix="/volumes", tags=["标本册"])


def _volume_out(conn, row: dict) -> dict:
    pages = [dict(r) for r in conn.execute(
        "SELECT * FROM pages WHERE volume_id = ? ORDER BY seq", (row["id"],)).fetchall()]
    for page in pages:
        page["has_interleaf"] = bool(page["has_interleaf"])
    zones = [dict(r) for r in conn.execute(
        "SELECT * FROM fragile_zones WHERE volume_id = ?", (row["id"],)).fetchall()]
    return {**row, "pages": pages, "fragile_zones": zones}


@router.post("", status_code=201)
def create_volume(body: VolumeIn, conn=Depends(get_db)):
    volume_id = new_id()
    conn.execute(
        "INSERT INTO volumes (id, name, binding_direction, created_at) VALUES (?,?,?,?)",
        (volume_id, body.name, body.binding_direction, now_iso()),
    )
    conn.commit()
    return _volume_out(conn, get_or_404(conn, "volumes", volume_id, "标本册"))


@router.get("")
def list_volumes(conn=Depends(get_db)):
    rows = conn.execute("SELECT * FROM volumes ORDER BY created_at").fetchall()
    return [_volume_out(conn, dict(r)) for r in rows]


@router.get("/{volume_id}")
def get_volume(volume_id: str, conn=Depends(get_db)):
    return _volume_out(conn, get_or_404(conn, "volumes", volume_id, "标本册"))


@router.patch("/{volume_id}")
def patch_volume(volume_id: str, body: VolumePatch, conn=Depends(get_db)):
    row = get_or_404(conn, "volumes", volume_id, "标本册")
    data = body.model_dump(exclude_unset=True)
    stale = 0
    if data:
        binding_changed = ("binding_direction" in data
                           and data["binding_direction"] != row["binding_direction"])
        conn.execute(
            "UPDATE volumes SET name = ?, binding_direction = ? WHERE id = ?",
            (data.get("name", row["name"]),
             data.get("binding_direction", row["binding_direction"]), volume_id),
        )
        if binding_changed:
            # 装订方向改变固定/翻页方式，影响该册全部待执行步骤
            stale = mark_steps_stale(conn, [volume_id])
        conn.commit()
    return _volume_out(conn, get_or_404(conn, "volumes", volume_id, "标本册")) | {"stale_steps": stale}


@router.put("/{volume_id}/pages")
def replace_pages(volume_id: str, body: PagesReplace, conn=Depends(get_db)):
    """整体替换册页次序：按 seq 就地更新，内容变化的页才使对应步骤过期。"""
    get_or_404(conn, "volumes", volume_id, "标本册")
    old_rows = {r["seq"]: dict(r) for r in conn.execute(
        "SELECT * FROM pages WHERE volume_id = ?", (volume_id,)).fetchall()}
    changed: list[str] = []
    for seq, page in enumerate(body.pages):
        old = old_rows.get(seq)
        if old is None:
            conn.execute(
                "INSERT INTO pages (id, volume_id, seq, label, side, has_interleaf)"
                " VALUES (?,?,?,?,?,?)",
                (new_id(), volume_id, seq, page.label, page.side, int(page.has_interleaf)),
            )
        elif (old["label"] != page.label or old["side"] != page.side
              or bool(old["has_interleaf"]) != page.has_interleaf):
            conn.execute(
                "UPDATE pages SET label = ?, side = ?, has_interleaf = ? WHERE id = ?",
                (page.label, page.side, int(page.has_interleaf), old["id"]),
            )
            changed.append(old["id"])
    for seq, old in old_rows.items():
        if seq >= len(body.pages):
            conn.execute("DELETE FROM pages WHERE id = ?", (old["id"],))
            changed.append(old["id"])
    if len(old_rows) != len(body.pages):
        # 页数变化影响复位/收尾等全部后续步骤
        changed.append(volume_id)
    stale = mark_steps_stale(conn, changed)
    conn.commit()
    return _volume_out(conn, get_or_404(conn, "volumes", volume_id, "标本册")) | {"stale_steps": stale}


@router.post("/{volume_id}/fragile-zones", status_code=201)
def add_fragile_zone(volume_id: str, body: FragileZoneIn, conn=Depends(get_db)):
    get_or_404(conn, "volumes", volume_id, "标本册")
    page = get_or_404(conn, "pages", body.page_id, "册页")
    if page["volume_id"] != volume_id:
        raise HTTPException(status_code=422, detail="册页不属于该标本册")
    zone_id = new_id()
    conn.execute(
        "INSERT INTO fragile_zones (id, volume_id, page_id, x, y, w, h, note) VALUES (?,?,?,?,?,?,?,?)",
        (zone_id, volume_id, body.page_id, body.x, body.y, body.w, body.h, body.note),
    )
    # 只使依赖该页易碎区的待执行步骤过期
    stale = mark_steps_stale(conn, [f"zones:{body.page_id}"])
    conn.commit()
    return get_or_404(conn, "fragile_zones", zone_id, "易碎区") | {"stale_steps": stale}


@router.delete("/{volume_id}/fragile-zones/{zone_id}")
def delete_fragile_zone(volume_id: str, zone_id: str, conn=Depends(get_db)):
    zone = get_or_404(conn, "fragile_zones", zone_id, "易碎区")
    if zone["volume_id"] != volume_id:
        raise HTTPException(status_code=404, detail="易碎区不属于该标本册")
    conn.execute("DELETE FROM fragile_zones WHERE id = ?", (zone_id,))
    stale = mark_steps_stale(conn, [f"zones:{zone['page_id']}"])
    conn.commit()
    return {"deleted": zone_id, "stale_steps": stale}
