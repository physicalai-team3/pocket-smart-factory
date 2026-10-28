"""C1 Gateway — 세션/QR, 폰·대시보드 WebSocket, C2→C3→C4 오케스트레이션.

명세: docs/02-containers.md#c1-gateway, 메시지: docs/03-messages.md §2, §3
"""
import os

from fastapi import FastAPI, HTTPException, WebSocket

from shared.schemas import SessionConfig, SessionInfo  # noqa: F401

app = FastAPI(title="c1-gateway")

PUBLIC_URL = os.getenv("PUBLIC_URL") or None
CLOUDFLARED_METRICS = os.getenv("CLOUDFLARED_METRICS", "http://cloudflared:2000")
C2_URL = os.getenv("C2_URL", "http://c2-preprocess:8002")
C3_URL = os.getenv("C3_URL", "http://web:8090")
C4_URL = os.getenv("C4_URL", "http://c4-visualizer:8004")
MAX_INFLIGHT = int(os.getenv("MAX_INFLIGHT", "4"))
PRECISE_INTERVAL_MS = int(os.getenv("PRECISE_INTERVAL_MS", "700"))
STAGE_TIMEOUT_MS = int(os.getenv("STAGE_TIMEOUT_MS", "3000"))


@app.get("/api/health")
async def health():
    # TODO: C2/C3/C4 /health, 터널 상태를 deps 에 채우기
    return {"status": "ok", "service": "c1-gateway", "deps": {}}


@app.get("/api/public-url")
async def public_url():
    # TODO: PUBLIC_URL → 없으면 GET {CLOUDFLARED_METRICS}/quicktunnel 의 hostname
    return {"public_url": PUBLIC_URL, "source": "env" if PUBLIC_URL else "none"}


@app.post("/api/sessions", status_code=201)
async def create_session():
    # TODO: 8자 ID 발급, SessionConfig 초기화, SessionInfo 반환
    raise HTTPException(501, "TODO")


@app.get("/api/sessions/{session}")
async def get_session(session: str):
    raise HTTPException(501, "TODO")


@app.get("/api/sessions/{session}/qr.png")
async def session_qr(session: str):
    # TODO: qrcode 로 phone_url PNG 생성 (public_url 없으면 503)
    raise HTTPException(501, "TODO")


@app.websocket("/ws/phone")
async def ws_phone(ws: WebSocket):
    # TODO: ?s= 검증(4404) → hello → 봉투 프레임 수신 → inflight 초과 시 드롭
    #       → asyncio task 로 run_pipeline() / 1초마다 stats 전송
    await ws.accept()
    await ws.close(code=1011)


@app.websocket("/ws/dashboard")
async def ws_dashboard(ws: WebSocket):
    # TODO: session/phone/stats/config 텍스트, result 봉투 바이너리 송신
    #       config/roi/reset 수신
    await ws.accept()
    await ws.close(code=1011)


async def run_pipeline(session: str, seq: int, jpeg: bytes, mode: str) -> None:
    """C2 /preprocess → C3 /infer → C4 /track → 대시보드 푸시. 실패 시 드롭."""
    # TODO
