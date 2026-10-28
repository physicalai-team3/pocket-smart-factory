"""C4 스텁 — POST /track 계약(TrackResponse)을 지키는 더미. 실제 C4(B 담당) 자리를 대신한다.

설계대로 하는 것: seq 순서 검사(stale), 검사선 통과 카운트, 결과 이미지 마킹, reset/삭제.
단순화한 것: 추적은 같은 라인(y)에서 가장 가까운 트랙에 붙이는 greedy 매칭이다.
STUB_NEEDS_PRECISE_EVERY=N 이면 N번째 fast 응답마다 미카운트 트랙 하나에 needs_precise=true 를 붙인다.
"""
from __future__ import annotations

import base64
import os
import time
from dataclasses import dataclass, field

import cv2
import numpy as np
from fastapi import FastAPI

from shared.schemas import Counts, CountedEvent, TrackOut, TrackRequest, TrackResponse, Votes

app = FastAPI(title="c4-stub")

NEEDS_PRECISE_EVERY = int(os.getenv("STUB_NEEDS_PRECISE_EVERY", "0"))
MAX_MISSES = int(os.getenv("MAX_MISSES", "5"))
JPEG_QUALITY = int(os.getenv("JPEG_QUALITY", "70"))
COLORS = {"ok": (80, 200, 80), "ng": (60, 60, 230), "pending": (40, 200, 230)}


@dataclass
class Track:
    id: int
    bbox: tuple
    cx: float
    cy: float
    votes: Votes = field(default_factory=Votes)
    counted: bool = False
    misses: int = 0
    partial: bool = False

    @property
    def verdict(self) -> str:
        v = self.votes
        ok, ng = (v.precise_ok, v.precise_ng) if v.precise_ok + v.precise_ng else (v.fast_ok, v.fast_ng)
        return "pending" if ok + ng == 0 else ("ng" if ng >= ok else "ok")


@dataclass
class State:
    last_seq: int = -1
    next_id: int = 1
    counts: Counts = field(default_factory=Counts)
    tracks: list = field(default_factory=list)
    history: dict = field(default_factory=dict)  # seq -> {track_id: bbox}
    responses: int = 0


SESSIONS: dict[str, State] = {}


def _iou(a, b) -> float:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    iw = max(0, min(ax + aw, bx + bw) - max(ax, bx))
    ih = max(0, min(ay + ah, by + bh) - max(ay, by))
    inter = iw * ih
    return inter / float(aw * ah + bw * bh - inter or 1)


def _update_fast(st: State, req: TrackRequest) -> list[CountedEvent]:
    w = req.infer.w
    line = req.config.line_x * w
    rtl = req.config.direction == "rtl"
    unmatched = list(st.tracks)
    events = []
    for o in req.infer.objects:
        d = max(o.bbox[2], o.bbox[3])
        best = min(unmatched, key=lambda t: abs(t.cx - o.centroid[0]) + 3 * abs(t.cy - o.centroid[1]), default=None)
        if best is not None and abs(best.cy - o.centroid[1]) < 0.5 * d and abs(best.cx - o.centroid[0]) < 1.2 * d:
            t = best
            unmatched.remove(t)
            prev_x = t.cx
        else:
            t = Track(st.next_id, o.bbox, *o.centroid)
            st.next_id += 1
            st.tracks.append(t)
            prev_x = None
        t.bbox, t.cx, t.cy, t.misses, t.partial = o.bbox, o.centroid[0], o.centroid[1], 0, o.partial
        if o.defect is not None:
            if o.defect:
                t.votes.fast_ng += 1
            else:
                t.votes.fast_ok += 1
        crossed = prev_x is not None and ((prev_x > line >= t.cx) if rtl else (prev_x < line <= t.cx))
        if crossed and not t.counted:
            t.counted = True
            verdict = "ok" if t.verdict == "ok" else "ng"
            st.counts.total += 1
            st.counts.ok += verdict == "ok"
            st.counts.ng += verdict == "ng"
            events.append(CountedEvent(track_id=t.id, verdict=verdict, seq=req.seq))
    for t in unmatched:
        t.misses += 1
    st.tracks = [t for t in st.tracks if t.misses <= MAX_MISSES]
    st.history[req.seq] = {t.id: t.bbox for t in st.tracks}
    for s in [s for s in st.history if s < req.seq - 30]:
        del st.history[s]
    return events


def _update_precise(st: State, req: TrackRequest):
    boxes = st.history.get(req.seq, {})
    for o in req.infer.objects:
        best = max(boxes.items(), key=lambda kv: _iou(kv[1], o.bbox), default=None)
        if best and _iou(best[1], o.bbox) > 0.3 and o.defect is not None:
            for t in st.tracks:
                if t.id == best[0] and not t.counted:
                    if o.defect:
                        t.votes.precise_ng += 1
                    else:
                        t.votes.precise_ok += 1


def _draw(jpeg_b64: str, st: State, req: TrackRequest) -> str:
    img = cv2.imdecode(np.frombuffer(base64.b64decode(jpeg_b64), np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        return jpeg_b64
    h, w = img.shape[:2]
    x = int(req.config.line_x * w)
    for y in range(0, h, 12):
        cv2.line(img, (x, y), (x, y + 7), (0, 0, 255), 1)
    for t in st.tracks:
        bx, by, bw, bh = t.bbox
        c = COLORS[t.verdict]
        cv2.rectangle(img, (bx, by), (bx + bw, by + bh), c, 2)
        cv2.putText(img, f"#{t.id}", (bx, max(10, by - 4)), cv2.FONT_HERSHEY_SIMPLEX, 0.4, c, 1, cv2.LINE_AA)
    label = f"STUB seq {req.seq}  total {st.counts.total}  ok {st.counts.ok}  ng {st.counts.ng}"
    cv2.putText(img, label, (8, h - 8), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1, cv2.LINE_AA)
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
    return base64.b64encode(buf.tobytes()).decode() if ok else jpeg_b64


@app.get("/health")
async def health():
    return {"status": "ok", "service": "c4-visualizer", "backend": "stub"}


@app.post("/track", response_model=TrackResponse)
def track(req: TrackRequest):
    t0 = time.perf_counter()
    st = SESSIONS.setdefault(req.session, State())
    if req.mode == "fast" and req.seq <= st.last_seq:
        return TrackResponse(session=req.session, seq=req.seq, stale=True, counts=st.counts)
    events, image = [], None
    if req.mode == "fast":
        st.last_seq = req.seq
        events = _update_fast(st, req)
        st.responses += 1
        if req.image_jpeg_b64:
            image = _draw(req.image_jpeg_b64, st, req)
    else:
        _update_precise(st, req)
    flag = None
    if req.mode == "fast" and NEEDS_PRECISE_EVERY and st.responses % NEEDS_PRECISE_EVERY == 0:
        flag = next((t.id for t in st.tracks if not t.counted and not t.partial), None)
    tracks = [TrackOut(track_id=t.id, bbox=t.bbox, verdict=t.verdict, counted=t.counted, votes=t.votes,
                       needs_precise=t.id == flag) for t in st.tracks]
    return TrackResponse(session=req.session, seq=req.seq, counts=st.counts, tracks=tracks, events=events,
                         image_jpeg_b64=image, proc_ms=round((time.perf_counter() - t0) * 1000, 1))


@app.post("/reset/{session}")
async def reset(session: str):
    SESSIONS[session] = State(last_seq=SESSIONS[session].last_seq if session in SESSIONS else -1)
    return {"ok": True}


@app.delete("/sessions/{session}")
async def drop_session(session: str):
    SESSIONS.pop(session, None)
    return {"ok": True}
