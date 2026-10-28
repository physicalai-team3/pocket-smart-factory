"""시뮬레이터 frame_*.png + labels_*.json → C3 결과와 비교, 임계값 격자 탐색.

사용: python tools/eval_offline.py --data data/sim --c3 http://localhost:8003
명세: docs/05-detection.md §6.1

- 이미지를 C2와 같은 조건(긴 변 960px, JPEG q=85)으로 만들어 C3 /infer 에 한 번씩 보낸다.
- 정답 bbox와 C3 객체를 IoU ≥ 0.5 로 매칭한다. 프레임 경계에 걸친 정답은 채점하지 않는다.
- 특징값(원형도·볼록도)은 임계값과 무관하므로, 격자 탐색은 받아 둔 특징값으로 로컬에서 계산한다.
"""
import argparse
import csv
import json
import re
import sys
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np

EDGE_PX = 2  # services/c3_inference/app/features.py 와 같은 값
STAMP = re.compile(r"(\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2})")
GROUP_PARAMS = ("strength", "light", "noise", "size")  # 조건별 정확도를 나눠 볼 슬라이더


def pair_files(root: Path) -> list[tuple[Path, Path]]:
    """같은 폴더에서 frame_X ↔ labels_X. 이름이 다르면 저장 시각이 가장 가까운 것(30초 이내)."""
    pairs = []
    for folder in sorted({p.parent for p in root.rglob("frame_*.png")}):
        frames = sorted(folder.glob("frame_*.png"))
        labels = {p.stem[len("labels_"):]: p for p in folder.glob("labels_*.json")}
        for f in frames:
            key = f.stem[len("frame_"):]
            if key in labels:
                pairs.append((f, labels.pop(key)))
                continue
            m = STAMP.search(key)
            if not m:
                print(f"skip (no labels): {f}", file=sys.stderr)
                continue
            t = datetime.strptime(m.group(1), "%Y-%m-%dT%H-%M-%S")
            best, best_dt = None, 30.0
            for k in labels:
                mk = STAMP.search(k)
                if mk:
                    dt = abs((datetime.strptime(mk.group(1), "%Y-%m-%dT%H-%M-%S") - t).total_seconds())
                    if dt <= best_dt:
                        best, best_dt = k, dt
            if best is None:
                print(f"skip (no labels): {f}", file=sys.stderr)
            else:
                pairs.append((f, labels.pop(best)))
    return pairs


def to_c2_jpeg(img: np.ndarray, width: int, quality: int) -> tuple[bytes, float]:
    s = width / max(img.shape[:2])
    if s != 1:
        img = cv2.resize(img, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, quality])
    assert ok
    return buf.tobytes(), s


def call_c3(url: str, jpeg: bytes, mode: str, seq: int, circ_th: float, solid_th: float) -> dict:
    req = urllib.request.Request(f"{url.rstrip('/')}/infer", data=jpeg, method="POST", headers={
        "Content-Type": "image/jpeg", "X-Mode": mode, "X-Session": "eval", "X-Seq": str(seq),
        "X-Circ-Th": str(circ_th), "X-Solid-Th": str(solid_th)})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        sys.exit(f"C3 {e.code}: {e.read().decode(errors='replace')}")


def iou(a, b) -> float:
    ax1, ay1, ax2, ay2 = a[0], a[1], a[0] + a[2], a[1] + a[3]
    bx1, by1, bx2, by2 = b[0], b[1], b[0] + b[2], b[1] + b[3]
    iw = max(0.0, min(ax2, bx2) - max(ax1, bx1))
    ih = max(0.0, min(ay2, by2) - max(ay1, by1))
    inter = iw * ih
    union = a[2] * a[3] + b[2] * b[3] - inter
    return inter / union if union > 0 else 0.0


def match(gts: list[dict], preds: list[dict], th: float) -> list[tuple[int, int]]:
    """IoU 큰 순서로 greedy 1:1 매칭."""
    cand = sorted(((iou(g["bbox"], p["bbox"]), gi, pi)
                   for gi, g in enumerate(gts) for pi, p in enumerate(preds)), reverse=True)
    used_g, used_p, out = set(), set(), []
    for v, gi, pi in cand:
        if v < th:
            break
        if gi in used_g or pi in used_p:
            continue
        used_g.add(gi)
        used_p.add(pi)
        out.append((gi, pi))
    return out


def scores(rows: list[dict], circ_th: float, solid_th: float) -> dict:
    tp = fp = tn = fn = 0
    for r in rows:
        pred = r["circularity"] < circ_th or r["solidity"] < solid_th
        if r["gt_defect"]:
            tp += pred
            fn += not pred
        else:
            fp += pred
            tn += not pred
    n = tp + fp + tn + fn
    prec = tp / (tp + fp) if tp + fp else 0.0
    rec = tp / (tp + fn) if tp + fn else 0.0
    return {"n": n, "tp": tp, "fp": fp, "tn": tn, "fn": fn,
            "accuracy": (tp + tn) / n if n else 0.0, "precision": prec, "recall": rec,
            "f1": 2 * prec * rec / (prec + rec) if prec + rec else 0.0}


def pearson(x, y) -> float:
    if len(x) < 2 or np.std(x) == 0 or np.std(y) == 0:
        return float("nan")
    return float(np.corrcoef(x, y)[0, 1])


def draw(img: np.ndarray, objects: list[dict], path: Path) -> None:
    """C3 결과를 이미지에 그린다. 판정은 C3 가 받은 임계값 기준."""
    for o in objects:
        color = (0, 220, 255) if o["partial"] else (0, 0, 255) if o["defect"] else (0, 200, 0)
        cv2.polylines(img, [np.int32(o["contour"])], True, color, 2)
        x, y = o["bbox"][:2]
        cv2.putText(img, f"{o['circularity']:.2f}/{o['solidity']:.2f}", (x, max(10, y - 4)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.38, color, 1, cv2.LINE_AA)
    cv2.imencode(".jpg", img)[1].tofile(str(path))  # 한글 경로 대응


def fmt(s: dict) -> str:
    return (f"acc {s['accuracy']:.3f}  prec {s['precision']:.3f}  rec {s['recall']:.3f}  "
            f"f1 {s['f1']:.3f}  (n={s['n']}, TP {s['tp']} FP {s['fp']} TN {s['tn']} FN {s['fn']})")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", default="data/sim", help="frame_*.png / labels_*.json 폴더 (하위 폴더 포함)")
    ap.add_argument("--c3", default="http://localhost:8003")
    ap.add_argument("--mode", default="fast", choices=["fast", "precise"])
    ap.add_argument("--width", type=int, default=960, help="C2 TARGET_WIDTH")
    ap.add_argument("--quality", type=int, default=85, help="C2 JPEG 품질")
    ap.add_argument("--iou", type=float, default=0.5)
    ap.add_argument("--circ-th", type=float, default=0.935, help="현재 임계값 (비교 기준)")
    ap.add_argument("--solid-th", type=float, default=0.95)
    ap.add_argument("--csv", help="객체별 특징값을 CSV로 저장 (그래프용)")
    ap.add_argument("--draw", help="판정 결과를 그린 이미지를 저장할 폴더 (정상 초록 / 불량 빨강 / partial 노랑)")
    args = ap.parse_args()
    if args.draw:
        Path(args.draw).mkdir(parents=True, exist_ok=True)

    pairs = pair_files(Path(args.data))
    if not pairs:
        sys.exit(f"no frame_*.png + labels_*.json pairs under {args.data}")

    rows = []  # 매칭된 (정답 비partial, 예측 비partial) 객체
    n_gt = n_det = n_fp = n_edge = 0
    missed = defaultdict(int)  # 유형별 놓친 정답
    infer_ms = []
    for seq, (fpath, lpath) in enumerate(pairs):
        img = cv2.imdecode(np.fromfile(str(fpath), np.uint8), cv2.IMREAD_COLOR)  # 한글 경로 대응
        label = json.loads(lpath.read_text(encoding="utf-8"))
        jpeg, s = to_c2_jpeg(img, args.width, args.quality)
        res = call_c3(args.c3, jpeg, args.mode, seq, args.circ_th, args.solid_th)
        infer_ms.append(res["infer_ms"])
        w, h = res["w"], res["h"]
        if args.draw:
            draw(cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR), res["objects"],
                 Path(args.draw) / f"{'_'.join(fpath.relative_to(args.data).with_suffix('').parts)}_{args.mode}.jpg")

        # 정답 bbox 를 C3 입력 좌표로. 경계에 걸치면 C3 도 partial 로 내므로 채점에서 뺀다.
        gts, edge_boxes = [], []
        for a in label["annotations"]:
            x, y, bw, bh = (v * s for v in a["bbox"])
            if x + bw <= 0 or x >= w or y + bh <= 0 or y >= h:
                continue
            if x <= EDGE_PX or y <= EDGE_PX or x + bw >= w - EDGE_PX or y + bh >= h - EDGE_PX:
                edge_boxes.append([x, y, bw, bh])
                continue
            gts.append({**a, "bbox": [x, y, bw, bh]})
        preds = [o for o in res["objects"] if not o["partial"]]
        partials = [o for o in res["objects"] if o["partial"]]

        pairs_m = match(gts, preds, args.iou)
        matched_g = {gi for gi, _ in pairs_m}
        for gi, g in enumerate(gts):
            if gi in matched_g:
                continue
            # 경계 바로 옆 과자: C3 외곽선이 정답보다 조금 커서 partial 로 나온 경우 → 놓침이 아님
            if any(iou(g["bbox"], o["bbox"]) >= args.iou for o in partials):
                n_edge += 1
            else:
                n_gt += 1
                missed[g.get("defect_type") or "round"] += 1
        n_gt += len(pairs_m)
        n_det += len(pairs_m)
        # 매칭 안 된 예측 중 경계 정답과도 겹치지 않는 것만 오검출로 센다
        matched_p = {pi for _, pi in pairs_m}
        for pi, p in enumerate(preds):
            if pi in matched_p:
                continue
            if any(iou(p["bbox"], e) > 0.1 for e in edge_boxes):
                n_edge += 1
            else:
                n_fp += 1
        params = label.get("params", {})
        for gi, pi in pairs_m:
            g, p = gts[gi], preds[pi]
            rows.append({
                "frame": str(fpath.relative_to(args.data)),
                "gt_defect": g["category"] != "round",
                "defect_type": g.get("defect_type") or "round",
                "roundness": g.get("roundness"),
                "circularity": p["circularity"],
                "solidity": p["solidity"],
                "iou": round(iou(g["bbox"], p["bbox"]), 3),
                **{k: params.get(k) for k in GROUP_PARAMS},
            })

    print(f"frames {len(pairs)}  |  C3 {args.mode} infer_ms median {np.median(infer_ms):.1f}"
          f"  p95 {np.percentile(infer_ms, 95):.1f}")
    print(f"detection: {n_det}/{n_gt} matched (recall {n_det / max(1, n_gt):.3f}),"
          f" false detections {n_fp}, near-edge disagreements {n_edge}")
    if missed:
        print("missed by type: " + ", ".join(f"{k} {v}" for k, v in sorted(missed.items())))
    if not rows:
        sys.exit("no matched objects")

    # precise 는 C3 가 원형도 기준에 PRECISE_CIRC_OFFSET 을 더해 판정한다 (/health 에서 읽음)
    circ_cur = args.circ_th
    if args.mode == "precise":
        with urllib.request.urlopen(f"{args.c3.rstrip('/')}/health", timeout=10) as r:
            circ_cur += json.load(r).get("precise_circ_offset", 0.0)
    cur = scores(rows, circ_cur, args.solid_th)
    print(f"\ncurrent  CIRC_TH={circ_cur:.3f} SOLID_TH={args.solid_th:.3f}  {fmt(cur)}")

    # 격자 탐색: 정확도 최대 → 동률이면 f1 → 동률이면 두 임계값이 덜 극단적인 쪽(중앙값)
    grid = []
    for c in np.arange(0.70, 0.99, 0.005):
        for so in np.arange(0.85, 0.999, 0.002):
            grid.append((round(float(c), 3), round(float(so), 3), scores(rows, c, so)))
    top = max(sc["accuracy"] for _, _, sc in grid)
    best = [g for g in grid if g[2]["accuracy"] == top]
    top_f1 = max(g[2]["f1"] for g in best)
    best = [g for g in best if g[2]["f1"] == top_f1]
    bc, bs, bsc = best[len(best) // 2]
    cs = sorted({g[0] for g in best})
    ss = sorted({g[1] for g in best})
    print(f"best     CIRC_TH={bc:.3f} SOLID_TH={bs:.3f}  {fmt(bsc)}")
    print(f"         (같은 점수 범위: CIRC_TH {cs[0]:.3f}~{cs[-1]:.3f}, SOLID_TH {ss[0]:.3f}~{ss[-1]:.3f})")

    def breakdown(title, key):
        groups = defaultdict(list)
        for r in rows:
            groups[r[key]].append(r)
        print(f"\nby {title} (best thresholds)")
        for k in sorted(groups, key=lambda v: (v is None, v)):
            g = groups[k]
            sc = scores(g, bc, bs)
            print(f"  {str(k):>8}: acc {sc['accuracy']:.3f}  n={sc['n']}")

    # 결손 유형별: 불량은 재현율(잡은 비율), 정상은 정상으로 둔 비율
    breakdown("defect_type", "defect_type")
    for k in GROUP_PARAMS:
        if len({r[k] for r in rows}) > 1:
            breakdown(k, k)

    rd = [r for r in rows if r["roundness"] is not None]
    print("\ncorrelation with labels.roundness:"
          f" circularity {pearson([r['roundness'] for r in rd], [r['circularity'] for r in rd]):.3f},"
          f" solidity {pearson([r['roundness'] for r in rd], [r['solidity'] for r in rd]):.3f}")

    print("\nfeature range (min / median / max)")
    for t in ("round", "bite", "cut", "wedge", "half"):
        g = [r for r in rows if r["defect_type"] == t]
        if g:
            c = [r["circularity"] for r in g]
            so = [r["solidity"] for r in g]
            print(f"  {t:>6} n={len(g):3d}  circ {min(c):.3f} / {np.median(c):.3f} / {max(c):.3f}"
                  f"   solid {min(so):.3f} / {np.median(so):.3f} / {max(so):.3f}")

    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8") as f:
            wr = csv.DictWriter(f, fieldnames=list(rows[0]))
            wr.writeheader()
            wr.writerows(rows)
        print(f"\nwrote {len(rows)} rows → {args.csv}")


if __name__ == "__main__":
    main()
