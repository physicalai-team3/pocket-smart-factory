"""시뮬레이터 정답(labels_*.json + frame_*.png) → C4 TrackRequest JSON 변환·전송 (C1/C3 없이 C4 개발용).

정답 윤곽선으로 C3 InferResult 를 흉내 낸다 (backend="sim-truth"). 좌표는 C2 출력처럼 --width 로 리사이즈.
정지 화면 1장을 --frames 장의 이동 시퀀스로 늘릴 수 있다 (rtl 로 프레임마다 --dx px 이동,
새 과자는 들어오지 않음). 시퀀스 중 검사선을 넘는 과자 수를 정답 카운트로 함께 기록한다.
--jitter / --miss / --skip 으로 C3 검출 흔들림·검출 누락·C1 프레임 드롭을 흉내 내 추적을 시험한다.

    # 시뮬레이터에서 일시정지 → S, L 저장 후
    python tools/sim2track.py data/sim/labels_*.json --frames 60 --dx 13 --out data/track
    python tools/sim2track.py data/sim/labels_*.json --frames 60 --post http://localhost:8004
    curl -X POST localhost:8004/track -H "Content-Type: application/json" -d @data/track/req_00000.json

출력: <out>/req_<seq>.json (TrackRequest), <out>/manifest.json (seq별 정답 id·bbox, 기대 카운트)
"""
from __future__ import annotations

import argparse
import base64
import json
import math
import re
import sys
import urllib.request
from pathlib import Path

import cv2
import numpy as np

VOID_BGR = (27, 24, 20)  # 시뮬레이터 배경 #14181b
# circ_band·solid_band 는 넣지 않는다: C4 가 계약 기본값(shared/schemas.py TrackConfig)을 쓰게 한다
DEFAULT_CONFIG = {"line_x": 0.34, "direction": "rtl", "circ_th": 0.80, "solid_th": 0.95}


def find_png(labels_path: Path) -> Path | None:
    """labels_<stamp>.json 과 같은 stamp 의 frame PNG. 없으면 같은 폴더에서 수정 시각이 가장 가까운 PNG."""
    stamp = labels_path.stem.removeprefix("labels_")
    same = labels_path.with_name(f"frame_{stamp}.png")
    if same.exists():
        return same
    pngs = list(labels_path.parent.glob("frame_*.png"))
    if not pngs:
        return None
    t = labels_path.stat().st_mtime
    return min(pngs, key=lambda p: abs(p.stat().st_mtime - t))


def features(pts: np.ndarray, w: int, h: int, circ_th: float, solid_th: float, truth_defect: bool | None):
    """C3 와 같은 방식(docs/05-detection.md §4)으로 InferObject 를 만든다."""
    cnt = np.round(pts).astype(np.int32).reshape(-1, 1, 2)
    area = float(cv2.contourArea(cnt))
    if area < 1:
        return None
    approx = cv2.approxPolyDP(cnt, 0.005 * cv2.arcLength(cnt, True), True)
    perim = float(cv2.arcLength(approx, True))
    hull_area = float(cv2.contourArea(cv2.convexHull(cnt)))
    circ = 4 * math.pi * area / (perim * perim) if perim else 0.0
    solid = area / hull_area if hull_area else 0.0
    m = cv2.moments(cnt)
    x, y, bw, bh = cv2.boundingRect(cnt)
    partial = x <= 0 or y <= 0 or x + bw >= w or y + bh >= h
    poly = approx.reshape(-1, 2)
    if len(poly) > 64:
        poly = poly[np.linspace(0, len(poly) - 1, 64).astype(int)]
    if partial:
        defect = None
    elif truth_defect is None:
        defect = circ < circ_th or solid < solid_th
    else:
        defect = truth_defect
    return {
        "bbox": [int(x), int(y), int(bw), int(bh)],
        "centroid": [round(m["m10"] / m["m00"], 1), round(m["m01"] / m["m00"], 1)],
        "area": round(area, 1), "perimeter": round(perim, 1),
        "circularity": round(circ, 4), "solidity": round(solid, 4),
        "contour": poly.tolist(), "partial": partial, "defect": defect, "score": 1.0,
    }


def build(args) -> tuple[list[dict], dict]:
    reqs, truth, seq = [], [], args.start_seq
    rng = np.random.default_rng(args.seed)
    expected = {"total": 0, "ok": 0, "ng": 0}
    for lp in args.labels:
        lp = Path(lp)
        doc = json.loads(lp.read_text(encoding="utf-8"))
        png = Path(args.png) if args.png else find_png(lp)
        src_w, src_h = doc["image"]["width"], doc["image"]["height"]
        img = cv2.imread(str(png)) if png else None
        if img is not None and img.shape[1] != src_w:
            sys.exit(f"{png}: 크기 {img.shape[1]}x{img.shape[0]} 가 labels 의 {src_w}x{src_h} 와 다름 (다른 순간 캡처?)")
        s = args.width / src_w if args.width else 1.0
        w, h = round(src_w * s), round(src_h * s)
        if img is not None and s != 1.0:
            img = cv2.resize(img, (w, h), interpolation=cv2.INTER_AREA)
        print(f"{lp.name}: {len(doc['annotations'])}개, {src_w}x{src_h} → {w}x{h}, png={png.name if png else '없음'}")

        # 캡처 오른쪽 경계에 걸친 과자는 지운다: 이미지를 왼쪽으로 밀면 경계도 같이 움직여서
        # 끝까지 잘린(partial) 채로 남고, 실제 시뮬레이터처럼 온전한 모습이 되지 않기 때문
        anns = []
        for a in doc["annotations"]:
            x0, y0, bw, bh = (np.array(a["bbox"]) * s).round().astype(int)
            if x0 + bw < w:
                anns.append(a)
            elif img is not None:
                cv2.rectangle(img, (x0 - 3, y0 - 3), (w, y0 + bh + 3), VOID_BGR, -1)

        line = DEFAULT_CONFIG["line_x"] * w
        prev_cx: dict[int, float] = {}
        for k in range(args.frames):
            shift = -k * args.dx
            objs, gts = [], []
            for a in anns:
                pts = np.array(a["segmentation"][0], dtype=np.float64).reshape(-1, 2) * s
                pts[:, 0] += shift
                if args.jitter:
                    pts += rng.normal(0, args.jitter, 2)
                if pts[:, 0].max() < 0:
                    continue
                pts[:, 0] = pts[:, 0].clip(0, w - 1)  # 화면 밖은 경계에 붙여 partial 로 만든다
                truth_defect = None if args.defect_from == "features" else a["category"] == "deformed"
                o = features(pts, w, h, args.circ_th, args.solid_th, truth_defect)
                if o is None:
                    continue
                if rng.random() >= args.miss:
                    objs.append(o)
                gts.append({"gt_id": a["id"], "category": a["category"], "bbox": o["bbox"]})
                # 정답 카운트: 화면에 잘리지 않은 전체 윤곽선의 bbox 중심이 검사선을 rtl 로 넘을 때
                # (시뮬레이터는 원 중심 기준이라 불량품은 넘는 시점이 몇 px 다를 수 있지만 총합은 같다)
                full = np.array(a["segmentation"][0]).reshape(-1, 2) * s
                cx = (full[:, 0].min() + full[:, 0].max()) / 2 + shift
                if a["id"] in prev_cx and prev_cx[a["id"]] > line >= cx:
                    expected["total"] += 1
                    expected["ng" if a["category"] == "deformed" else "ok"] += 1
                prev_cx[a["id"]] = cx

            if k and rng.random() < args.skip:  # C1 이 드롭한 프레임: seq 만 건너뛴다
                seq += 1
                continue
            b64 = None
            if img is not None:
                frame = cv2.warpAffine(img, np.float32([[1, 0, shift], [0, 1, 0]]), (w, h),
                                       borderMode=cv2.BORDER_CONSTANT, borderValue=VOID_BGR)
                ok, jpg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
                b64 = base64.b64encode(jpg.tobytes()).decode("ascii")
            reqs.append({
                "session": args.session, "seq": seq, "mode": "fast", "config": DEFAULT_CONFIG,
                "infer": {"session": args.session, "seq": seq, "mode": "fast", "backend": "sim-truth",
                          "replica": "sim2track", "w": w, "h": h, "infer_ms": 0.0, "objects": objs},
                "image_jpeg_b64": b64,
            })
            truth.append({"seq": seq, "source": lp.name, "objects": gts})
            seq += 1
    return reqs, {"session": args.session, "expected_counts": expected, "frames": truth}


def validate(reqs: list[dict]):
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from shared.schemas import TrackRequest
    except ImportError:
        print("(pydantic 없음: 스키마 검증 생략)")
        return
    for r in reqs:
        TrackRequest.model_validate(r)
    print(f"스키마 검증 통과: TrackRequest {len(reqs)}개")


def post(base: str, path: str, body: dict | None = None, method: str = "POST") -> dict:
    data = json.dumps(body).encode() if body is not None else b""
    base = base.replace("://localhost", "://127.0.0.1")  # Windows 에서 ::1 먼저 시도하느라 요청마다 ~2초 지연
    req = urllib.request.Request(base.rstrip("/") + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("labels", nargs="+", help="시뮬레이터 labels_*.json (여러 개면 seq 를 이어서 붙임)")
    ap.add_argument("--png", help="frame PNG (생략하면 labels 옆에서 찾음, labels 1개일 때만)")
    ap.add_argument("--out", help="TrackRequest JSON 저장 폴더")
    ap.add_argument("--post", metavar="C4_URL", help="예: http://localhost:8004 — 세션 삭제 후 순서대로 전송")
    ap.add_argument("--frames", type=int, default=1, help="labels 1개당 만들 프레임 수")
    ap.add_argument("--dx", type=float, default=13, help="프레임당 이동 px (출력 해상도 기준, 기본 130px/s @10fps)")
    ap.add_argument("--width", type=int, default=960, help="C2 출력 폭 흉내 (0 이면 원본 크기)")
    ap.add_argument("--session", default="simtest1")
    ap.add_argument("--start-seq", type=int, default=0)
    ap.add_argument("--defect-from", choices=["truth", "features"], default="truth",
                    help="truth: 정답 category / features: circ_th·solid_th 로 계산 (C3 흉내)")
    ap.add_argument("--jitter", type=float, default=0, help="객체 위치 흔들림 표준편차 px")
    ap.add_argument("--miss", type=float, default=0, help="객체를 검출하지 못할 확률 (0~1)")
    ap.add_argument("--skip", type=float, default=0, help="프레임이 드롭될 확률 (0~1, seq 는 건너뜀)")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--circ-th", type=float, default=0.80)
    ap.add_argument("--solid-th", type=float, default=0.95)
    args = ap.parse_args()
    if args.png and len(args.labels) > 1:
        ap.error("--png 는 labels 가 1개일 때만 쓸 수 있습니다")

    reqs, manifest = build(args)
    validate(reqs)
    print(f"정답 카운트 (검사선 통과): {manifest['expected_counts']}")

    if args.out:
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        for r in reqs:
            (out / f"req_{r['seq']:05d}.json").write_text(json.dumps(r), encoding="utf-8")
        (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"저장: {out} (req_*.json {len(reqs)}개, manifest.json)")

    if args.post:
        # reset 은 last_seq 를 유지하므로(C1 seq 는 계속 증가) 재생할 때는 세션을 통째로 지운다
        post(args.post, f"/sessions/{args.session}", method="DELETE")
        res = None
        for r in reqs:
            res = post(args.post, "/track", r)
            if res["events"] or res["stale"]:
                print(f"seq {res['seq']}: stale={res['stale']} events={res['events']} counts={res['counts']}")
        print(f"C4 최종 카운트: {res['counts']}  /  정답: {manifest['expected_counts']}")


if __name__ == "__main__":
    main()
