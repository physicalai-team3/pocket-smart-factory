"""fast: HSV 임계값 → morphology → findContours → 원형도/볼록도 (docs/05-detection.md §2, §4)."""
import os

import cv2
import numpy as np

from shared.schemas import InferObject

from ..features import analyze

NAME = "opencv"
# 0.002 는 지름 40 슬라이더의 반쪽(half) 과자까지 버림. 0.0005 에서도 오검출 0 (tools/eval_offline.py)
MIN_AREA_RATIO = float(os.getenv("MIN_AREA_RATIO", "0.0005"))

# 과자 갈색 (OpenCV H는 0~180). 시뮬레이터 과자는 H≈12~17.
# 빨간 검사선(H≈3)과 라인 번호판 노란색(H≈22)은 범위 밖.
HSV_LO = np.array([6, 70, 35], np.uint8)
HSV_HI = np.array([20, 255, 255], np.uint8)
_KERNEL = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
MAX_ASPECT = 3.0


def find_contours(bgr: np.ndarray) -> list[np.ndarray]:
    """과자 후보 외곽선 (면적 필터까지)."""
    h, w = bgr.shape[:2]
    blur = cv2.GaussianBlur(bgr, (5, 5), 0)  # 화면 촬영 모아레·줄무늬 완화
    mask = cv2.inRange(cv2.cvtColor(blur, cv2.COLOR_BGR2HSV), HSV_LO, HSV_HI)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, _KERNEL)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, _KERNEL)
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    min_area = MIN_AREA_RATIO * w * h
    return [c for c in contours if cv2.contourArea(c) >= min_area and _roundish(c)]


def _roundish(c: np.ndarray) -> bool:
    """반쪽 과자도 2:1 정도. 그보다 훨씬 가늘면 라인 번호판 글자·화면 가장자리 조각."""
    _, _, bw, bh = cv2.boundingRect(c)
    return max(bw, bh) <= MAX_ASPECT * min(bw, bh)


def infer(bgr: np.ndarray, circ_th: float, solid_th: float) -> list[InferObject]:
    h, w = bgr.shape[:2]
    objs = (analyze(c, w, h, circ_th, solid_th) for c in find_contours(bgr))
    return [o for o in objs if o is not None]
