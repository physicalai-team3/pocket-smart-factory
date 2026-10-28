"""C1 Gateway — 세션/QR, 폰·대시보드 WebSocket, C2→C3→C4 오케스트레이션.

명세: docs/02-containers.md#c1-gateway, 메시지: docs/03-messages.md §2, §3
"""
from __future__ import annotations

import asyncio
import io
import json
import os
import time
from contextlib import asynccontextmanager
from typing import Optional

import httpx
import qrcode
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import Response
from pydantic import ValidationError

from shared.schemas import SessionConfig, SessionInfo, unpack_envelope

from .pipeline import Pipeline, should_run_precise
from .state import Peer, Session, new_session_id, now_ms
from .tunnel import Tunnel

PUBLIC_URL = os.getenv("PUBLIC_URL") or None
CLOUDFLARED_METRICS = os.getenv("CLOUDFLARED_METRICS", "http://cloudflared:2000")
C2_URL = os.getenv("C2_URL", "http://c2-preprocess:8002")
C3_URL = os.getenv("C3_URL", "http://web:8090")
C3_PRECISE_URL = os.getenv("C3_PRECISE_URL") or C3_URL  # precise 전용 풀 (C 와 합의 후 nginx 에 추가)
C4_URL = os.getenv("C4_URL", "http://c4-visualizer:8004")
MAX_INFLIGHT = int(os.getenv("MAX_INFLIGHT", "4"))
PRECISE_POLICY = os.getenv("PRECISE_POLICY", "uncertain")  # uncertain | interval | off
PRECISE_INTERVAL_MS = int(os.getenv("PRECISE_INTERVAL_MS", "300"))
STAGE_TIMEOUT_MS = int(os.getenv("STAGE_TIMEOUT_MS", "3000"))
PRECISE_TIMEOUT_MS = int(os.getenv("PRECISE_TIMEOUT_MS", "10000"))
TARGET_FPS = int(os.getenv("TARGET_FPS", "10"))
SESSION_TTL_S = int(os.getenv("SESSION_TTL_S", "600"))
TUNNEL_POLL_S = float(os.getenv("TUNNEL_POLL_S", "5"))

# 앱 상태 — 프로세스 하나에 하나 (C1 은 복제하지 않는다)
sessions: dict[str, Session] = {}
tunnel = Tunnel(PUBLIC_URL, CLOUDFLARED_METRICS)
_tasks: set[asyncio.Task] = set()  # 이벤트 루프는 태스크를 약하게만 참조하므로 여기서 붙잡아 둔다
_ctx: dict = {}


def spawn(coro) -> asyncio.Task:
    t = asyncio.create_task(coro)
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)
    return t


@asynccontextmanager
async def lifespan(_app: FastAPI):
    client = httpx.AsyncClient(limits=httpx.Limits(max_connections=64, max_keepalive_connections=32))
    _ctx["client"] = client
    _ctx["pipeline"] = Pipeline(client, C2_URL, C3_URL, C4_URL, STAGE_TIMEOUT_MS / 1000, PRECISE_TIMEOUT_MS / 1000,
                                C3_PRECISE_URL)
    loops = [spawn(_tunnel_loop()), spawn(_stats_loop()), spawn(_cleanup_loop())]
    try:
        yield
    finally:
        for t in loops:
            t.cancel()
        await client.aclose()


app = FastAPI(title="c1-gateway", lifespan=lifespan)


# ── 세션 / REST ──────────────────────────────────────────────

def session_info(s: Session) -> SessionInfo:
    base = tunnel.public_url
    return SessionInfo(session=s.id, public_url=base, phone_url=f"{base}/phone.html?s={s.id}" if base else None,
                       qr_url=f"/api/sessions/{s.id}/qr.png", created_at=s.created_at)


def get_or_404(session: str) -> Session:
    s = sessions.get(session)
    if s is None:
        raise HTTPException(404, {"error": "SESSION_NOT_FOUND", "message": session})
    return s


@app.get("/api/health")
async def health():
    client: httpx.AsyncClient = _ctx["client"]

    async def probe(url: str) -> str:
        try:
            r = await client.get(f"{url.rstrip('/')}/health", timeout=1.5)
            return "ok" if r.status_code == 200 else f"http {r.status_code}"
        except httpx.HTTPError:
            return "down"

    c2, c3, c4 = await asyncio.gather(probe(C2_URL), probe(C3_URL), probe(C4_URL))
    deps = {"c2": c2, "c3": c3, "c4": c4, "tunnel": "ok" if tunnel.public_url else "pending"}
    return {"status": "ok" if all(v == "ok" for v in (c2, c3, c4)) else "degraded", "service": "c1-gateway",
            "deps": deps}


@app.get("/api/public-url")
async def public_url():
    return {"public_url": tunnel.public_url, "source": tunnel.source}


@app.post("/api/sessions", status_code=201, response_model=SessionInfo)
async def create_session():
    sid = new_session_id()
    while sid in sessions:
        sid = new_session_id()
    sessions[sid] = Session(sid)
    return session_info(sessions[sid])


@app.get("/api/sessions/{session}", response_model=SessionInfo)
async def get_session(session: str):
    return session_info(get_or_404(session))


@app.get("/api/sessions/{session}/qr.png")
async def session_qr(session: str):
    info = session_info(get_or_404(session))
    if not info.phone_url:
        raise HTTPException(503, {"error": "PUBLIC_URL_PENDING", "message": "tunnel is starting"})
    qr = qrcode.QRCode(box_size=8, border=2)
    qr.add_data(info.phone_url)
    buf = io.BytesIO()
    qr.make_image().save(buf, format="PNG")
    return Response(buf.getvalue(), media_type="image/png", headers={"Cache-Control": "no-store"})


# ── WebSocket ────────────────────────────────────────────────

async def _reject(ws: WebSocket, code: str, close_code: int, message: str = ""):
    await ws.send_text(json.dumps({"type": "error", "code": code, "message": message}))
    await ws.close(code=close_code)


def _phone_status(s: Session) -> dict:
    return {"type": "phone", "connected": s.phone is not None, "device": s.device}


@app.websocket("/ws/phone")
async def ws_phone(ws: WebSocket):
    await ws.accept()
    s = sessions.get(ws.query_params.get("s", ""))
    if s is None:
        return await _reject(ws, "SESSION_NOT_FOUND", 4404)
    peer = Peer(ws)
    old, s.phone = s.phone, peer
    if old is not None:  # 같은 세션에 새 폰 → 이전 연결을 닫는다
        await old.send_json({"type": "error", "code": "PHONE_REPLACED", "message": "another phone connected"})
        try:
            await old.ws.close(code=4409)
        except RuntimeError:
            pass
    s.touch()
    await peer.send_json({"type": "welcome", "session": s.id, "target_fps": s.target_fps or TARGET_FPS})
    await s.broadcast_json(_phone_status(s))
    try:
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                break
            s.touch()
            if msg.get("bytes") is not None:
                handle_frame(s, msg["bytes"])
            elif msg.get("text") is not None:
                await _phone_text(s, peer, msg["text"])
    except WebSocketDisconnect:
        pass
    finally:
        if s.phone is peer:
            s.phone = None
            await s.broadcast_json(_phone_status(s))


async def _phone_text(s: Session, peer: Peer, text: str):
    try:
        m = json.loads(text)
    except ValueError:
        return
    if m.get("type") == "hello":
        s.device = {**(m.get("device") or {}), "send_w": m.get("send_w"), "send_h": m.get("send_h")}
        if isinstance(m.get("target_fps"), (int, float)):
            s.target_fps = int(min(m["target_fps"], TARGET_FPS * 3))
        await s.broadcast_json(_phone_status(s))
    elif m.get("type") == "ping":
        await peer.send_json({"type": "pong", "t": now_ms()})


def handle_frame(s: Session, data: bytes):
    """프레임 수신 → 처리 중 프레임이 MAX_INFLIGHT 이상이면 즉시 버린다(큐에 쌓지 않음)."""
    t_recv = now_ms()
    s.stats.frame_in()
    try:
        header, jpeg = unpack_envelope(data)
        if header.get("type") != "frame" or len(jpeg) < 4:
            raise ValueError("not a frame envelope")
    except Exception:  # struct.error(짧은 데이터), JSON·UTF-8 오류 등 → 이 프레임만 버리고 연결은 유지
        s.stats.errors["envelope"] += 1
        return
    if s.inflight >= MAX_INFLIGHT:
        s.stats.drop()
        return
    seq = s.next_seq
    s.next_seq += 1
    s.inflight += 1
    pipe: Pipeline = _ctx["pipeline"]
    spawn(pipe.run(s, seq, jpeg, t_recv, "fast"))
    mono_ms = time.monotonic() * 1000
    if should_run_precise(s.config.precise_policy, s.needs_precise, s.precise_busy, s.precise_disabled,
                          mono_ms, s.precise_last, s.config.precise_interval_ms):
        s.precise_busy, s.precise_last = True, mono_ms
        spawn(pipe.run(s, seq, jpeg, t_recv, "precise"))  # 같은 프레임을 precise 로도 보낸다


@app.websocket("/ws/dashboard")
async def ws_dashboard(ws: WebSocket):
    await ws.accept()
    s = sessions.get(ws.query_params.get("s", ""))
    if s is None:
        return await _reject(ws, "SESSION_NOT_FOUND", 4404)
    peer = Peer(ws)
    s.dashboards.add(peer)
    s.touch()
    await peer.send_json({"type": "session", **session_info(s).model_dump()})
    await peer.send_json({"type": "config", "config": s.config.model_dump()})
    await peer.send_json(_phone_status(s))
    try:
        while True:
            text = await ws.receive_text()
            s.touch()
            await _dashboard_text(s, peer, text)
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        s.dashboards.discard(peer)


def validate_roi(points) -> Optional[list]:
    if points is None:
        return None
    if not (isinstance(points, list) and len(points) == 4
            and all(isinstance(p, list) and len(p) == 2 and all(isinstance(v, (int, float)) for v in p)
                    for p in points)):
        raise ValueError("roi needs 4 [x, y] points")
    if any(not -0.05 <= v <= 1.05 for p in points for v in p):
        raise ValueError("roi points must be within 0..1")
    cross = []
    for i in range(4):
        (x0, y0), (x1, y1), (x2, y2) = points[i], points[(i + 1) % 4], points[(i + 2) % 4]
        cross.append((x1 - x0) * (y2 - y1) - (y1 - y0) * (x2 - x1))
    if not (all(c > 1e-4 for c in cross) or all(c < -1e-4 for c in cross)):
        raise ValueError("roi must be a convex quadrilateral in order TL, TR, BR, BL")
    return [[float(x), float(y)] for x, y in points]


async def _dashboard_text(s: Session, peer: Peer, text: str):
    try:
        m = json.loads(text)
    except ValueError:
        return
    kind = m.get("type")
    if kind == "config" and isinstance(m.get("config"), dict):
        patch = {k: v for k, v in m["config"].items() if k != "roi"}  # roi 는 roi 메시지로만
        try:
            s.config = SessionConfig.model_validate({**s.config.model_dump(), **patch})
        except ValidationError as e:
            return await peer.send_json({"type": "error", "code": "BAD_CONFIG", "message": str(e.errors()[:1])})
        if "precise_policy" in patch:
            s.precise_disabled = False
        await s.broadcast_json({"type": "config", "config": s.config.model_dump()})
    elif kind == "roi":
        try:
            roi = validate_roi(m.get("points"))
        except ValueError as e:
            return await peer.send_json({"type": "error", "code": "BAD_ROI", "message": str(e)})
        s.config = SessionConfig.model_validate({**s.config.model_dump(), "roi": roi})
        await s.broadcast_json({"type": "config", "config": s.config.model_dump()})
    elif kind == "reset":
        s.needs_precise = False
        try:
            await _ctx["client"].post(f"{C4_URL.rstrip('/')}/reset/{s.id}", timeout=STAGE_TIMEOUT_MS / 1000)
        except httpx.HTTPError:
            s.stats.errors["c4"] += 1
    elif kind == "ping":
        await peer.send_json({"type": "pong", "t": now_ms()})


# ── 백그라운드 루프 ───────────────────────────────────────────

async def _tunnel_loop():
    while True:
        if await tunnel.refresh(_ctx["client"]):
            for s in list(sessions.values()):  # URL 이 바뀌면 QR 을 다시 그리도록 알린다
                await s.broadcast_json({"type": "session", **session_info(s).model_dump()})
        await asyncio.sleep(TUNNEL_POLL_S if not tunnel.public_url else TUNNEL_POLL_S * 6)


async def _stats_loop():
    while True:
        await asyncio.sleep(1.0)
        for s in list(sessions.values()):
            if s.phone is None and not s.dashboards:
                continue
            snap = s.stats.snapshot()
            await s.broadcast_json(snap)
            if s.phone is not None:
                await s.phone.send_json({k: snap[k] for k in ("type", "fps_in", "fps_out", "dropped")}
                                        | {"latency_ms": snap["latency_ms"]["total"]})
                await _maybe_throttle(s, snap)


async def _maybe_throttle(s: Session, snap: dict):
    """드롭이 30% 를 넘으면 폰 전송 fps 를 처리량 수준으로 낮추고, 드롭이 없으면 1씩 되돌린다."""
    current = s.target_fps or TARGET_FPS
    ratio = s.stats.drop_ratio()
    target = current
    if ratio > 0.3 and current > 2:
        target = max(2, int(snap["fps_out"]) or 2)
    elif ratio == 0 and current < TARGET_FPS and snap["fps_in"] >= current - 1:
        target = current + 1
    if target != current:
        s.target_fps = target
        await s.phone.send_json({"type": "throttle", "target_fps": target})


async def _cleanup_loop():
    """대시보드·폰이 모두 끊기고 SESSION_TTL_S 가 지난 세션을 정리하고 C4 에도 알린다."""
    while True:
        await asyncio.sleep(30)
        cutoff = time.monotonic() - SESSION_TTL_S
        for sid, s in list(sessions.items()):
            if s.phone is None and not s.dashboards and s.last_active < cutoff:
                del sessions[sid]
                try:
                    await _ctx["client"].delete(f"{C4_URL.rstrip('/')}/sessions/{sid}", timeout=2.0)
                except httpx.HTTPError:
                    pass
