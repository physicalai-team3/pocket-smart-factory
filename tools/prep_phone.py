"""폰으로 찍은 시뮬레이터 사진 → 캔버스 영역만 원근 보정 → eval_offline.py 입력 형식.

사용: python tools/prep_phone.py --src data/phone --out data/phone_warped
      python tools/eval_offline.py --data data/phone_warped --draw data/phone_marked

- 폴더마다(하위 폴더 포함) 사진(jpg/jpeg/png, heic 제외)과 labels_*.json 을 각각 이름순으로
  정렬해 1:1 로 짝짓는다. (사진 1장마다 시뮬레이터에서 L 을 한 번 → 개수가 다른 폴더는 건너뜀)
- 캔버스 위아래의 노란 경고 띠로 캔버스 네 모서리를 찾고, 정답 JSON 의 캔버스 크기로 편다.
  C2 의 ROI 4점 원근 보정과 같은 일.
- 찾은 사각형은 out/check_*.jpg 에 그려 두니 눈으로 확인할 것. 틀리면 --corners 로 직접 지정.
"""
import argparse
import json
import shutil
from pathlib import Path

import cv2
import numpy as np

PHOTO_EXT = {".jpg", ".jpeg", ".png"}


def order_quad(pts: np.ndarray) -> np.ndarray:
    """좌상 → 우상 → 우하 → 좌하."""
    pts = pts.reshape(4, 2).astype(np.float32)
    s, d = pts.sum(1), np.diff(pts, axis=1).ravel()
    return np.array([pts[s.argmin()], pts[d.argmin()], pts[s.argmax()], pts[d.argmax()]], np.float32)


def find_canvas(img: np.ndarray, sat: int) -> tuple[np.ndarray | None, np.ndarray]:
    """캔버스 위아래의 노란 경고 띠(#e8b21f) 두 개를 찾아, 그 사이를 캔버스로 본다.

    어두운 영역으로 찾으면 어두운 방·모니터 테두리와 이어져 버려서 쓰지 않는다.
    반환: (4점 또는 None, 디버그용 노란색 마스크)
    """
    h, w = img.shape[:2]
    hsv = cv2.cvtColor(cv2.GaussianBlur(img, (5, 5), 0), cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, np.array([17, sat, 120], np.uint8), np.array([32, 255, 255], np.uint8))
    # 띠는 노랑/검정 사선 줄무늬 → 가로로 이어 붙인다
    k = max(9, w // 60)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (k, 3)))
    cs, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    bands = []
    for c in cs:
        (_, _), (rw, rh), _ = rect = cv2.minAreaRect(c)
        if max(rw, rh) > 0.25 * w and max(rw, rh) > 8 * min(rw, rh):  # 길고 가는 것만
            bands.append((max(rw, rh), cv2.boxPoints(rect)))
    if len(bands) < 2:
        return None, mask
    top, bot = sorted((b for _, b in sorted(bands, key=lambda b: -b[0])[:2]), key=lambda b: b[:, 1].mean())

    # 화면을 비스듬히 보면 띠 한쪽 끝이 어두워져 위 기준에서 잘린다. 기준을 그냥 낮추면 띠가 과자 등과
    # 붙어 버리므로, 찾은 띠의 직선 위(띠 두께 안)에서만 완화된 기준으로 띠를 끝까지 따라간다.
    loose = cv2.inRange(hsv, np.array([15, 60, 25], np.uint8), np.array([32, 255, 255], np.uint8))
    ys, xs = np.nonzero(loose)
    pts = np.stack([xs, ys], 1).astype(np.float32)
    tl, tr = _band_inner_edge(top, bot.mean(0), pts)
    bl, br = _band_inner_edge(bot, top.mean(0), pts)
    return np.array([tl, tr, br, bl], np.float32), mask


def _band_inner_edge(box: np.ndarray, toward: np.ndarray, pts: np.ndarray):
    """띠(minAreaRect 4점)의 캔버스 쪽 변의 왼쪽·오른쪽 끝."""
    c = box.mean(0)
    e1, e2 = box[1] - box[0], box[2] - box[1]
    d, t = (e1, np.linalg.norm(e2)) if np.linalg.norm(e1) > np.linalg.norm(e2) else (e2, np.linalg.norm(e1))
    d = d / np.linalg.norm(d)
    if d[0] < 0:
        d = -d  # 왼쪽 → 오른쪽
    n = np.array([-d[1], d[0]])
    if np.dot(toward - c, n) < 0:
        n = -n  # 캔버스 쪽
    rel = pts - c
    s, u = rel @ n, rel @ d
    u = np.sort(u[np.abs(s) <= 0.75 * t])  # 띠 안쪽 글자(LINE 01)는 띠 바깥 0.9t 근처라 제외됨
    half = np.linalg.norm(box[0] - box[2]) / 2  # 엄격 기준으로 찾은 길이의 절반쯤
    lo, hi = -half * 0.5, half * 0.5
    # 띠의 검은 사선 줄무늬(띠 길이의 ~0.3%)는 건너뛰고, 모니터 테두리(~2.5% 이상)에서는 멈춘다
    gap = 0.012 * 2 * half
    for v in u[u > hi]:
        if v - hi > gap:
            break
        hi = v
    for v in u[u < lo][::-1]:
        if lo - v > gap:
            break
        lo = v
    return c + lo * d + t / 2 * n, c + hi * d + t / 2 * n


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--src", default="data/phone", help="사진 + labels_*.json 폴더")
    ap.add_argument("--out", default="data/phone_warped")
    ap.add_argument("--sat", type=int, default=120, help="노란 띠로 볼 채도 하한 (0~255). 못 찾으면 낮춰 보기")
    ap.add_argument("--corners", help='사진별 4점 직접 지정 JSON: {"IMG_1.jpg": [[x,y],...4개]} (원본 사진 좌표)')
    ap.add_argument("--stream-width", type=int, default=0,
                    help="폰 송출 재현: 사진 전체를 긴 변 N px + JPEG q=70 으로 만든 뒤 처리 (phone.html 은 960)")
    args = ap.parse_args()

    src, out = Path(args.src), Path(args.out)
    manual = json.loads(Path(args.corners).read_text(encoding="utf-8")) if args.corners else {}

    # src 와 그 하위 폴더(정면/비스듬히/…)를 각각 한 묶음으로. 출력도 같은 폴더 구조.
    total = failed = 0
    for folder in sorted({p.parent for p in src.rglob("*") if p.suffix.lower() in PHOTO_EXT}):
        photos = sorted(p for p in folder.iterdir() if p.suffix.lower() in PHOTO_EXT)
        labels = sorted(folder.glob("labels_*.json"))
        if len(photos) != len(labels):
            print(f"{folder}: 사진 {len(photos)}장, 라벨 {len(labels)}개 — 개수가 같아야 순서대로 짝지을 수 있어 건너뜀")
            failed += len(photos)
            total += len(photos)
            continue
        fout = out / folder.relative_to(src)
        fout.mkdir(parents=True, exist_ok=True)
        for i, (p, l) in enumerate(zip(photos, labels)):
            total += 1
            failed += not prep_one(p, l, fout, i, manual, args.sat, args.stream_width)

    print(f"\n{total - failed}/{total} 완료. {out} 아래 check_*.jpg 에서 초록 사각형이 캔버스와 맞는지 확인하세요.")


def imread(p: Path):
    """cv2.imread 는 Windows 에서 한글 경로를 못 읽는다."""
    return cv2.imdecode(np.fromfile(str(p), np.uint8), cv2.IMREAD_COLOR)


def imwrite(p: Path, img: np.ndarray) -> None:
    cv2.imencode(p.suffix, img)[1].tofile(str(p))


def prep_one(p: Path, l: Path, out: Path, i: int, manual: dict, sat: int, stream_width: int) -> bool:
    img = imread(p)
    if img is None:
        print(f"{p.name}: 읽을 수 없음 (HEIC 이면 JPG 로 바꿔 주세요)")
        return False
    scale = 1.0
    if stream_width:
        scale = min(1.0, stream_width / max(img.shape[:2]))
        img = cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
        img = cv2.imdecode(cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 70])[1], cv2.IMREAD_COLOR)
    info = json.loads(l.read_text(encoding="utf-8"))["image"]
    cw, ch = info["width"], info["height"]
    if p.name in manual:
        quad, ymask = order_quad(np.float32(manual[p.name]) * scale), None
    else:
        quad, ymask = find_canvas(img, sat)

    check = img.copy()
    ok = quad is not None
    if not ok:
        print(f"{p.name}: 노란 띠 2개를 찾지 못함 → --sat 를 낮추거나 --corners 로 지정 (yellow_{i:03d}.png 참고)")
        imwrite(out / f"yellow_{i:03d}.png", ymask)
    else:
        cv2.polylines(check, [quad.astype(np.int32)], True, (0, 255, 0), max(2, img.shape[1] // 300))
        # 고해상도 사진을 한 번에 펴면 픽셀을 건너뛰어 줄무늬가 생긴다 → 캔버스 크기 근처로 먼저 줄인다
        s = min(1.0, cw / np.linalg.norm(quad[1] - quad[0]))
        if s < 1:
            img, quad = cv2.resize(img, None, fx=s, fy=s, interpolation=cv2.INTER_AREA), quad * s
        M = cv2.getPerspectiveTransform(quad, np.float32([[0, 0], [cw, 0], [cw, ch], [0, ch]]))
        imwrite(out / f"frame_{i:03d}.png", cv2.warpPerspective(img, M, (cw, ch), flags=cv2.INTER_LINEAR))
        shutil.copy(l, out / f"labels_{i:03d}.json")
        print(f"{p.parent.name}/{p.name} + {l.name} → frame_{i:03d}.png")
    s = 1280 / max(check.shape[:2])
    imwrite(out / f"check_{i:03d}.jpg", cv2.resize(check, None, fx=s, fy=s))
    return ok


if __name__ == "__main__":
    main()
