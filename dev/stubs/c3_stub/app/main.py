"""C3 스텁 — POST /infer 계약(InferResult)을 지키는 더미. A 파트 개발용이며 판정 품질은 목표가 아니다.

환경변수로 일부러 나쁘게 만들 수 있다 (docs: A 파트 개발 계획 › 더미 구성요소 명세):
  STUB_LATENCY_MS, STUB_JITTER_MS, STUB_PRECISE_LATENCY_MS, STUB_FAIL_RATE, STUB_PRECISE, STUB_OBJECTS
"""
from __future__ import annotations

import asyncio
import math
import os
import random
import socket
import time

import cv2
import numpy as np
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from shared.schemas import InferObject, InferResult

app = FastAPI(title="c3-stub")

REPLICA = socket.gethostname()
DEFAULT_MODE = os.getenv("C3_BACKEND", "fast")
LATENCY_MS = float(os.getenv("STUB_LATENCY_MS", "15"))
JITTER_MS = float(os.getenv("STUB_JITTER_MS", "10"))
PRECISE_LATENCY_MS = float(os.getenv("STUB_PRECISE_LATENCY_MS", "2000"))
FAIL_RATE = float(os.getenv("STUB_FAIL_RATE", "0"))
PRECISE_ON = os.getenv("STUB_PRECISE", "1") == "1"
OBJECTS = os.getenv("STUB_OBJECTS", "detect")  # fixed | moving | detect
CIRC_TH = float(os.getenv("CIRC_TH", "0.935"))
SOLID_TH = float(os.getenv("SOLID_TH", "0.95"))
MIN_AREA_RATIO = float(os.getenv("MIN_AREA_RATIO", "0.0005"))

# 실제 C3 처럼 복제본 하나가 요청을 한 번에 하나씩 처리하게 만든다 (라운드로빈 문제를 드러내기 위해)
_busy = asyncio.Lock()


def _err(status: int, code: str, message: str = "") -> JSONResponse:
    return JSONResponse({"error": code, "message": message}, status_code=status)


def _features(c: np.ndarray, w: int, h: int, circ_th: float, solid_th: float) -> InferObject:
    area = float(cv2.contourArea(c))
    approx = cv2.approxPolyDP(c, 0.005 * cv2.arcLength(c, True), True)
    perim = float(cv2.arcLength(approx, True))
    hull = float(cv2.contourArea(cv2.convexHull(c))) or 1.0
    circ = 4 * math.pi * area / (perim * perim) if perim else 0.0
    solid = area / hull
    x, y, bw, bh = cv2.boundingRect(c)
    m = cv2.moments(c)
    cx, cy = (m["m10"] / m["m00"], m["m01"] / m["m00"]) if m["m00"] else (x + bw / 2, y + bh / 2)
    partial = x <= 1 or y <= 1 or x + bw >= w - 1 or y + bh >= h - 1
    pts = approx.reshape(-1, 2)
    if len(pts) > 64:
        pts = pts[np.linspace(0, len(pts) - 1, 64).astype(int)]
    return InferObject(bbox=(x, y, bw, bh), centroid=(round(cx, 1), round(cy, 1)), area=area,
                       perimeter=round(perim, 1), circularity=round(circ, 4), solidity=round(solid, 4),
                       contour=[(int(px), int(py)) for px, py in pts], partial=partial,
                       defect=None if partial else (circ < circ_th or solid < solid_th))


def detect(img: np.ndarray, circ_th: float, solid_th: float) -> list[InferObject]:
    """V 채널 Otsu + 컨투어. 어두운 벨트 위 밝은 과자라서 이 정도면 스텁으로 충분하다."""
    h, w = img.shape[:2]
    v = cv2.GaussianBlur(cv2.cvtColor(img, cv2.COLOR_BGR2HSV)[:, :, 2], (5, 5), 0)
    _, mask = cv2.threshold(v, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
    mask = cv2.morphologyEx(cv2.morphologyEx(mask, cv2.MORPH_OPEN, k), cv2.MORPH_CLOSE, k, iterations=2)
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    min_area = MIN_AREA_RATIO * w * h
    return [_features(c, w, h, circ_th, solid_th) for c in contours if cv2.contourArea(c) >= min_area]


def fixed_objects(w: int, h: int, seq: int, moving: bool) -> list[InferObject]:
    d = max(8, int(min(w, h) * 0.17))
    objs = []
    for i in range(3 if moving else 2):
        x = int((w - (seq * 9 + i * 170) % (w + d))) if moving else int(w * (0.3 + 0.35 * i))
        y = int(h * (0.15 + 0.3 * i))
        c = np.array([[x + d / 2 + d / 2 * math.cos(a), y + d / 2 + d / 2 * math.sin(a)]
                      for a in np.linspace(0, 2 * math.pi, 32, endpoint=False)], np.int32).reshape(-1, 1, 2)
        o = _features(c, w, h, CIRC_TH, SOLID_TH)
        objs.append(o.model_copy(update={"defect": None if o.partial else (i == 1)}))
    return objs


@app.get("/health")
async def health():
    return {"status": "ok", "service": "c3-inference", "replica": REPLICA, "backend": "stub", "sam2": PRECISE_ON}


@app.post("/infer")
async def infer(request: Request):
    body = await request.body()
    hdr = request.headers
    mode = hdr.get("x-mode") or DEFAULT_MODE
    if mode not in ("fast", "precise"):
        return _err(400, "BAD_MODE", mode)
    if mode == "precise" and not PRECISE_ON:
        return _err(409, "PRECISE_UNAVAILABLE", "STUB_PRECISE=0")
    img = cv2.imdecode(np.frombuffer(body, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        return _err(422, "DECODE_FAILED", "cv2.imdecode returned None")
    t0 = time.perf_counter()
    async with _busy:
        delay = PRECISE_LATENCY_MS if mode == "precise" else LATENCY_MS
        await asyncio.sleep(max(0.0, delay + random.uniform(-JITTER_MS, JITTER_MS)) / 1000)
        if random.random() < FAIL_RATE:
            return _err(500, "MODEL_ERROR", "injected failure (STUB_FAIL_RATE)")
        h, w = img.shape[:2]
        seq = int(hdr.get("x-seq", "0"))
        circ_th = float(hdr.get("x-circ-th", CIRC_TH))
        solid_th = float(hdr.get("x-solid-th", SOLID_TH))
        objs = detect(img, circ_th, solid_th) if OBJECTS == "detect" else fixed_objects(w, h, seq, OBJECTS == "moving")
        if mode == "precise":
            objs = [o.model_copy(update={"score": 0.9}) for o in objs]
    return InferResult(session=hdr.get("x-session", ""), seq=seq, mode=mode, backend="stub", replica=REPLICA,
                       w=w, h=h, infer_ms=round((time.perf_counter() - t0) * 1000, 1), objects=objs)
