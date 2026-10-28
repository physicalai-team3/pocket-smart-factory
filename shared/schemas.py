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
PrecisePolicy = Literal["uncertain", "interval", "off"]


class SessionConfig(BaseModel):
    roi: Optional[list[Point]] = None  # 좌상→우상→우하→좌하, 원본 프레임 비율
    line_x: float = Field(0.34, ge=0, le=1)  # 시뮬레이터 검사선(SCAN=0.34)과 일치
    direction: Literal["rtl", "ltr"] = "rtl"
    # 작은 정상 과자(C3 입력 지름 ~33px)도 원형도 0.92 이상 → 0.935 (docs/05-detection.md §4)
    circ_th: float = 0.935
    solid_th: float = 0.95
    # uncertain: C4 가 needs_precise 를 올린 트랙이 있을 때만 / interval: 주기적으로 / off
    precise_policy: PrecisePolicy = "uncertain"
    precise_interval_ms: int = 300  # precise 요청 사이 최소 간격
    # |circularity - circ_th| < band 이면 애매. 정상 과자 원형도 편차(작은 과자일수록 큼)를 덮지 않도록 좁게
    circ_band: float = 0.02
    solid_band: float = 0.02


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
    line_x: float = 0.34
    direction: Literal["rtl", "ltr"] = "rtl"
    # needs_precise 판단용 (SessionConfig 에서 그대로 복사)
    circ_th: float = 0.935
    solid_th: float = 0.95
    circ_band: float = 0.02
    solid_band: float = 0.02


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
    # 미카운트 · 비partial · precise 투표 없음 · (특징값이 임계값 band 안 or fast 투표가 갈림)
    needs_precise: bool = False


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
