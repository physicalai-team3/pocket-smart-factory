"""대시보드 개발용 가짜 C1 — 폰·C2·C3 없이 web/index.html 을 계약(docs/03-messages.md §2, §3.2)대로 띄운다.

시뮬레이터 캡처(labels_*.json + frame_*.png)를 tools/sim2track.py 로 움직이는 프레임으로 만들고,
진짜 C4 코드(services/c4_visualizer)를 같은 프로세스에서 호출해 마킹된 결과를 /ws/dashboard 로 보낸다.

- fast 판정: 캡처 정답 윤곽선의 원형도·볼록도를 대시보드 config 의 circ_th / solid_th 로 판정 (C3 흉내)
- precise: precise_policy 에 따라 C1 규칙대로 예약하고, 정답 라벨을 SAM 2 결과처럼 늦게 넣는다
- 터널: 처음 몇 초는 public_url 이 없다가 생긴다 (QR 갱신 확인용)
- 드롭·지연은 무작위로 흉내 낸다

    pip install -r services/c1_gateway/requirements.txt -r services/c4_visualizer/requirements.txt
    python tools/mock_c1.py data/sim/labels_*.json        # → http://localhost:8080
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import io
import json
import random
import secrets
import string
import sys
import time
from argparse import Namespace
from contextlib import asynccontextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "services" / "c4_visualizer"), str(ROOT / "tools")]

import uvicorn  # noqa: E402
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect  # noqa: E402
from fastapi.responses import Response  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

import sim2track  # noqa: E402
from app import main as c4  # noqa: E402  (services/c4_visualizer/app)
from shared.schemas import SessionConfig, SessionInfo, TrackConfig, TrackRequest, pack_envelope  # noqa: E402

OPTS: Namespace
FRAMES: list[dict] = []  # sim2track TrackRequest dict 들 (모든 캡처를 이어 붙인 것, 사이사이 빈 프레임)
TUNNEL = {"url": None}
SESSIONS: dict[str, "Session"] = {}


@asynccontextmanager
async def lifespan(_app):
    task = asyncio.create_task(tunnel_later())
    yield
    task.cancel()


app = FastAPI(title="mock-c1", lifespan=lifespan)


class Session:
    def __init__(self, sid: str):
        self.sid = sid
        self.created_at = time.time() * 1000
        self.config = SessionConfig()
        self.dashboards: set[WebSocket] = set()
        self.task: asyncio.Task | None = None
        self.seq = 0
        self.dropped = 0
        self.fin = self.fout = 0  # 직전 1초 동안 받은/보낸 프레임 수
        self.lat: list[dict] = []  # 직전 1초 지연 샘플
        self.totals: list[float] = []  # p95 용 최근 total
        self.precise_busy = False
        self.precise_last = 0.0
        self.precise_n = 0
        self.precise_ms = None
        self.needs_precise = False

    def info(self) -> dict:
        pub = TUNNEL["url"]
        return SessionInfo(session=self.sid, public_url=pub, phone_url=f"{pub}/phone.html?s={self.sid}" if pub else None,
                           qr_url=f"/api/sessions/{self.sid}/qr.png", created_at=self.created_at).model_dump()

    async def broadcast(self, msg: dict | bytes):
        for ws in list(self.dashboards):
            try:
                await (ws.send_bytes(msg) if isinstance(msg, bytes) else ws.send_json(msg))
            except Exception:
                self.dashboards.discard(ws)


def track_config(cfg: SessionConfig) -> TrackConfig:
    return TrackConfig(**cfg.model_dump(include=set(TrackConfig.model_fields)))


# ── REST ──
@app.get("/api/health")
async def health():
    return {"status": "ok", "service": "mock-c1", "deps": {"c2": "mock", "c3": "mock", "c4": "in-process",
                                                           "tunnel": "ok" if TUNNEL["url"] else "starting"}}


@app.get("/api/public-url")
async def public_url():
    return {"public_url": TUNNEL["url"], "source": "quicktunnel" if TUNNEL["url"] else "none"}


@app.post("/api/sessions", status_code=201)
async def create_session():
    sid = "".join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(8))
    SESSIONS[sid] = Session(sid)
    return SESSIONS[sid].info()


@app.get("/api/sessions/{sid}")
async def get_session(sid: str):
    if sid not in SESSIONS:
        raise HTTPException(404, "SESSION_NOT_FOUND")
    return SESSIONS[sid].info()


@app.get("/api/sessions/{sid}/qr.png")
async def session_qr(sid: str):
    if sid not in SESSIONS:
        raise HTTPException(404, "SESSION_NOT_FOUND")
    if not TUNNEL["url"]:
        raise HTTPException(503, "TUNNEL_NOT_READY")
    import qrcode
    buf = io.BytesIO()
    qrcode.make(SESSIONS[sid].info()["phone_url"], box_size=10, border=2).resize((300, 300)).save(buf, "PNG")
    return Response(buf.getvalue(), media_type="image/png", headers={"Cache-Control": "no-store"})


# ── WebSocket ──
@app.websocket("/ws/dashboard")
async def ws_dashboard(ws: WebSocket):
    await ws.accept()
    st = SESSIONS.get(ws.query_params.get("s", ""))
    if not st:
        await ws.send_json({"type": "error", "code": "SESSION_NOT_FOUND", "message": "unknown session"})
        await ws.close(code=4404)
        return
    st.dashboards.add(ws)
    await ws.send_json({"type": "session", **st.info()})
    await ws.send_json({"type": "phone", "connected": True,
                        "device": {"ua": "Mozilla/5.0 (Linux; Android 15; Pixel 9) mock", "w": 1280, "h": 720}})
    await ws.send_json({"type": "config", "config": st.config.model_dump()})
    if not st.task or st.task.done():
        st.task = asyncio.create_task(produce(st))
    try:
        while True:
            msg = json.loads(await ws.receive_text())
            if msg.get("type") == "config":
                st.config = SessionConfig.model_validate({**st.config.model_dump(), **msg.get("config", {})})
            elif msg.get("type") == "roi":
                st.config = SessionConfig.model_validate({**st.config.model_dump(), "roi": msg.get("points")})
            elif msg.get("type") == "reset":
                await c4.reset(st.sid)
                continue
            else:
                continue
            await st.broadcast({"type": "config", "config": st.config.model_dump()})
    except (WebSocketDisconnect, json.JSONDecodeError, ValueError):
        pass
    finally:
        st.dashboards.discard(ws)


# ── 가짜 파이프라인 ──
def judged(frame: dict, st: Session, seq: int, mode: str) -> TrackRequest:
    """프레임 dict 를 이 세션의 seq·config 로 판정한 TrackRequest 로 만든다 (fast = 임계값, precise = 정답)."""
    cfg = st.config
    # 이미지(base64)는 크므로 복사하지 않고, 바꿀 infer 만 새로 만든다
    r = {**frame, "infer": {**frame["infer"], "objects": [dict(o) for o in frame["infer"]["objects"]]}}
    r["session"] = r["infer"]["session"] = st.sid  # C4 상태는 세션별
    r["seq"] = r["infer"]["seq"] = seq
    r["mode"] = r["infer"]["mode"] = mode
    r["config"] = track_config(cfg).model_dump()
    for o, truth in zip(r["infer"]["objects"], frame["_truth"]):
        if o["partial"]:
            o["defect"] = None
        elif mode == "precise":
            o["defect"], o["score"] = truth, 0.93
        else:
            o["defect"] = o["circularity"] < cfg.circ_th or o["solidity"] < cfg.solid_th
    if mode == "precise":
        r["image_jpeg_b64"] = None
        r["infer"]["backend"] = "sam2-mock"
    r.pop("_truth", None)
    return TrackRequest.model_validate(r)


async def run_precise(st: Session, frame: dict, seq: int):
    t0 = time.perf_counter()
    try:
        await asyncio.sleep(random.uniform(0.6, 1.1))  # SAM 2 CPU 추론 시간 흉내
        await c4.track(judged(frame, st, seq, "precise"))
        st.precise_n += 1
        st.precise_ms = round((time.perf_counter() - t0) * 1000)
    finally:
        st.precise_busy = False
        st.precise_last = time.monotonic()


async def produce(st: Session):
    period = 1 / OPTS.fps
    i, next_stats = 0, time.monotonic() + 1
    while st.dashboards:
        t_start = time.monotonic()
        frame = FRAMES[i % len(FRAMES)]
        i += 1
        seq, st.seq = st.seq, st.seq + 1
        st.fin += 1
        if random.random() < OPTS.drop:
            st.dropped += 1
        else:
            cfg = st.config
            # precise 예약 (C1 규칙: 진행 중 precise 없음 + 최소 간격 + 정책)
            pol = cfg.precise_policy
            if (pol != "off" and not st.precise_busy
                    and (time.monotonic() - st.precise_last) * 1000 >= cfg.precise_interval_ms
                    and (pol == "interval" or st.needs_precise) and frame["infer"]["objects"]):
                st.precise_busy = True
                asyncio.create_task(run_precise(st, frame, seq))

            res = await c4.track(judged(frame, st, seq, "fast"))
            st.needs_precise = any(t.needs_precise for t in res.tracks)
            lat = {"c2": round(random.gauss(9, 2), 1), "c3": round(random.gauss(22, 6), 1), "c4": res.proc_ms}
            lat["total"] = round(sum(lat.values()) + random.uniform(4, 30), 1)
            st.lat.append(lat)
            st.totals = (st.totals + [lat["total"]])[-100:]
            header = {"type": "result", "seq": seq, "w": frame["infer"]["w"], "h": frame["infer"]["h"],
                      "counts": res.counts.model_dump(),
                      "tracks": [t.model_dump(include={"track_id", "bbox", "verdict", "counted", "needs_precise"})
                                 for t in res.tracks],
                      "events": [e.model_dump() for e in res.events], "latency_ms": lat}
            if res.image_jpeg_b64:
                await st.broadcast(pack_envelope(header, base64.b64decode(res.image_jpeg_b64)))
                st.fout += 1

        if time.monotonic() >= next_stats:
            next_stats += 1
            avg = {k: round(sum(x[k] for x in st.lat) / len(st.lat), 1) for k in ("c2", "c3", "c4", "total")} if st.lat \
                else {"c2": 0, "c3": 0, "c4": 0, "total": 0}
            p95 = sorted(st.totals)[int(len(st.totals) * 0.95)] if st.totals else 0
            await st.broadcast({"type": "stats", "fps_in": float(st.fin), "fps_out": float(st.fout),
                                "dropped": st.dropped, "errors": {"c2": 0, "c3": 0, "c4": 0},
                                "latency_ms": {**avg, "p95_total": p95},
                                "precise": {"fps": float(st.precise_n), "last_ms": st.precise_ms},
                                "c3_replicas_seen": 2})
            st.fin = st.fout = st.precise_n = 0
            st.lat.clear()
        await asyncio.sleep(max(0.0, period - (time.monotonic() - t_start)))


async def tunnel_later():
    await asyncio.sleep(OPTS.tunnel_delay)
    TUNNEL["url"] = f"https://mock-{secrets.token_hex(3)}.trycloudflare.com"
    for st in SESSIONS.values():
        await st.broadcast({"type": "session", **st.info()})




def load_frames(labels: list[str]) -> list[dict]:
    frames = []
    for lp in labels:
        a = Namespace(labels=[lp], png=None, width=960, frames=OPTS.frames, dx=OPTS.dx, session="mock", start_seq=0,
                      defect_from="truth", circ_th=sim2track.CIRC_TH, solid_th=sim2track.SOLID_TH, jitter=1.5, miss=0.05, skip=0, seed=0)
        reqs, _ = sim2track.build(a)
        for r in reqs:
            r["_truth"] = [o["defect"] for o in r["infer"]["objects"]]
        frames += reqs
        # 다음 캡처로 넘어가기 전 빈 프레임: 이전 트랙이 MAX_MISSES 로 사라지게 한다
        blank = {**reqs[-1], "infer": {**reqs[-1]["infer"], "objects": []}, "image_jpeg_b64": None, "_truth": []}
        frames += [blank] * (c4.MAX_MISSES + 2)
    return frames


def main():
    global OPTS, FRAMES
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("labels", nargs="*", help="시뮬레이터 labels_*.json (생략하면 data/sim/labels_*.json)")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--fps", type=float, default=10)
    ap.add_argument("--frames", type=int, default=70, help="캡처 1장당 프레임 수")
    ap.add_argument("--dx", type=float, default=13)
    ap.add_argument("--drop", type=float, default=0.05, help="C1 드롭 확률")
    ap.add_argument("--tunnel-delay", type=float, default=4, help="공개 URL 이 생기기까지 초")
    OPTS = ap.parse_args()
    labels = OPTS.labels or sorted(str(p) for p in (ROOT / "data" / "sim").glob("labels_*.json"))
    if not labels:
        sys.exit("labels_*.json 이 없습니다. 시뮬레이터에서 일시정지 후 S, L 로 저장해 data/sim/ 에 넣으세요.")
    FRAMES = load_frames(labels)
    print(f"프레임 {len(FRAMES)}장 준비 → http://localhost:{OPTS.port}")
    app.mount("/", StaticFiles(directory=ROOT / "web", html=True), name="web")
    uvicorn.run(app, host="127.0.0.1", port=OPTS.port, log_level="warning")


if __name__ == "__main__":
    main()
