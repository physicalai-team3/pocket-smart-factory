import json

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.main import app, parse_roi, PreprocessError
from conftest import ROOT

SAMPLES = ROOT / "dev" / "samples"
client = TestClient(app)


def _decode(b: bytes) -> np.ndarray:
    return cv2.imdecode(np.frombuffer(b, np.uint8), cv2.IMREAD_COLOR)


def test_resize_without_roi():
    r = client.post("/preprocess", content=(SAMPLES / "frame_camera.jpg").read_bytes())
    assert r.status_code == 200
    assert r.headers["x-roi-applied"] == "0"
    img = _decode(r.content)
    assert max(img.shape[:2]) == 960
    assert (int(r.headers["x-width"]), int(r.headers["x-height"])) == (img.shape[1], img.shape[0])


def test_roi_recovers_canvas():
    """합성 카메라 프레임 + 정답 모서리 → 보정 결과가 원본 캔버스와 거의 같아야 한다."""
    corners = json.loads((SAMPLES / "frame_camera_corners.json").read_text())["roi"]
    roi = ",".join(f"{v:.5f}" for p in corners for v in p)
    r = client.post("/preprocess", content=(SAMPLES / "frame_camera.jpg").read_bytes(), headers={"X-Roi": roi})
    assert r.status_code == 200 and r.headers["x-roi-applied"] == "1"
    out = _decode(r.content)
    ref = _decode((SAMPLES / "frame.jpg").read_bytes())
    # 가로세로 비율은 원본 캔버스(960x328)와 2% 이내
    assert abs(out.shape[1] / out.shape[0] - ref.shape[1] / ref.shape[0]) / (ref.shape[1] / ref.shape[0]) < 0.02
    out = cv2.resize(out, (ref.shape[1], ref.shape[0]))
    diff = np.abs(cv2.GaussianBlur(out, (5, 5), 0).astype(int) - cv2.GaussianBlur(ref, (5, 5), 0).astype(int))
    assert diff.mean() < 12, diff.mean()


def test_decode_failed():
    r = client.post("/preprocess", content=(SAMPLES / "frame_corrupt.bin").read_bytes())
    assert r.status_code == 422 and r.json()["error"] == "DECODE_FAILED"


@pytest.mark.parametrize("roi", [
    "0.1,0.1,0.9,0.1,0.9,0.9",                    # 점 6개
    "0.1,0.1,0.9,0.1,0.9,0.9,a,b",                # 숫자 아님
    "0.1,0.1,0.5,0.1,0.9,0.1,0.1,0.9",            # 세 점이 한 직선
    "0.1,0.1,0.9,0.9,0.9,0.1,0.1,0.9",            # 꼬인 사각형
    "-0.5,0.1,0.9,0.1,0.9,0.9,0.1,0.9",           # 범위 밖
])
def test_bad_roi(roi):
    with pytest.raises(PreprocessError) as e:
        parse_roi(roi)
    assert e.value.code == "BAD_ROI"
    r = client.post("/preprocess", content=(SAMPLES / "frame.jpg").read_bytes(), headers={"X-Roi": roi})
    assert r.status_code == 400 and r.json()["error"] == "BAD_ROI"


def test_target_width_header():
    r = client.post("/preprocess", content=(SAMPLES / "frame.jpg").read_bytes(), headers={"X-Target-Width": "640"})
    assert max(_decode(r.content).shape[:2]) == 640
