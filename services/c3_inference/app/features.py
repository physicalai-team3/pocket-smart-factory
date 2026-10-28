"""컨투어 → circularity, solidity, partial 판정 (fast/precise 공통)."""
import math
from typing import Optional

import cv2
import numpy as np

from shared.schemas import InferObject

EDGE_PX = 2  # 프레임 경계에서 이 거리 안에 닿으면 partial
MAX_POINTS = 64  # 응답 contour 최대 점 수 (03-messages §4.2)


def _simplify(contour: np.ndarray, perimeter: float) -> np.ndarray:
    """점이 MAX_POINTS 이하가 될 때까지 epsilon을 키워 approxPolyDP."""
    eps = 0.005 * perimeter
    approx = cv2.approxPolyDP(contour, eps, True)
    while len(approx) > MAX_POINTS:
        eps *= 1.5
        approx = cv2.approxPolyDP(contour, eps, True)
    return approx


def analyze(contour: np.ndarray, w: int, h: int, circ_th: float, solid_th: float,
            score: float = 1.0) -> Optional[InferObject]:
    """컨투어 하나를 InferObject로. 면적이 0이면 None."""
    area = float(cv2.contourArea(contour))
    if area <= 0:
        return None

    # 둘레는 픽셀 계단 노이즈에 민감 → approxPolyDP(ε=0.005·P) 후 계산 (05-detection §4)
    approx = cv2.approxPolyDP(contour, 0.005 * cv2.arcLength(contour, True), True)
    perimeter = float(cv2.arcLength(approx, True))
    circularity = 4 * math.pi * area / (perimeter * perimeter) if perimeter > 0 else 0.0

    hull_area = float(cv2.contourArea(cv2.convexHull(contour)))
    solidity = area / hull_area if hull_area > 0 else 0.0

    x, y, bw, bh = cv2.boundingRect(contour)
    partial = x <= EDGE_PX or y <= EDGE_PX or x + bw >= w - EDGE_PX or y + bh >= h - EDGE_PX

    m = cv2.moments(contour)
    centroid = (m["m10"] / m["m00"], m["m01"] / m["m00"]) if m["m00"] else (x + bw / 2, y + bh / 2)

    pts = _simplify(contour, perimeter).reshape(-1, 2)
    return InferObject(
        bbox=(x, y, bw, bh),
        centroid=(round(centroid[0], 1), round(centroid[1], 1)),
        area=area,
        perimeter=round(perimeter, 1),
        circularity=round(circularity, 4),
        solidity=round(solidity, 4),
        contour=[(int(px), int(py)) for px, py in pts],
        partial=partial,
        defect=None if partial else bool(circularity < circ_th or solidity < solid_th),
        score=round(score, 4),
    )
