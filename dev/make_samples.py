"""dev/samples/ 예제 메시지 생성 — 손으로 쓰지 않고 shared/schemas.py 모델로 만든다.

계약이 바뀌면 다시 실행:  python dev/make_samples.py
"""
from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dev.synth import Scene, camera_view, encode_jpeg, resize_long  # noqa: E402
from shared.schemas import (  # noqa: E402
    Counts, CountedEvent, InferObject, InferResult, TrackOut, TrackResponse, Votes, pack_envelope,
)

OUT = ROOT / "dev" / "samples"


def _obj(x, y, d, circ, solid, partial=False, defect=False, score=1.0) -> InferObject:
    r = d / 2
    contour = [(int(x + r + r * c), int(y + r + r * s)) for c, s in
               ((1, 0), (0.7, 0.7), (0, 1), (-0.7, 0.7), (-1, 0), (-0.7, -0.7), (0, -1), (0.7, -0.7))]
    area = 3.1416 * r * r * (circ if defect else 1)
    return InferObject(bbox=(x, y, d, d), centroid=(x + r, y + r), area=round(area, 1),
                       perimeter=round(3.1416 * d, 1), circularity=circ, solidity=solid, contour=contour,
                       partial=partial, defect=None if partial else defect, score=score)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    scene = Scene(seed=7)
    for _ in range(60):
        scene.step(0.1)
    canvas, _ = scene.render()
    frame = resize_long(canvas, 960)
    h, w = frame.shape[:2]
    (OUT / "frame.jpg").write_bytes(encode_jpeg(frame, 85))
    cam, corners = camera_view(canvas, seed=7)
    (OUT / "frame_camera.jpg").write_bytes(encode_jpeg(cam, 70))
    (OUT / "frame_camera_corners.json").write_text(json.dumps({"roi": corners}, indent=2))
    (OUT / "frame_corrupt.bin").write_bytes(b"\x00not-a-jpeg" * 32)

    objs = [_obj(400, 30, 57, 0.982, 0.998),
            _obj(560, 140, 57, 0.71, 0.97, defect=True),
            _obj(0, 250, 40, 0.80, 0.90, partial=True)]
    fast = InferResult(session="k3x9q2ab", seq=42, mode="fast", backend="mock", replica="c3-sample",
                       w=w, h=h, infer_ms=14.2, objects=objs)
    precise = fast.model_copy(update={"mode": "precise", "backend": "sam2", "infer_ms": 1830.0,
                                      "objects": [o.model_copy(update={"score": 0.93}) for o in objs[:2]]})
    empty = fast.model_copy(update={"objects": []})

    counts = Counts(total=37, ok=26, ng=11)
    tracks = [TrackOut(track_id=88, bbox=(560, 140, 57, 57), verdict="ng", counted=False,
                       votes=Votes(fast_ok=1, fast_ng=4)),
              TrackOut(track_id=89, bbox=(400, 30, 57, 57), verdict="ok", counted=False,
                       votes=Votes(fast_ok=5))]
    img_b64 = base64.b64encode((OUT / "frame.jpg").read_bytes()).decode()
    ok = TrackResponse(session="k3x9q2ab", seq=42, counts=counts, tracks=tracks,
                       events=[CountedEvent(track_id=85, verdict="ok", seq=42)],
                       image_jpeg_b64=img_b64, proc_ms=7.9)
    stale = TrackResponse(session="k3x9q2ab", seq=41, stale=True, counts=counts)
    needs = ok.model_copy(update={"tracks": [tracks[0].model_copy(update={"needs_precise": True}), tracks[1]]})

    def dump(name, model):
        (OUT / name).write_text(model.model_dump_json(indent=2), encoding="utf-8")

    dump("infer_fast.json", fast)
    dump("infer_precise.json", precise)
    dump("infer_empty.json", empty)
    dump("track_ok.json", ok)
    dump("track_stale.json", stale)
    dump("track_needs_precise.json", needs)

    header = {"type": "result", "seq": 42, "w": w, "h": h, "counts": counts.model_dump(),
              "tracks": [t.model_dump(include={"track_id", "bbox", "verdict", "counted", "needs_precise"})
                         for t in tracks],
              "events": [e.model_dump() for e in ok.events],
              "latency_ms": {"c2": 9, "c3": 22, "c4": 8, "total": 61}}
    (OUT / "dashboard_result.json").write_text(json.dumps(header, indent=2), encoding="utf-8")
    # phone.html pack() 과 같은 배치: uint32 BE 헤더 길이 + 헤더 JSON + JPEG
    env_header = {"type": "frame", "client_seq": 512, "ts_capture": 1761620001234.5, "w": w, "h": h}
    (OUT / "envelope_frame.bin").write_bytes(pack_envelope(env_header, (OUT / "frame.jpg").read_bytes()))
    total = sum(p.stat().st_size for p in OUT.iterdir())
    print(f"wrote {len(list(OUT.iterdir()))} files to {OUT} ({total / 1024:.0f} KiB)")


if __name__ == "__main__":
    main()
