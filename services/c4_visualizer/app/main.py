"""C4 Visualizer — 추적, 검사선 카운트, 결과 마킹. 상태 보유, 반드시 1개.

명세: docs/02-containers.md#c4-visualizer, 로직: docs/05-detection.md §5
메시지: docs/03-messages.md §4.3, §4.4
"""
import os
import time

from fastapi import FastAPI

from shared.schemas import Counts, TrackOut, TrackRequest, TrackResponse

from . import counter, draw
from .tracker import Tracker

app = FastAPI(title="c4-visualizer")

MAX_MISSES = int(os.getenv("MAX_MISSES", "5"))
MATCH_MAX_DIST = float(os.getenv("MATCH_MAX_DIST", "0.6"))
JPEG_QUALITY = int(os.getenv("JPEG_QUALITY", "70"))
HISTORY_FRAMES = int(os.getenv("HISTORY_FRAMES", "30"))  # precise 매칭용 위치 이력 (10fps 기준 3초)


class SessionState:
    def __init__(self):
        self.last_seq = -1  # 마지막으로 처리한 fast seq. reset 해도 유지 (C1 seq 는 계속 증가)
        self.counts = Counts()
        self.tracker = Tracker(MAX_MISSES, MATCH_MAX_DIST, HISTORY_FRAMES)


_sessions: dict[str, SessionState] = {}


@app.get("/health")
async def health():
    return {"status": "ok", "service": "c4-visualizer", "sessions": len(_sessions)}


@app.post("/track", response_model=TrackResponse)
async def track(req: TrackRequest):
    t0 = time.perf_counter()
    st = _sessions.setdefault(req.session, SessionState())
    cfg = req.config

    def respond(**kw):
        return TrackResponse(session=req.session, seq=req.seq, counts=st.counts.model_copy(),
                             proc_ms=round((time.perf_counter() - t0) * 1000, 2), **kw)

    if req.mode == "precise":
        # 늦게 오는 게 정상: 그 seq 의 위치 이력과 IoU 매칭해 precise 투표만 더한다 (확정된 트랙은 그대로)
        for t, o in st.tracker.match_history(req.seq, req.infer.objects):
            if not t.counted:
                counter.vote(t, o, "precise")
        return respond()

    if req.seq <= st.last_seq:
        return respond(stale=True)
    st.last_seq = req.seq

    pairs = st.tracker.update(req.seq, req.infer.objects, cfg.direction)
    events = []
    for t, o in pairs:
        if not t.counted:
            counter.vote(t, o, "fast")
        ev = counter.check_line(t, cfg, req.infer.w, req.seq, st.counts)
        if ev:
            events.append(ev)

    outs = [TrackOut(track_id=t.track_id, bbox=t.bbox, verdict=counter.current_verdict(t),
                     counted=t.counted, votes=t.votes.model_copy(),
                     needs_precise=counter.needs_precise(t, cfg)) for t, _ in pairs]
    image = None
    if req.image_jpeg_b64:
        image = draw.render(req.image_jpeg_b64, req.infer.w, req.infer.h,
                            [(out, o) for out, (_, o) in zip(outs, pairs)], cfg, st.counts, events, JPEG_QUALITY)
    return respond(tracks=outs, events=events, image_jpeg_b64=image)


@app.post("/reset/{session}")
async def reset(session: str):
    st = _sessions.get(session)
    if st:
        st.counts = Counts()
        st.tracker.clear()
    return {"ok": True}


@app.delete("/sessions/{session}")
async def drop_session(session: str):
    _sessions.pop(session, None)
    return {"ok": True}
