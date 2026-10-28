"""폰 없이 프레임을 /ws/phone 으로 송출 (봉투 형식, docs/03-messages.md §3.1).

사용:
  # 새 세션을 만들고 합성 프레임 30초 송출 (대시보드는 /dev.html?s=<출력된 세션>)
  python tools/replay.py --base http://localhost:8080 --src synthetic --duration 30
  # 기존 세션에 녹화 영상 반복 송출
  python tools/replay.py --base http://localhost:8080 --session k3x9q2ab --src data/recordings/phone_default.mp4 --loop
  # 원근이 들어간 합성 카메라 프레임 + 프레임/정답 저장 (C 에게 줄 평가 데이터)
  python tools/replay.py --src synthetic --camera --duration 20 --dump data/synth --dry-run

--src: synthetic | 이미지 폴더(png/jpg 정렬 순) | 동영상 파일
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Iterator, Optional

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dev.synth import Scene, camera_view, encode_jpeg, resize_long  # noqa: E402
from shared.schemas import pack_envelope  # noqa: E402


def frames(args) -> Iterator[tuple[np.ndarray, Optional[dict]]]:
    """(BGR 프레임, 정답 또는 None) 을 무한히 혹은 소스 끝까지 낸다."""
    if args.src == "synthetic":
        scene = Scene(seed=args.seed)
        for _ in range(int(8 * args.fps)):  # 벨트가 과자로 찰 때까지 미리 돌린다
            scene.step(1 / args.fps)
        while True:
            scene.step(1 / args.fps)
            canvas, anns = scene.render(draw_scan=args.scan_line)
            labels = scene.labels(anns)
            if args.camera:
                canvas, corners = camera_view(canvas, seed=args.seed)
                labels["roi"] = corners
            yield canvas, labels
    src = Path(args.src)
    while True:
        if src.is_dir():
            files = sorted(p for p in src.iterdir() if p.suffix.lower() in (".png", ".jpg", ".jpeg"))
            if not files:
                raise SystemExit(f"no images in {src}")
            for p in files:
                img = cv2.imread(str(p))
                if img is not None:
                    yield img, None
        else:
            cap = cv2.VideoCapture(str(src))
            if not cap.isOpened():
                raise SystemExit(f"cannot open {src}")
            while True:
                ok, img = cap.read()
                if not ok:  # 영상 끝
                    break
                yield img, None
            cap.release()
        if not args.loop:
            return


def ws_url(args) -> str:
    if args.url:
        return args.url
    import httpx
    base = args.base.rstrip("/")
    sid = args.session or httpx.post(f"{base}/api/sessions", timeout=5).json()["session"]
    print(f"session {sid}   dashboard: {base}/dev.html?s={sid}")
    return base.replace("http", "ws", 1) + f"/ws/phone?s={sid}"


async def run(args):
    from websockets.asyncio.client import connect
    from websockets.exceptions import ConnectionClosed

    url = ws_url(args)
    fps = args.fps
    sent = skipped = 0
    last_stats: dict = {}
    dump = Path(args.dump) if args.dump else None
    if dump:
        dump.mkdir(parents=True, exist_ok=True)

    async with connect(url, max_size=None) as ws:
        welcome = json.loads(await ws.recv())
        if welcome.get("type") == "error":
            raise SystemExit(f"server: {welcome}")
        src = frames(args)
        sample =resize_long(next(src)[0], args.width)
        await ws.send(json.dumps({"type": "hello", "device": {"ua": "tools/replay.py", "w": sample.shape[1],
                                                             "h": sample.shape[0]},
                                  "send_w": sample.shape[1], "send_h": sample.shape[0], "target_fps": fps}))

        async def reader():
            try:
                await _read(ws)
            except ConnectionClosed:
                pass

        async def _read(ws):
            nonlocal fps, last_stats
            async for msg in ws:
                if isinstance(msg, bytes):
                    continue
                m = json.loads(msg)
                if m.get("type") == "stats":
                    last_stats = m
                    print(f"\rsent {sent:5d}  fps_in {m['fps_in']:4.1f}  fps_out {m['fps_out']:4.1f}  "
                          f"dropped {m['dropped']:4d}  latency {m.get('latency_ms')} ms   ", end="", flush=True)
                elif m.get("type") == "throttle" and not args.ignore_throttle:
                    fps = max(1, int(m["target_fps"]))
                    print(f"\nthrottle → {fps} fps")
                elif m.get("type") == "error":
                    print(f"\nserver error: {m}")

        rd = asyncio.create_task(reader())
        t_end = time.monotonic() + args.duration if args.duration else None
        next_t = time.monotonic()
        try:
            for i, (img, labels) in enumerate(src):
                if t_end and time.monotonic() >= t_end:
                    break
                img = resize_long(img, args.width)
                jpeg = encode_jpeg(img, args.quality)
                if dump:
                    (dump / f"frame_{i:05d}.jpg").write_bytes(jpeg)
                    if labels:
                        (dump / f"labels_{i:05d}.json").write_text(json.dumps(labels))
                header = {"type": "frame", "client_seq": i, "ts_capture": time.time() * 1000,
                          "w": img.shape[1], "h": img.shape[0]}
                # 폰의 bufferedAmount 검사에 해당: 보내기가 한 프레임 간격보다 오래 걸리면 다음 프레임은 건너뛴다
                if time.monotonic() > next_t + 1 / fps:
                    skipped += 1
                else:
                    await ws.send(pack_envelope(header, jpeg))
                    sent += 1
                next_t += 1 / fps
                await asyncio.sleep(max(0.0, next_t - time.monotonic()))
            await asyncio.sleep(1.5)  # 마지막 stats 수신
        except ConnectionClosed as e:
            code = e.rcvd.code if e.rcvd else None
            reason = {4404: "세션 없음", 4409: "다른 폰(송출기)이 같은 세션에 연결됨"}.get(code, "연결 끊김")
            print(f"\nserver closed the connection: {code} {reason}")
        finally:
            rd.cancel()
    print(f"\ndone: sent {sent}, skipped {skipped}, last stats: {json.dumps(last_stats)}")


def dry_run(args):
    """서버 없이 프레임과 정답만 저장."""
    dump = Path(args.dump)
    dump.mkdir(parents=True, exist_ok=True)
    n = int(args.duration * args.fps) if args.duration else 100
    for i, (img, labels) in zip(range(n), frames(args)):
        (dump / f"frame_{i:05d}.jpg").write_bytes(encode_jpeg(resize_long(img, args.width), args.quality))
        if labels:
            (dump / f"labels_{i:05d}.json").write_text(json.dumps(labels))
    print(f"wrote {n} frames to {dump}")


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--base", default="http://localhost:8080", help="nginx 주소 (세션 생성·WS 주소 계산)")
    p.add_argument("--session", help="기존 세션 ID (없으면 새로 만든다)")
    p.add_argument("--url", help="ws://.../ws/phone?s=... 를 직접 지정")
    p.add_argument("--src", default="synthetic")
    p.add_argument("--fps", type=float, default=10)
    p.add_argument("--width", type=int, default=960, help="긴 변 픽셀")
    p.add_argument("--quality", type=int, default=70, help="JPEG 품질 0~100")
    p.add_argument("--duration", type=float, default=0, help="초. 0 = 소스 끝까지")
    p.add_argument("--loop", action="store_true", help="폴더/영상을 반복")
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--camera", action="store_true", help="합성 프레임에 원근·노이즈를 넣어 폰 촬영 흉내")
    p.add_argument("--scan-line", action="store_true", help="합성 프레임에 빨간 검사선 점선 그리기")
    p.add_argument("--dump", help="보낸 프레임(+정답)을 저장할 폴더")
    p.add_argument("--dry-run", action="store_true", help="서버에 보내지 않고 --dump 에 저장만")
    p.add_argument("--ignore-throttle", action="store_true", help="서버 throttle 을 무시 (부하 테스트용)")
    args = p.parse_args(argv)
    if args.dry_run:
        if not args.dump:
            p.error("--dry-run needs --dump")
        return dry_run(args)
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
