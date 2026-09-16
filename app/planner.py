"""翻拍动作编排器。

把“固定—揭隔页—展平—拍摄—复核—复位”流程展开为逐步指令，并保证：

1. 任何时刻只安排能力档案可完成的动作（通道匹配，否则整体判定不可行）；
2. 压条落位避开易碎区（候选位扫描 + 矩形相交检测）；
3. 未获得稳定支撑不得松手或翻页（支撑不变量由 validate_support 校验）；
4. 拍摄前后强制核对页码、正反面、比例尺（核对步骤需三项全部确认）。
"""
from __future__ import annotations

from .hashing import sha256

# 流程相位（每页按此顺序推进；无隔页的页跳过 lift_interleaf）
PHASES = ["secure", "lift_interleaf", "flatten", "pre_shoot_check", "shoot", "post_shoot_check", "reset"]

# 拍摄前后核对步骤（确认时必须携带三项核对结果）
CHECK_KINDS = {"pre_shoot_check", "post_shoot_check"}

# 安全落点：这些步骤完成后标本处于稳定状态，异常后从最近的落点重排
CHECKPOINT_KINDS = {"secure", "flatten", "post_shoot_check", "reset"}

NEXT_PHASE = {
    "secure": "lift_interleaf",
    "lift_interleaf": "flatten",
    "flatten": "pre_shoot_check",
    "pre_shoot_check": "shoot",
    "shoot": "post_shoot_check",
    "post_shoot_check": "reset",
    "reset": None,
}

HAND_NAMES = {"left": "左手", "right": "右手"}
SIDE_NAMES = {"recto": "正面", "verso": "背面"}
TRIGGER_CHANNELS = ("hand", "foot_pedal", "single_key", "voice")
TRIGGER_NAMES = {"hand": "可用手", "foot_pedal": "脚踏开关", "single_key": "单键开关", "voice": "语音指令"}
# 装订方向 -> 切口侧（夹具固定在切口侧，避开书脊）
FORE_EDGE = {"left": "右", "right": "左", "top": "下"}

# 压条候选落位（归一化坐标，按优先级扫描）
BAR_CANDIDATE_OFFSETS = (0.15, 0.85, 0.30, 0.70, 0.50)
DEFAULT_BAR_WIDTH = 0.04


class PlanInfeasible(Exception):
    """能力档案 / 器材 / 易碎区约束下无法编排安全动作序列。"""

    def __init__(self, reasons: list[str]):
        super().__init__("；".join(reasons))
        self.reasons = reasons


def profile_channels(profile: dict) -> set[str]:
    """能力档案可提供的输入通道。"""
    channels: set[str] = set()
    if profile.get("usable_hand"):
        channels.add("hand")
    if profile.get("foot_pedal"):
        channels.add("foot_pedal")
    if profile.get("single_key_switch"):
        channels.add("single_key")
    if profile.get("voice_confirmation"):
        channels.add("voice")
    return channels


def _rects_overlap(a: dict, b: dict) -> bool:
    return not (
        a["x"] + a["w"] <= b["x"]
        or b["x"] + b["w"] <= a["x"]
        or a["y"] + a["h"] <= b["y"]
        or b["y"] + b["h"] <= a["y"]
    )


def _bar_rect(orientation: str, offset: float, width: float) -> dict:
    half = width / 2
    if orientation == "horizontal":
        return {"x": 0.08, "y": round(offset - half, 4), "w": 0.84, "h": width}
    return {"x": round(offset - half, 4), "y": 0.08, "w": width, "h": 0.84}


def find_bar_placements(zones: list[dict], count: int, width: float) -> list[dict]:
    """在归一化页面上挑选不落入易碎区、互不重叠的压条落位（最多 count 个）。"""
    placements: list[dict] = []
    for orientation in ("horizontal", "vertical"):
        for offset in BAR_CANDIDATE_OFFSETS:
            rect = _bar_rect(orientation, offset, width)
            if any(_rects_overlap(rect, zone) for zone in zones):
                continue
            if any(_rects_overlap(rect, placed["rect"]) for placed in placements):
                continue
            placements.append({"orientation": orientation, "offset": offset, "width": width, "rect": rect})
            if len(placements) >= count:
                return placements
    return placements


class PlanContext:
    """一次编排用到的静态资料快照。"""

    def __init__(self, volume: dict, profile: dict, equipment: list[dict]):
        self.volume = volume
        self.profile = profile
        self.channels = profile_channels(profile)
        by_kind: dict[str, list[dict]] = {}
        for item in equipment:
            by_kind.setdefault(item["kind"], []).append(item)
        self.clamps = by_kind.get("clamp", [])
        self.pads = by_kind.get("turning_pad", [])
        self.bars = by_kind.get("pressure_bar", [])
        self.remotes = by_kind.get("camera_remote", [])
        # 接力关键：拍摄时册页由夹具/压条支撑，通道空出来触发遥控
        self.remote = next(
            (r for r in self.remotes if r["attributes"].get("trigger") in self.channels), None
        )

    @property
    def bar_width(self) -> float:
        if not self.bars:
            return DEFAULT_BAR_WIDTH
        return float(self.bars[0]["attributes"].get("width", DEFAULT_BAR_WIDTH))


def check_feasibility(ctx: PlanContext, pages: list[dict], zones_by_page: dict[str, list[dict]]) -> None:
    """任何一步能力档案无法完成，都拒绝编排并说明原因。"""
    reasons: list[str] = []
    if "hand" not in ctx.channels:
        reasons.append("能力档案未登记可用手：固定、揭隔页、展平、复位等徒手工步无法编排")
    if not ctx.clamps:
        reasons.append("未选择夹具：无法固定标本册")
    if any(page["has_interleaf"] for page in pages) and not ctx.pads:
        reasons.append("存在带隔页的册页，但未选择翻页垫")
    if not ctx.bars:
        reasons.append("未选择压条：无法展平册页")
    if not ctx.remotes:
        reasons.append("未选择相机遥控")
    elif ctx.remote is None:
        triggers = "、".join(sorted({str(r["attributes"].get("trigger", "?")) for r in ctx.remotes}))
        reasons.append(f"相机遥控的触发方式（{triggers}）与能力档案的可用通道不匹配")
    if not ctx.channels:
        reasons.append("能力档案未登记任何确认通道（可用手/脚踏/单键/语音）")
    if reasons:
        raise PlanInfeasible(reasons)
    for page in pages:
        zones = zones_by_page.get(page["id"], [])
        if not find_bar_placements(zones, 1, ctx.bar_width):
            reasons.append(
                f"第 {page['seq'] + 1} 页「{page['label']}」的易碎区覆盖了全部候选压条位置，无法安全展平"
            )
    if reasons:
        raise PlanInfeasible(reasons)


class StepFactory:
    """按相位生成步骤草稿，并维护“支撑状态”模拟量（接力：支撑在手与器材间传递）。"""

    def __init__(self, ctx: PlanContext, zones_by_page: dict[str, list[dict]]):
        self.ctx = ctx
        self.zones_by_page = zones_by_page
        self.state = {"leaf": "flat", "holders": []}

    def reset_page_state(self) -> None:
        self.state = {"leaf": "flat", "holders": []}

    def _engage(self, holder: str) -> None:
        self.state["holders"] = [*self.state["holders"], holder]

    def _step(self, page: dict, kind: str, *, instruction: str, channels: list[str],
              equipment: list[dict] | None = None, placement: dict | None = None,
              checkpoint: bool = False, confirm_checks: bool = False) -> dict:
        ctx = self.ctx
        equipment = equipment or []
        depends = [ctx.volume["id"], ctx.profile["id"], page["id"], f"zones:{page['id']}"]
        depends += [item["id"] for item in equipment]
        fingerprint = {
            "kind": kind,
            "binding": ctx.volume["binding_direction"],
            "page": {
                "id": page["id"], "seq": page["seq"], "label": page["label"],
                "side": page["side"], "has_interleaf": bool(page["has_interleaf"]),
            },
            "profile": {k: ctx.profile[k] for k in
                        ("id", "usable_hand", "foot_pedal", "single_key_switch", "voice_confirmation")},
            "equipment": [
                {"id": e["id"], "kind": e["kind"], "name": e["name"], "attributes": e["attributes"]}
                for e in equipment
            ],
            "zones": self.zones_by_page.get(page["id"], []),
            "placement": placement,
        }
        return {
            "kind": kind,
            "page_id": page["id"],
            "page_seq": page["seq"],
            "page_label": page["label"],
            "side": page["side"],
            "instruction": instruction,
            "channels": list(channels),
            "placement": placement,
            "checkpoint": checkpoint,
            "confirm_checks": confirm_checks,
            "support_state": {"leaf": self.state["leaf"], "holders": list(self.state["holders"])},
            "depends_on": sorted(set(depends)),
            "input_hash": sha256(fingerprint),
        }

    def build(self, page: dict, phase: str, is_last: bool) -> list[dict]:
        return getattr(self, f"_build_{phase}")(page, is_last)

    # -- 各相位 --------------------------------------------------------

    def _build_secure(self, page: dict, is_last: bool) -> list[dict]:
        ctx = self.ctx
        hand = HAND_NAMES[ctx.profile["usable_hand"]]
        clamp = ctx.clamps[0]
        self._engage(f"clamp:{clamp['id']}")
        edge = FORE_EDGE[ctx.volume["binding_direction"]]
        return [self._step(
            page, "secure",
            instruction=(
                f"用{hand}将夹具「{clamp['name']}」夹住标本册{edge}侧切口，"
                f"固定册页「{page['label']}」（{SIDE_NAMES[page['side']]}）；夹具接管支撑后再松手"
            ),
            channels=["hand"], equipment=[clamp], checkpoint=True,
        )]

    def _build_lift_interleaf(self, page: dict, is_last: bool) -> list[dict]:
        ctx = self.ctx
        hand = HAND_NAMES[ctx.profile["usable_hand"]]
        pad = ctx.pads[0]
        self._engage(f"pad:{pad['id']}")
        return [self._step(
            page, "lift_interleaf",
            instruction=(
                f"用{hand}以翻页垫「{pad['name']}」将「{page['label']}」上的隔页挑起并挂牢，"
                "隔页改由翻页垫支撑后再松手"
            ),
            channels=["hand"], equipment=[pad],
        )]

    def _build_flatten(self, page: dict, is_last: bool) -> list[dict]:
        ctx = self.ctx
        hand = HAND_NAMES[ctx.profile["usable_hand"]]
        zones = self.zones_by_page.get(page["id"], [])
        placements = find_bar_placements(zones, min(2, len(ctx.bars)), ctx.bar_width)
        steps = []
        for index, placement in enumerate(placements):
            bar = ctx.bars[index % len(ctx.bars)]
            self._engage(f"bar:{bar['id']}#{index}")
            orientation = "水平" if placement["orientation"] == "horizontal" else "垂直"
            base = "上缘" if placement["orientation"] == "horizontal" else "左缘"
            steps.append(self._step(
                page, "flatten",
                instruction=(
                    f"用{hand}将压条「{bar['name']}」{orientation}放置于距{base} "
                    f"{round(placement['offset'] * 100)}% 处（已避开易碎区），压平「{page['label']}」"
                ),
                channels=["hand"], equipment=[bar], placement=placement,
                checkpoint=index == len(placements) - 1,
            ))
        return steps

    def _build_pre_shoot_check(self, page: dict, is_last: bool) -> list[dict]:
        return [self._step(
            page, "pre_shoot_check",
            instruction=(
                f"拍摄前核对：页码「{page['label']}」、{SIDE_NAMES[page['side']]}、"
                "比例尺均已进入画面；三项全部符合再确认"
            ),
            channels=[], confirm_checks=True,
        )]

    def _build_shoot(self, page: dict, is_last: bool) -> list[dict]:
        ctx = self.ctx
        remote = ctx.remote
        trigger = remote["attributes"]["trigger"]
        return [self._step(
            page, "shoot",
            instruction=(
                f"保持册页由夹具与压条稳定支撑，用{TRIGGER_NAMES[trigger]}"
                f"触发相机遥控「{remote['name']}」拍摄「{page['label']}」"
            ),
            channels=[trigger], equipment=[remote],
        )]

    def _build_post_shoot_check(self, page: dict, is_last: bool) -> list[dict]:
        return [self._step(
            page, "post_shoot_check",
            instruction="拍摄后复核：成片中的页码、正反面与比例尺清晰完整；三项全部符合再确认",
            channels=[], confirm_checks=True, checkpoint=True,
        )]

    def _build_reset(self, page: dict, is_last: bool) -> list[dict]:
        ctx = self.ctx
        hand = HAND_NAMES[ctx.profile["usable_hand"]]
        equipment = [*ctx.clamps[:1], *ctx.bars]
        if page["has_interleaf"] and ctx.pads:
            equipment.append(ctx.pads[0])
        interleaf = "，将隔页放回原位" if page["has_interleaf"] else ""
        if is_last:
            instruction = (
                f"用{hand}依次取下压条与夹具{interleaf}；"
                "确认册页平放稳定后再松手，合上标本册并归位器材"
            )
        else:
            instruction = (
                f"用{hand}依次取下压条与夹具{interleaf}；"
                f"托稳「{page['label']}」页面边缘翻至下一页，翻页全程保持支撑，页面落平后再松手"
            )
        # 复位完成：册页落平、支撑清空（下一页从稳定状态开始）
        self.reset_page_state()
        return [self._step(
            page, "reset", instruction=instruction,
            channels=["hand"], equipment=equipment, checkpoint=True,
        )]


def page_phases(page: dict) -> list[str]:
    return [p for p in PHASES if p != "lift_interleaf" or page["has_interleaf"]]


def validate_support(steps: list[dict]) -> None:
    """支撑不变量：册页悬空必须有支撑；拍摄/核对必须已有夹具或压条支撑。"""
    for step in steps:
        state = step["support_state"]
        if state["leaf"] == "lifted" and not state["holders"]:
            raise PlanInfeasible([f"步骤 {step['kind']} 结束时册页悬空且无任何支撑"])
        if step["kind"] in ("pre_shoot_check", "shoot", "post_shoot_check"):
            if not any(h.split(":", 1)[0] in ("clamp", "bar") for h in state["holders"]):
                raise PlanInfeasible(["拍摄/核对前未获得夹具或压条的稳定支撑"])


def generate_steps(volume: dict, profile: dict, equipment: list[dict], pages: list[dict],
                   zones_by_page: dict[str, list[dict]], start_index: int = 0,
                   start_phase: str = "secure", initial_state: dict | None = None) -> list[dict]:
    """从指定页与相位生成后续全部步骤（完整计划，或安全落点/当前位置之后的重排）。"""
    if start_index >= len(pages):
        return []
    ctx = PlanContext(volume, profile, equipment)
    check_feasibility(ctx, pages[start_index:], zones_by_page)
    factory = StepFactory(ctx, zones_by_page)
    if initial_state:
        factory.state = {
            "leaf": initial_state.get("leaf", "flat"),
            "holders": list(initial_state.get("holders", [])),
        }
    drafts: list[dict] = []
    last_index = len(pages) - 1
    for index in range(start_index, len(pages)):
        if index > start_index:
            factory.reset_page_state()
        phases = page_phases(pages[index])
        if index == start_index:
            if start_phase in phases:
                phases = phases[phases.index(start_phase):]
            else:
                phases = [p for p in phases if PHASES.index(p) > PHASES.index(start_phase)]
        for phase in phases:
            drafts.extend(factory.build(pages[index], phase, is_last=index == last_index))
    validate_support(drafts)
    return drafts


def resume_position(done_steps: list[dict]) -> tuple[int, str]:
    """根据已完成步骤推断重排起点：(页序号, 起始相位)。"""
    if not done_steps:
        return (0, "secure")
    last = done_steps[-1]
    nxt = NEXT_PHASE[last["kind"]]
    if nxt is None:
        return (last["page_seq"] + 1, "secure")
    return (last["page_seq"], nxt)
