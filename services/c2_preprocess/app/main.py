"""C2 Preprocess — 디코드, ROI 원근 보정, 리사이즈, 재인코딩. 무상태.

명세: docs/02-containers.md#c2-preprocess, 메시지: docs/03-messages.md §4.1
"""
from __future__ import annotations

import os
import time
from typing import Optional

import cv2
import numpy as np
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool

TARGET_WIDTH = int(os.getenv("TARGET_WIDTH", "960"))
JPEG_QUALITY = int(os.getenv("C2_JPEG_QUALITY", "85"))
CLAHE_CLIP = float(os.getenv("CLAHE_CLIP", "0"))  # 0 = 끔. 켤 때는 2.0 권장 (OpenCV 기본 40은 너무 강함)
cv2.setNumThreads(int(os.getenv("CV_THREADS", "2")))  # 시작 시 한 번 (스레드 안전하지 않음)

app = FastAPI(title="c2-preprocess")


class PreprocessError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def parse_roi(value: Optional[str]) -> Optional[np.ndarray]:
    """'x1,y1,...,x4,y4' (0~1 비율, 좌상→우상→우하→좌하) → (4,2) 배열. 없으면 None."""
    if not value:
        return None
    try:
        nums = [float(v) for v in value.split(",")]
    except ValueError:
        raise PreprocessError(400, "BAD_ROI", "ROI must be 8 comma-separated numbers")
    if len(nums) != 8:
        raise PreprocessError(400, "BAD_ROI", f"ROI needs 8 numbers, got {len(nums)}")
    pts = np.array(nums, np.float32).reshape(4, 2)
    if (pts < -0.05).any() or (pts > 1.05).any():
        raise PreprocessError(400, "BAD_ROI", "ROI points must be within 0..1")
    # 네 점이 볼록 사각형이고 같은 방향으로 돌아야 한다 (세 점이 한 직선 위면 cross 가 0)
    cross = []
    for i in range(4):
        a, b = pts[(i + 1) % 4] - pts[i], pts[(i + 2) % 4] - pts[(i + 1) % 4]
        cross.append(float(a[0] * b[1] - a[1] * b[0]))
    if not (all(c > 1e-4 for c in cross) or all(c < -1e-4 for c in cross)):
        raise PreprocessError(400, "BAD_ROI", "ROI must be a convex quadrilateral (TL, TR, BR, BL)")
    return pts


def rectangle_aspect(src: np.ndarray, center: tuple[float, float]) -> float:
    """원근으로 찍힌 사각형의 실제 가로/세로 비율.

    Zhang & He, "Whiteboard scanning and image enhancement" (2007) 의 방법: 주점을 영상 중심으로 두고
    두 소실점에서 초점거리를 구한 뒤 실제 비율을 계산한다. 변이 평행해 소실점이 없으면(정면 촬영)
    변 길이 평균으로 대신한다. 단순 평균은 기울기 15° 에서 비율이 5~10% 틀어진다.
    """
    tl, tr, br, bl = src
    m1, m2, m3, m4 = (np.array([p[0] - center[0], p[1] - center[1], 1.0]) for p in (tl, tr, bl, br))
    k2 = np.dot(np.cross(m1, m4), m3) / np.dot(np.cross(m2, m4), m3)
    k3 = np.dot(np.cross(m1, m4), m2) / np.dot(np.cross(m3, m4), m2)
    n2, n3 = k2 * m2 - m1, k3 * m3 - m1
    fallback = (np.linalg.norm(tr - tl) + np.linalg.norm(br - bl)) / (np.linalg.norm(bl - tl) + np.linalg.norm(br - tr))
    if abs(n2[2]) < 1e-6 or abs(n3[2]) < 1e-6:
        return float(np.hypot(n2[0], n2[1]) / np.hypot(n3[0], n3[1]))
    f2 = -(n2[0] * n3[0] + n2[1] * n3[1]) / (n2[2] * n3[2])
    if f2 <= 0:
        return float(fallback)
    r2 = (n2[0] ** 2 + n2[1] ** 2 + f2 * n2[2] ** 2) / (n3[0] ** 2 + n3[1] ** 2 + f2 * n3[2] ** 2)
    return float(np.sqrt(r2)) if np.isfinite(r2) and r2 > 0 else float(fallback)


def output_size(src: np.ndarray, target: int, center: tuple[float, float]) -> tuple[int, int]:
    """보정 결과 크기: 실제 비율을 유지하면서 긴 변을 target 에 맞춘다."""
    aspect = rectangle_aspect(src, center)
    return (target, max(1, round(target / aspect))) if aspect >= 1 else (max(1, round(target * aspect)), target)


def preprocess_image(jpeg: bytes, roi: Optional[np.ndarray], target: int, clahe_clip: float = CLAHE_CLIP
                     ) -> tuple[bytes, int, int, bool]:
    img = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise PreprocessError(422, "DECODE_FAILED", "cv2.imdecode returned None")
    ih, iw = img.shape[:2]
    if roi is not None:
        src = roi * np.array([iw, ih], np.float32)
        ow, oh = output_size(src, target, (iw / 2, ih / 2))
        dst = np.array([[0, 0], [ow - 1, 0], [ow - 1, oh - 1], [0, oh - 1]], np.float32)
        # 보정과 리사이즈를 warpPerspective 한 번으로 처리
        out = cv2.warpPerspective(img, cv2.getPerspectiveTransform(src, dst), (ow, oh), flags=cv2.INTER_LINEAR)
    else:
        s = target / max(iw, ih)
        out = img if abs(s - 1) < 1e-6 else cv2.resize(
            img, (round(iw * s), round(ih * s)), interpolation=cv2.INTER_AREA if s < 1 else cv2.INTER_LINEAR)
    if clahe_clip > 0:
        lab = cv2.cvtColor(out, cv2.COLOR_BGR2LAB)
        lab[:, :, 0] = cv2.createCLAHE(clipLimit=clahe_clip, tileGridSize=(8, 8)).apply(lab[:, :, 0])
        out = cv2.cvtColor(lab, cv2.COLOR_LAB2BGR)
    ok, buf = cv2.imencode(".jpg", out, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
    if not ok:
        raise PreprocessError(500, "ENCODE_FAILED", "cv2.imencode failed")
    oh, ow = out.shape[:2]
    return buf.tobytes(), ow, oh, roi is not None


@app.get("/health")
def health():
    return {"status": "ok", "service": "c2-preprocess"}


@app.post("/preprocess")
async def preprocess(request: Request):
    t0 = time.perf_counter()
    body = await request.body()
    try:
        target = int(request.headers.get("x-target-width") or TARGET_WIDTH)
        roi = parse_roi(request.headers.get("x-roi"))
        # OpenCV 연산은 스레드풀에서 → 이벤트 루프(다른 요청, /health)를 막지 않는다
        out, w, h, applied = await run_in_threadpool(preprocess_image, body, roi, target)
    except PreprocessError as e:
        return JSONResponse({"error": e.code, "message": e.message}, status_code=e.status)
    except ValueError:
        return JSONResponse({"error": "BAD_REQUEST", "message": "X-Target-Width must be an integer"},
                            status_code=400)
    return Response(out, media_type="image/jpeg", headers={
        "X-Width": str(w), "X-Height": str(h), "X-Roi-Applied": "1" if applied else "0",
        "X-Proc-Ms": f"{(time.perf_counter() - t0) * 1000:.1f}"})
