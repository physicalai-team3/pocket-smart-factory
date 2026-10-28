"""C1 세션 상태, WebSocket 피어, 지표 집계."""
from __future__ import annotations

import asyncio
import json
import secrets
import string
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

from fastapi import WebSocket

from shared.schemas import SessionConfig

ALPHABET = string.ascii_lowercase + string.digits
WINDOW_S = 2.0  # fps·드롭률 계산 구간


def now_ms() -> float:
    return time.time() * 1000


def new_session_id() -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(8))


class Peer:
    """WebSocket 하나. 여러 태스크가 동시에 보내도 메시지가 섞이지 않게 잠그고, 느린 상대는 타임아웃으로 버린다."""

    def __init__(self, ws: WebSocket):
        self.ws = ws
        self._lock = asyncio.Lock()

    async def send_json(self, obj: dict, timeout: float = 2.0) -> bool:
        return await self._send(self.ws.send_text(json.dumps(obj, separators=(",", ":"))), timeout)

    async def send_bytes(self, data: bytes, timeout: float = 2.0) -> bool:
        return await self._send(self.ws.send_bytes(data), timeout)

    async def _send(self, coro, timeout: float) -> bool:
        try:
            async with self._lock:
                await asyncio.wait_for(coro, timeout)
            return True
        except Exception:
            return False


class Stats:
    def __init__(self):
        self.t_in: deque[float] = deque()
        self.t_out: deque[float] = deque()
        self.t_drop: deque[float] = deque()
        self.dropped = 0
        self.stale = 0
        self.errors = {"c2": 0, "c3": 0, "c4": 0, "envelope": 0}
        self.lat = {k: deque(maxlen=100) for k in ("c2", "c3", "c4", "total")}
        self.t_precise: deque[float] = deque()
        self.precise_last_ms: Optional[float] = None
        self.precise_calls = 0
        self.replicas: dict[str, float] = {}

    @staticmethod
    def _rate(q: deque, now: float) -> float:
        while q and q[0] < now - WINDOW_S:
            q.popleft()
        return len(q) / WINDOW_S

    def frame_in(self):
        self.t_in.append(time.monotonic())

    def frame_out(self, lat: dict[str, float]):
        self.t_out.append(time.monotonic())
        for k, v in lat.items():
            self.lat[k].append(v)

    def drop(self):
        self.dropped += 1
        self.t_drop.append(time.monotonic())

    def precise_done(self, ms: float):
        self.t_precise.append(time.monotonic())
        self.precise_last_ms = ms
        self.precise_calls += 1

    def drop_ratio(self) -> float:
        now = time.monotonic()
        fin = self._rate(self.t_in, now)
        return self._rate(self.t_drop, now) / fin if fin else 0.0

    def snapshot(self) -> dict:
        now = time.monotonic()
        avg = {k: round(sum(q) / len(q), 1) if q else None for k, q in self.lat.items()}
        tot = sorted(self.lat["total"])
        avg["p95_total"] = round(tot[min(len(tot) - 1, int(len(tot) * 0.95))], 1) if tot else None
        return {
            "type": "stats",
            "fps_in": round(self._rate(self.t_in, now), 1),
            "fps_out": round(self._rate(self.t_out, now), 1),
            "dropped": self.dropped,
            "stale": self.stale,
            "errors": {k: v for k, v in self.errors.items() if k != "envelope" or v},
            "latency_ms": avg,
            "precise": {"fps": round(self._rate(self.t_precise, now), 2), "last_ms": self.precise_last_ms,
                        "calls": self.precise_calls},
            "c3_replicas_seen": sum(1 for t in self.replicas.values() if t > now - 10),
        }


@dataclass
class Session:
    id: str
    created_at: float = field(default_factory=now_ms)
    config: SessionConfig = field(default_factory=SessionConfig)
    phone: Optional[Peer] = None
    device: Optional[dict] = None
    dashboards: set = field(default_factory=set)
    next_seq: int = 0
    inflight: int = 0
    precise_busy: bool = False
    precise_last: float = 0.0  # monotonic ms
    precise_disabled: bool = False
    needs_precise: bool = False
    target_fps: Optional[int] = None
    stats: Stats = field(default_factory=Stats)
    last_active: float = field(default_factory=time.monotonic)

    def touch(self):
        self.last_active = time.monotonic()

    async def broadcast_json(self, obj: dict):
        for p in list(self.dashboards):
            if not await p.send_json(obj):
                self.dashboards.discard(p)

    async def broadcast_bytes(self, data: bytes):
        for p in list(self.dashboards):
            if not await p.send_bytes(data):
                self.dashboards.discard(p)
