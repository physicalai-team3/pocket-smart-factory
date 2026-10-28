"""mock: 고정된 가짜 객체 2개 반환 — A가 C3 없이 파이프라인을 개발할 때 사용.

이미지는 보지 않는다. 두 과자(정상 1, 불량 1)가 seq에 따라 오른쪽→왼쪽(rtl)으로
움직이므로 C4 추적·검사선 카운트까지 mock만으로 확인할 수 있다.
"""
import math

import numpy as np

from shared.schemas import InferObject

from ..features import analyze

NAME = "mock"
STEP_PX = 13  # 프레임당 이동량 (05-detection §1: 130 px/s, 10 fps)


def _cookie(cx: float, cy: float, r: float, bite: bool) -> np.ndarray:
    pts = []
    for i in range(48):
        a = 2 * math.pi * i / 48
        rr = r * (0.55 if bite and abs(math.remainder(a, 2 * math.pi)) < 0.6 else 1.0)
        pts.append((cx + rr * math.cos(a), cy + rr * math.sin(a)))
    return np.round(np.array(pts)).astype(np.int32).reshape(-1, 1, 2)


def infer(w: int, h: int, seq: int, circ_th: float, solid_th: float) -> list[InferObject]:
    r = max(8.0, min(w, h) * 0.08)
    span = w + 4 * r
    out = []
    for k, (row, bite) in enumerate(((0.3, False), (0.7, True))):
        # 두 과자는 반 주기 어긋나서 등장
        x = w + 2 * r - ((seq * STEP_PX + k * span / 2) % span)
        if x + r < 0 or x - r > w:  # 화면 밖
            continue
        obj = analyze(_cookie(x, h * row, r, bite), w, h, circ_th, solid_th)
        if obj is not None:
            out.append(obj)
    return out
