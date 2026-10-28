"""C3 /infer · C4 /track 가 계약(shared/schemas.py)을 지키는지 검사한다. 통합 전에 B·C 브랜치마다 실행.

사용:
  python tools/contract_check.py --c3 http://localhost:8003 --c4 http://localhost:8004
  (한쪽만 검사하려면 하나만 지정. dev 오버라이드로 띄운 스텁에도 그대로 통과해야 한다)
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
from pathlib import Path

import httpx
from pydantic import ValidationError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from shared.schemas import InferResult, TrackConfig, TrackRequest, TrackResponse  # noqa: E402

SAMPLES = ROOT / "dev" / "samples"
results: list[tuple[bool, str]] = []


def check(ok: bool, name: str, detail: str = ""):
    results.append((ok, name))
    print(f"{'PASS' if ok else 'FAIL'}  {name}{('  — ' + detail) if detail and not ok else ''}")


def check_c3(base: str):
    c = httpx.Client(base_url=base.rstrip("/"), timeout=30)
    frame = (SAMPLES / "frame.jpg").read_bytes()
    hdr = {"Content-Type": "image/jpeg", "X-Session": "contract1", "X-Seq": "7", "X-Ts-Recv": "0"}
    r = c.get("/health")
    check(r.status_code == 200 and r.json().get("status") == "ok", "c3 GET /health")
    r = c.post("/infer", content=frame, headers={**hdr, "X-Mode": "fast"})
    try:
        res = InferResult.model_validate_json(r.content)
        check(r.status_code == 200, "c3 fast → InferResult")
        check(res.seq == 7 and res.session == "contract1", "c3 echoes X-Session / X-Seq")
        check(res.mode == "fast", "c3 mode=fast")
        check(all(o.defect is None for o in res.objects if o.partial), "c3 partial 객체는 defect=null")
        check(all(len(o.contour) <= 64 for o in res.objects), "c3 contour ≤ 64점")
        check(len(res.objects) > 0, "c3 sample 프레임에서 객체 검출", f"{len(res.objects)} objects")
    except ValidationError as e:
        check(False, "c3 fast → InferResult", str(e)[:300])
        res = None
    r = c.post("/infer", content=(SAMPLES / "frame_corrupt.bin").read_bytes(), headers={**hdr, "X-Mode": "fast"})
    check(r.status_code == 422 and r.json().get("error") == "DECODE_FAILED", "c3 깨진 JPEG → 422 DECODE_FAILED")
    r = c.post("/infer", content=frame, headers={**hdr, "X-Mode": "precise"})
    if r.status_code == 409:
        check(r.json().get("error") == "PRECISE_UNAVAILABLE", "c3 precise 미지원 → 409 PRECISE_UNAVAILABLE")
    else:
        try:
            check(InferResult.model_validate_json(r.content).mode == "precise", "c3 precise → InferResult(mode=precise)")
        except ValidationError as e:
            check(False, "c3 precise → InferResult", str(e)[:300])
    return res


def check_c4(base: str, infer: InferResult | None):
    c = httpx.Client(base_url=base.rstrip("/"), timeout=30)
    infer = infer or InferResult.model_validate_json((SAMPLES / "infer_fast.json").read_text())
    img = base64.b64encode((SAMPLES / "frame.jpg").read_bytes()).decode()
    sid = "contract2"
    c.delete(f"/sessions/{sid}")
    r = c.get("/health")
    check(r.status_code == 200 and r.json().get("status") == "ok", "c4 GET /health")

    def track(seq: int, mode: str = "fast", image: bool = True):
        req = TrackRequest(session=sid, seq=seq, mode=mode, config=TrackConfig(),
                           infer=infer.model_copy(update={"seq": seq, "session": sid, "mode": mode}),
                           image_jpeg_b64=img if image else None)
        return c.post("/track", content=req.model_dump_json(), headers={"Content-Type": "application/json"})

    r = track(10)
    try:
        tr = TrackResponse.model_validate_json(r.content)
        check(r.status_code == 200 and not tr.stale, "c4 fast → TrackResponse")
        check(tr.image_jpeg_b64 is not None and base64.b64decode(tr.image_jpeg_b64)[:2] == b"\xff\xd8",
              "c4 마킹 이미지가 JPEG")
        check(all(t.verdict in ("ok", "ng", "pending") for t in tr.tracks), "c4 verdict 값")
    except ValidationError as e:
        check(False, "c4 fast → TrackResponse", str(e)[:300])
    r = track(9)
    check(r.status_code == 200 and TrackResponse.model_validate_json(r.content).stale, "c4 늦게 온 fast(seq↓) → stale=true")
    r = track(10, mode="precise", image=False)
    check(r.status_code == 200 and not TrackResponse.model_validate_json(r.content).stale,
          "c4 precise(같은 seq, 이미지 없음) → 처리")
    r = c.post(f"/reset/{sid}")
    check(r.status_code == 200 and r.json() == {"ok": True}, "c4 POST /reset")
    r = track(11)
    check(TrackResponse.model_validate_json(r.content).counts.total == 0, "c4 reset 후 counts 0")
    r = c.delete(f"/sessions/{sid}")
    check(r.status_code == 200 and r.json() == {"ok": True}, "c4 DELETE /sessions")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--c3")
    p.add_argument("--c4")
    a = p.parse_args()
    if not (a.c3 or a.c4):
        p.error("--c3 또는 --c4 를 지정")
    infer = None
    try:
        if a.c3:
            infer = check_c3(a.c3)
        if a.c4:
            check_c4(a.c4, infer)
    except httpx.HTTPError as e:
        check(False, "연결", repr(e))
    failed = [n for ok, n in results if not ok]
    print(json.dumps({"passed": len(results) - len(failed), "failed": failed}, ensure_ascii=False))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
