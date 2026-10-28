"""precise: SAM2ImagePredictor + box prompt 일괄 입력 (docs/05-detection.md §3).

torch/sam2 는 WITH_SAM2=1 로 빌드한 이미지에만 있으므로 load() 안에서 import 한다.
"""
import os
import threading

import cv2
import numpy as np

from shared.schemas import InferObject

from ..features import EDGE_PX, analyze
from . import opencv_backend

NAME = "sam2"
BOX_PAD = 4  # fast 마스크가 가장자리를 조금 덜 잡는 것을 보정

_predictor = None
_torch = None
# 인코더가 OMP 스레드를 모두 쓰므로 복제본 하나에서 동시에 두 번 돌리지 않는다
_lock = threading.Lock()


def load(ckpt: str, cfg: str) -> None:
    global _predictor, _torch
    import torch
    from sam2.build_sam import build_sam2
    from sam2.sam2_image_predictor import SAM2ImagePredictor

    torch.set_num_threads(int(os.getenv("OMP_NUM_THREADS", "2")))
    _predictor = SAM2ImagePredictor(build_sam2(cfg, ckpt, device="cpu"))
    _torch = torch


def loaded() -> bool:
    return _predictor is not None


def infer(bgr: np.ndarray, circ_th: float, solid_th: float) -> list[InferObject]:
    h, w = bgr.shape[:2]
    # 박스 후보는 fast 경로에서. 잘린 과자(partial)는 판정하지 않으므로 SAM 2에 넣지 않는다.
    boxes = []
    for c in opencv_backend.find_contours(bgr):
        x, y, bw, bh = cv2.boundingRect(c)
        if x <= EDGE_PX or y <= EDGE_PX or x + bw >= w - EDGE_PX or y + bh >= h - EDGE_PX:
            continue
        boxes.append([max(0, x - BOX_PAD), max(0, y - BOX_PAD),
                      min(w - 1, x + bw + BOX_PAD), min(h - 1, y + bh + BOX_PAD)])
    if not boxes:
        return []

    rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
    with _lock, _torch.inference_mode():
        _predictor.set_image(rgb)  # 인코더 1회 (대부분의 시간)
        masks, scores, _ = _predictor.predict(box=np.array(boxes, np.float32),
                                              multimask_output=False)
    # 박스 1개면 (1,H,W)/(1,), N개면 (N,1,H,W)/(N,1) → (N,H,W)/(N,)
    masks = np.asarray(masks).reshape(len(boxes), h, w)
    scores = np.asarray(scores).reshape(len(boxes))

    out = []
    for m, s in zip(masks, scores):
        cs, _ = cv2.findContours((m > 0).astype(np.uint8), cv2.RETR_EXTERNAL,
                                 cv2.CHAIN_APPROX_NONE)
        if not cs:
            continue
        obj = analyze(max(cs, key=cv2.contourArea), w, h, circ_th, solid_th, score=float(s))
        if obj is not None:
            out.append(obj)
    return out
