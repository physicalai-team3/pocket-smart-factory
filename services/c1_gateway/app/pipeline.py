"""프레임 한 장: C2 /preprocess → C3 /infer → C4 /track → 대시보드 푸시. 실패하면 버리고 재시도하지 않는다."""
from __future__ import annotations

import base64
import logging
import time
from typing import Optional

import httpx
from pydantic import ValidationError

from shared.schemas import InferResult, TrackConfig, TrackRequest, TrackResponse, pack_envelope

from .state import Session

log = logging.getLogger("c1.pipeline")


def should_run_precise(policy: str, needs_precise: bool, busy: bool, disabled: bool,
                       now_ms: float, last_ms: float, interval_ms: int) -> bool:
    """precise(SAM 2) 요청을 지금 보낼지. 진행 중인 precise 는 한 번에 하나."""
    if policy == "off" or busy or disabled or now_ms - last_ms < interval_ms:
        return False
    return needs_precise if policy == "uncertain" else policy == "interval"


def roi_header(roi: Optional[list]) -> Optional[str]:
    return ",".join(f"{v:.5f}" for p in roi for v in p) if roi else None


class Pipeline:
    def __init__(self, client: httpx.AsyncClient, c2: str, c3: str, c4: str,
                 stage_timeout_s: float, precise_timeout_s: float, c3_precise: Optional[str] = None):
        self.client, self.c2, self.c3, self.c4 = client, c2.rstrip("/"), c3.rstrip("/"), c4.rstrip("/")
        # precise(SAM 2)는 수 초씩 복제본을 붙잡으므로, 전용 풀을 두면 fast 프레임이 그 뒤에 줄 서지 않는다
        self.c3_precise = (c3_precise or c3).rstrip("/")
        self.stage_timeout, self.precise_timeout = stage_timeout_s, precise_timeout_s

    async def run(self, s: Session, seq: int, jpeg: bytes, t_recv_ms: float, mode: str) -> None:
        t0 = time.perf_counter()
        lat: dict[str, float] = {}
        stage = "c2"
        try:
            base = {"X-Session": s.id, "X-Seq": str(seq), "X-Ts-Recv": f"{t_recv_ms:.1f}"}
            h2 = {**base, "Content-Type": "image/jpeg"}
            roi = roi_header(s.config.roi)
            if roi:
                h2["X-Roi"] = roi
            t = time.perf_counter()
            r = await self.client.post(f"{self.c2}/preprocess", content=jpeg, headers=h2, timeout=self.stage_timeout)
            r.raise_for_status()
            pre = r.content
            lat["c2"] = (time.perf_counter() - t) * 1000

            stage = "c3"
            h3 = {**base, "Content-Type": "image/jpeg", "X-Mode": mode,
                  "X-Circ-Th": str(s.config.circ_th), "X-Solid-Th": str(s.config.solid_th)}
            t = time.perf_counter()
            c3 = self.c3_precise if mode == "precise" else self.c3
            r = await self.client.post(f"{c3}/infer", content=pre, headers=h3,
                                       timeout=self.precise_timeout if mode == "precise" else self.stage_timeout)
            if mode == "precise" and r.status_code == 409:
                s.precise_disabled = True  # SAM 2 미로드 → 이 세션에서는 precise 중단
                return
            r.raise_for_status()
            infer = InferResult.model_validate_json(r.content)
            lat["c3"] = (time.perf_counter() - t) * 1000
            s.stats.replicas[infer.replica] = time.monotonic()

            stage = "c4"
            cfg = s.config
            req = TrackRequest(
                session=s.id, seq=seq, mode=mode, infer=infer,
                config=TrackConfig(line_x=cfg.line_x, direction=cfg.direction, circ_th=cfg.circ_th,
                                   solid_th=cfg.solid_th, circ_band=cfg.circ_band, solid_band=cfg.solid_band),
                image_jpeg_b64=base64.b64encode(pre).decode() if mode == "fast" else None)
            t = time.perf_counter()
            r = await self.client.post(f"{self.c4}/track", content=req.model_dump_json(),
                                       headers={"Content-Type": "application/json"}, timeout=self.stage_timeout)
            r.raise_for_status()
            tr = TrackResponse.model_validate_json(r.content)
            lat["c4"] = (time.perf_counter() - t) * 1000
        except (httpx.HTTPError, ValidationError, ValueError) as e:
            s.stats.errors[stage] += 1
            log.warning("session %s seq %d %s: %s failed: %r", s.id, seq, mode, stage, e)
            return
        finally:
            if mode == "fast":
                s.inflight -= 1
            else:
                s.precise_busy = False

        lat["total"] = (time.perf_counter() - t0) * 1000
        if mode == "precise":
            s.stats.precise_done(round(lat["total"], 1))
            return
        if tr.stale:  # 더 최신 fast 프레임을 이미 처리함 → 대시보드로 보내지 않는다
            s.stats.stale += 1
            return
        s.needs_precise = any(t.needs_precise for t in tr.tracks)
        s.stats.frame_out(lat)
        header = {
            "type": "result", "seq": seq, "w": infer.w, "h": infer.h,
            "counts": tr.counts.model_dump(),
            "tracks": [t.model_dump(include={"track_id", "bbox", "verdict", "counted", "needs_precise"})
                       for t in tr.tracks],
            "events": [e.model_dump() for e in tr.events],
            "latency_ms": {k: round(v, 1) for k, v in lat.items()},
        }
        image = base64.b64decode(tr.image_jpeg_b64) if tr.image_jpeg_b64 else pre
        await s.broadcast_bytes(pack_envelope(header, image))
