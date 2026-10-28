"""C3 Inference — 과자 마스크 + 원형도/볼록도 판정. 무상태, 복제 가능.

명세: docs/02-containers.md#c3-inference, 로직: docs/05-detection.md
메시지: docs/03-messages.md §4.2
"""
import logging
import os
import socket
import time

import cv2
import numpy as np
from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from shared.schemas import ErrorBody, InferResult

from .backends import mock_backend, opencv_backend, sam2_backend

log = logging.getLogger("uvicorn.error")
app = FastAPI(title="c3-inference")

REPLICA = socket.gethostname()
C3_BACKEND = os.getenv("C3_BACKEND", "fast")
SAM2_ENABLED = os.getenv("SAM2_ENABLED", "0") == "1"
SAM2_CKPT = os.getenv("SAM2_CKPT", "/models/sam2.1_hiera_tiny.pt")
SAM2_CFG = os.getenv("SAM2_CFG", "configs/sam2.1/sam2.1_hiera_t.yaml")
CIRC_TH = float(os.getenv("CIRC_TH", "0.935"))
SOLID_TH = float(os.getenv("SOLID_TH", "0.95"))

MODES = ("mock", "fast", "precise")


def _error(status: int, code: str, message: str = "") -> JSONResponse:
    return JSONResponse(ErrorBody(error=code, message=message).model_dump(), status_code=status)


@app.on_event("startup")
async def load_models():
    if not SAM2_ENABLED:
        return
    try:
        sam2_backend.load(SAM2_CKPT, SAM2_CFG)
        log.info("SAM 2 loaded: %s", SAM2_CKPT)
    except Exception:
        # 로드 실패해도 fast/mock 은 계속 서비스한다. precise 요청은 409.
        log.exception("SAM 2 load failed")


@app.get("/health")
async def health():
    return {"status": "ok", "service": "c3-inference", "replica": REPLICA,
            "backend": C3_BACKEND, "sam2": sam2_backend.loaded()}


def _run(mode: str, body: bytes, seq: int, circ_th: float, solid_th: float):
    bgr = cv2.imdecode(np.frombuffer(body, np.uint8), cv2.IMREAD_COLOR) if body else None
    if bgr is None:
        return None
    h, w = bgr.shape[:2]
    if mode == "mock":
        return w, h, mock_backend.NAME, mock_backend.infer(w, h, seq, circ_th, solid_th)
    if mode == "precise":
        return w, h, sam2_backend.NAME, sam2_backend.infer(bgr, circ_th, solid_th)
    return w, h, opencv_backend.NAME, opencv_backend.infer(bgr, circ_th, solid_th)


@app.post("/infer", response_model=InferResult)
async def infer(request: Request):
    hd = request.headers
    mode = hd.get("x-mode") or C3_BACKEND
    if mode not in MODES:
        return _error(400, "BAD_REQUEST", f"X-Mode must be one of {MODES}")
    try:
        seq = int(hd.get("x-seq", "0"))
        circ_th = float(hd.get("x-circ-th") or CIRC_TH)
        solid_th = float(hd.get("x-solid-th") or SOLID_TH)
    except ValueError as e:
        return _error(400, "BAD_REQUEST", str(e))
    if mode == "precise" and not sam2_backend.loaded():
        return _error(409, "PRECISE_UNAVAILABLE", "SAM 2 is not loaded on this replica")

    body = await request.body()
    t0 = time.perf_counter()
    try:
        out = await run_in_threadpool(_run, mode, body, seq, circ_th, solid_th)
    except Exception as e:
        log.exception("inference failed")
        return _error(500, "MODEL_ERROR", repr(e))
    if out is None:
        return _error(422, "DECODE_FAILED", "cv2.imdecode returned None")
    w, h, backend, objects = out

    return InferResult(
        session=hd.get("x-session", ""),
        seq=seq,
        mode="precise" if mode == "precise" else "fast",  # mock 은 fast 대역
        backend=backend,
        replica=REPLICA,
        w=w,
        h=h,
        infer_ms=round((time.perf_counter() - t0) * 1000, 1),
        objects=objects,
    )
