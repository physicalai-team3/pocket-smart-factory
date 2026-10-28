"""서비스 간 메시지 계약 — docs/03-messages.md 와 항상 함께 수정한다."""
from __future__ import annotations

import json
import struct
from typing import Literal, Optional

from pydantic import BaseModel, Field

Point = tuple[float, float]
BBox = tuple[int, int, int, int]  # x, y, w, h (C2 출력 픽셀)
Mode = Literal["fast", "precise"]
Verdict = Literal["ok", "ng", "pending"]


# ── 바이너리 봉투: [uint32 BE header_len][header JSON][JPEG] ──
def pack_envelope(header: dict, jpeg: bytes) -> bytes:
    h = json.dumps(header, separators=(",", ":")).encode("utf-8")
    return struct.pack(">I", len(h)) + h + jpeg


def unpack_envelope(data: bytes) -> tuple[dict, bytes]:
    (n,) = struct.unpack(">I", data[:4])
    return json.loads(data[4 : 4 + n].decode("utf-8")), data[4 + n :]


# ── 세션 설정 (C1 보관, 단계별 요청에 실어 보냄) ──
class SessionConfig(BaseModel):
    roi: Optional[list[Point]] = None  # 좌상→우상→우하→좌하, 원본 프레임 비율
    line_x: float = Field(0.5, ge=0, le=1)
    direction: Literal["rtl", "ltr"] = "rtl"
    circ_th: float = 0.80
    solid_th: float = 0.95
    precise_interval_ms: int = 700


class Counts(BaseModel):
    total: int = 0
    ok: int = 0
    ng: int = 0


# ── C3 /infer 응답 ──
class InferObject(BaseModel):
    bbox: BBox
    centroid: Point
    area: float
    perimeter: float
    circularity: float
    solidity: float
    contour: list[tuple[int, int]]  # approxPolyDP, 최대 64점
    partial: bool = False
    defect: Optional[bool] = None  # partial 이면 None
    score: float = 1.0


class InferResult(BaseModel):
    session: str
    seq: int
    mode: Mode
    backend: str  # mock | opencv | sam2
    replica: str
    w: int
    h: int
    infer_ms: float
    objects: list[InferObject] = []


# ── C4 /track ──
class TrackConfig(BaseModel):
    line_x: float = 0.5
    direction: Literal["rtl", "ltr"] = "rtl"


class TrackRequest(BaseModel):
    session: str
    seq: int
    mode: Mode
    config: TrackConfig
    infer: InferResult
    image_jpeg_b64: Optional[str] = None  # precise 는 생략 가능


class Votes(BaseModel):
    fast_ok: int = 0
    fast_ng: int = 0
    precise_ok: int = 0
    precise_ng: int = 0


class TrackOut(BaseModel):
    track_id: int
    bbox: BBox
    verdict: Verdict
    counted: bool
    votes: Votes = Votes()


class CountedEvent(BaseModel):
    type: Literal["counted"] = "counted"
    track_id: int
    verdict: Literal["ok", "ng"]
    seq: int


class TrackResponse(BaseModel):
    session: str
    seq: int
    stale: bool = False
    counts: Counts
    tracks: list[TrackOut] = []
    events: list[CountedEvent] = []
    image_jpeg_b64: Optional[str] = None
    proc_ms: float = 0.0


# ── 공통 ──
class ErrorBody(BaseModel):
    error: str
    message: str = ""


class SessionInfo(BaseModel):
    session: str
    public_url: Optional[str]
    phone_url: Optional[str]
    qr_url: str
    created_at: float
