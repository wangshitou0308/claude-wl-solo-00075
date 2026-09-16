"""API 入参模型（Pydantic）。"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

Hand = Literal["left", "right"]
BindingDirection = Literal["left", "right", "top"]
Side = Literal["recto", "verso"]
EquipmentKind = Literal["clamp", "pressure_bar", "turning_pad", "camera_remote"]
EventSource = Literal["single_key", "foot_pedal", "voice", "hand"]


class CapabilityProfileIn(BaseModel):
    """能力档案：不假定能力相同，逐项登记可用通道。"""

    name: str = Field(min_length=1, max_length=100)
    usable_hand: Hand | None = None
    foot_pedal: bool = False
    single_key_switch: bool = False
    voice_confirmation: bool = False


class CapabilityProfilePatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    usable_hand: Hand | None = None
    foot_pedal: bool | None = None
    single_key_switch: bool | None = None
    voice_confirmation: bool | None = None


class VolumeIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    binding_direction: BindingDirection


class VolumePatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    binding_direction: BindingDirection | None = None


class PageIn(BaseModel):
    label: str = Field(min_length=1, max_length=100)
    side: Side = "recto"
    has_interleaf: bool = False


class PagesReplace(BaseModel):
    """册页次序：列表顺序即翻拍顺序。"""

    pages: list[PageIn] = Field(min_length=1)


class FragileZoneIn(BaseModel):
    """易碎区：页面归一化坐标（0~1）下的矩形，压条不得落入。"""

    page_id: str
    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)
    w: float = Field(gt=0, le=1)
    h: float = Field(gt=0, le=1)
    note: str | None = None


class EquipmentIn(BaseModel):
    kind: EquipmentKind
    name: str = Field(min_length=1, max_length=100)
    attributes: dict[str, Any] = Field(default_factory=dict)


class EquipmentPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    attributes: dict[str, Any] | None = None


class PlanIn(BaseModel):
    volume_id: str
    profile_id: str
    equipment_ids: list[str] = Field(min_length=1)


class SessionIn(BaseModel):
    plan_id: str


class Checks(BaseModel):
    """拍摄前后核对：页码、正反面、比例尺，三项全部符合才算通过。"""

    page_number: bool
    side: bool
    scale: bool


class EventIn(BaseModel):
    """统一确认/撤回事件：单键顺序扫描、脚踏、语音、徒手均走此接口。

    client_event_id 用于开关抖动/重试的幂等去重。
    """

    type: Literal["confirm", "withdraw"]
    source: EventSource = "hand"
    client_event_id: str | None = Field(default=None, max_length=100)
    checks: Checks | None = None


class ExceptionIn(BaseModel):
    kind: str = Field(min_length=1, max_length=50)
    detail: str | None = None
