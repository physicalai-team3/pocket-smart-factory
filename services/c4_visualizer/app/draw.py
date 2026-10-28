"""결과 오버레이: 윤곽선(ok 초록 / ng 빨강 / pending 노랑), 트랙 ID, 검사선, 카운트."""
from __future__ import annotations

import base64
import logging

import cv2
import numpy as np

from shared.schemas import CountedEvent, Counts, InferObject, TrackConfig, TrackOut

log = logging.getLogger("c4.draw")

# BGR. ok/ng 는 시뮬레이터 정답 오버레이 색과 같다
COLORS = {"ok": (138, 192, 87), "ng": (69, 90, 255), "pending": (31, 178, 232)}
LINE_COLOR = (255, 200, 0)  # 시뮬레이터의 빨간 검사선과 겹쳐도 구분되도록 하늘색
FONT = cv2.FONT_HERSHEY_SIMPLEX


def _text(img, s, org, scale, color, thick=1):
    """어두운 벨트·밝은 과자 위 어디서든 읽히도록 검은 테두리를 먼저 그린다."""
    cv2.putText(img, s, org, FONT, scale, (0, 0, 0), thick + 2, cv2.LINE_AA)
    cv2.putText(img, s, org, FONT, scale, color, thick, cv2.LINE_AA)


def _dashed_vline(img, x, color, thick, dash, gap):
    for y in range(0, img.shape[0], dash + gap):
        cv2.line(img, (x, y), (x, min(y + dash, img.shape[0] - 1)), color, thick, cv2.LINE_AA)


def render(jpeg_b64: str, w: int, h: int, items: list[tuple[TrackOut, InferObject]], cfg: TrackConfig,
           counts: Counts, events: list[CountedEvent], quality: int) -> str | None:
    """마킹한 JPEG(base64)를 돌려준다. 디코드에 실패하면 원본을 그대로 돌려준다."""
    try:
        img = cv2.imdecode(np.frombuffer(base64.b64decode(jpeg_b64), np.uint8), cv2.IMREAD_COLOR)
    except (ValueError, cv2.error):  # 잘못된 base64 / 빈 버퍼
        img = None
    if img is None:
        log.warning("image decode failed; returning original")
        return jpeg_b64

    ih, iw = img.shape[:2]
    sx, sy = iw / w, ih / h  # infer 좌표(C2 출력)와 이미지 크기가 다르면 맞춘다
    u = iw / 960  # 글자·선 굵기 기준 (960px 폭에서 1)
    fs, lw = max(0.35, 0.45 * u), max(1, round(1.5 * u))

    # 검사선: 이번 프레임에 카운트가 있으면 마지막 판정 색으로 굵게 깜빡인다
    lx = round(cfg.line_x * iw)
    if events:
        cv2.line(img, (lx, 0), (lx, ih - 1), COLORS[events[-1].verdict], lw * 3, cv2.LINE_AA)
    else:
        _dashed_vline(img, lx, LINE_COLOR, lw, round(10 * u), round(6 * u))

    for t, o in items:
        color = COLORS[t.verdict]
        pts = np.array(o.contour, np.float32).reshape(-1, 1, 2) * (sx, sy)
        # 확정(counted)된 과자는 굵게, 아직 판정 중인 과자는 얇게
        cv2.polylines(img, [pts.round().astype(np.int32)], True, color, lw * (2 if t.counted else 1), cv2.LINE_AA)
        x, y = round(t.bbox[0] * sx), round(t.bbox[1] * sy)
        label = f"{t.track_id} {t.verdict.upper()}" + (" ?" if t.needs_precise else "")
        _text(img, label, (x, max(y - round(4 * u), round(10 * u))), fs, color)

    # 카운트 패널 (좌상단)
    rows = [("TOTAL", counts.total, (240, 240, 240)), ("OK", counts.ok, COLORS["ok"]), ("NG", counts.ng, COLORS["ng"])]
    pfs = fs * 1.2
    (tw, th), _ = cv2.getTextSize("TOTAL", FONT, pfs, 1)  # 글자 크기에 하한이 있어 위치도 글자 폭 기준으로 잡는다
    (nw, _), _ = cv2.getTextSize(str(max(counts.total, 9999)), FONT, pfs, 2)
    pad, rh = max(4, round(8 * u)), round(th * 1.8)
    num_x = pad + tw + pad
    x1, y1 = num_x + nw + pad, pad * 2 + rh * len(rows)
    img[:y1, :x1] = (img[:y1, :x1] * 0.35).astype(np.uint8)  # 반투명: 아래로 지나가는 과자가 비쳐 보이게
    for i, (k, v, c) in enumerate(rows):
        yy = pad + rh * (i + 1) - round(rh * 0.3)
        _text(img, k, (pad, yy), pfs, c)
        _text(img, str(v), (num_x, yy), pfs, c, 2)

    ok, jpg = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, quality])
    return base64.b64encode(jpg.tobytes()).decode("ascii") if ok else jpeg_b64
