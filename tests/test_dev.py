"""dev/ (예제 메시지, 합성 프레임, 스텁) 와 봉투 형식 테스트.  실행: python -m pytest tests"""
import base64
import importlib.util
import json
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dev.synth import Scene, camera_view  # noqa: E402
from shared.schemas import InferResult, TrackRequest, TrackConfig, TrackResponse, pack_envelope, unpack_envelope  # noqa: E402

SAMPLES = ROOT / "dev" / "samples"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("name,model", [
    ("infer_fast.json", InferResult), ("infer_precise.json", InferResult), ("infer_empty.json", InferResult),
    ("track_ok.json", TrackResponse), ("track_stale.json", TrackResponse), ("track_needs_precise.json", TrackResponse),
])
def test_samples_match_contract(name, model):
    """계약이 바뀌었는데 예제를 다시 만들지 않았으면 여기서 실패 → python dev/make_samples.py"""
    model.model_validate_json((SAMPLES / name).read_text(encoding="utf-8"))


def test_envelope_roundtrip_and_js_layout():
    data = (SAMPLES / "envelope_frame.bin").read_bytes()
    header, jpeg = unpack_envelope(data)
    assert header["type"] == "frame" and jpeg == (SAMPLES / "frame.jpg").read_bytes()
    # phone.html pack(): DataView.setUint32 기본값은 big-endian → 첫 4바이트가 헤더 길이
    assert int.from_bytes(data[:4], "big") == len(json.dumps(header, separators=(",", ":")).encode())
    assert unpack_envelope(pack_envelope({"a": "한글"}, b"\xff\xd8x"))[0] == {"a": "한글"}


def test_synth_is_deterministic_and_counts():
    a, b = Scene(seed=3), Scene(seed=3)
    for _ in range(200):
        a.step(0.1)
        b.step(0.1)
    ia, la = a.render()
    ib, lb = b.render()
    assert np.array_equal(ia, ib) and la == lb
    assert a.stats["total"] > 20 and 0 < a.stats["ng"] < a.stats["total"]
    assert {x["category"] for x in la} <= {"round", "deformed"}


def test_camera_corners_inside_frame():
    canvas, _ = Scene(seed=1).render()
    frame, corners = camera_view(canvas, seed=1)
    assert frame.shape == (720, 1280, 3)
    assert all(0 < x < 1 and 0 < y < 1 for x, y in corners)


def test_c3_stub_detects_sample_cookies():
    stub = _load("c3_stub_t", ROOT / "dev" / "stubs" / "c3_stub" / "app" / "main.py")
    import cv2
    img = cv2.imread(str(SAMPLES / "frame.jpg"))
    objs = stub.detect(img, 0.935, 0.95)
    whole = [o for o in objs if not o.partial]
    assert len(whole) >= 9  # sample 프레임에 온전한 과자 9개 이상
    assert all(0.5 < o.circularity <= 1.05 for o in whole)


def test_c4_stub_stale_and_count():
    stub = _load("c4_stub_t", ROOT / "dev" / "stubs" / "c4_stub" / "app" / "main.py")
    from fastapi.testclient import TestClient
    tc = TestClient(stub.app)
    infer = InferResult.model_validate_json((SAMPLES / "infer_fast.json").read_text())
    obj = infer.objects[0]

    def post(seq, x):
        o = obj.model_copy(update={"centroid": (x, obj.centroid[1]), "bbox": (int(x) - 28, obj.bbox[1], 57, 57)})
        req = TrackRequest(session="t", seq=seq, mode="fast", config=TrackConfig(line_x=0.34),
                           infer=infer.model_copy(update={"seq": seq, "objects": [o]}))
        return TrackResponse.model_validate_json(tc.post("/track", content=req.model_dump_json()).content)

    w = infer.w
    assert post(1, w * 0.40).counts.total == 0
    r = post(2, w * 0.33)                     # 검사선(0.34) 을 오른쪽→왼쪽으로 통과
    assert r.counts.total == 1 and r.events[0].verdict == "ok"
    assert post(2, w * 0.30).stale is True    # 같은 seq 다시 → stale
    assert post(3, w * 0.25).counts.total == 1  # 한 번만 센다
