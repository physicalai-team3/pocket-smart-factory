"""C4 Visualizer — 추적, 검사선 카운트, 결과 마킹. 상태 보유, 반드시 1개.

명세: docs/02-containers.md#c4-visualizer--담당-b, 로직: docs/05-detection.md §5
메시지: docs/03-messages.md §4.3, §4.4
"""
import os

from fastapi import FastAPI, HTTPException

from shared.schemas import TrackRequest, TrackResponse

app = FastAPI(title="c4-visualizer")

MAX_MISSES = int(os.getenv("MAX_MISSES", "5"))
MATCH_MAX_DIST = float(os.getenv("MATCH_MAX_DIST", "0.6"))
JPEG_QUALITY = int(os.getenv("JPEG_QUALITY", "70"))


@app.get("/health")
async def health():
    return {"status": "ok", "service": "c4-visualizer"}


@app.post("/track", response_model=TrackResponse)
async def track(req: TrackRequest):
    # TODO: fast  → seq <= last_seq 이면 stale, 아니면 tracker.update() → 검사선 통과 카운트 → 마킹
    #       precise → history[seq] 와 IoU 매칭해 precise 투표만 반영 (image 없음)
    raise HTTPException(501, "TODO")


@app.post("/reset/{session}")
async def reset(session: str):
    # TODO: 해당 세션의 트랙, 카운트, 이력 초기화
    return {"ok": True}


@app.delete("/sessions/{session}")
async def drop_session(session: str):
    # TODO: 세션 상태 삭제
    return {"ok": True}
