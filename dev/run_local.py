"""Docker 없이 A 파트를 띄운다: C2(실제) + C3·C4 스텁 + C1(실제) + web/ 정적 파일.

nginx 대신 C1 이 web/ 을 직접 서빙하므로 http://localhost:8080/dev.html 에서 바로 확인할 수 있다.
C3 복제·로드밸런싱, cloudflared 터널은 없다 (그건 docker compose 로 확인).

사용:
  pip install -r services/c1_gateway/requirements.txt -r services/c2_preprocess/requirements.txt
  python dev/run_local.py            # → http://localhost:8080/dev.html
  python tools/replay.py --base http://localhost:8080 --src synthetic --duration 30
환경변수 STUB_* 는 그대로 스텁에 전달된다.
USE_REAL=1 이면 스텁 대신 실제 C3(services/c3_inference, SAM 2 없이)·C4(services/c4_visualizer)를 띄운다 — 통합 확인용.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PORT = int(os.getenv("PORT", "8080"))
USE_REAL = os.getenv("USE_REAL") == "1"
SERVICES = [  # (이름, 작업 폴더, 포트)
    ("c2", ROOT / "services" / "c2_preprocess", 8002),
    ("c3", ROOT / "services" / "c3_inference", 8003) if USE_REAL else ("c3-stub", ROOT / "dev" / "stubs" / "c3_stub", 8003),
    ("c4", ROOT / "services" / "c4_visualizer", 8004) if USE_REAL else ("c4-stub", ROOT / "dev" / "stubs" / "c4_stub", 8004),
]
# SEPARATE_PRECISE=1 이면 precise 전용 C3 스텁(:8013)을 하나 더 띄워 C3_PRECISE_URL 로 보낸다
SEPARATE_PRECISE = os.getenv("SEPARATE_PRECISE") == "1"
if SEPARATE_PRECISE:
    SERVICES.append(("c3-precise-stub", ROOT / "dev" / "stubs" / "c3_stub", 8013))


def wait_healthy(port: int, timeout: float = 30):
    t_end = time.time() + timeout
    while time.time() < t_end:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1)
            return
        except OSError:
            time.sleep(0.3)
    raise SystemExit(f"service on :{port} did not become healthy")


def main():
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(ROOT), os.environ.get("PYTHONPATH", "")])}
    procs = []
    try:
        for name, cwd, port in SERVICES:
            procs.append(subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(port),
                                           "--log-level", "warning"], cwd=cwd, env=env))
        for _, _, port in SERVICES:
            wait_healthy(port)
        os.environ.update({"C2_URL": "http://127.0.0.1:8002", "C3_URL": "http://127.0.0.1:8003",
                           "C4_URL": "http://127.0.0.1:8004", "CLOUDFLARED_METRICS": "http://127.0.0.1:1",
                           "PUBLIC_URL": os.getenv("PUBLIC_URL", f"http://localhost:{PORT}")})
        if SEPARATE_PRECISE:
            os.environ["C3_PRECISE_URL"] = "http://127.0.0.1:8013"
        sys.path[:0] = [str(ROOT / "services" / "c1_gateway"), str(ROOT)]
        import uvicorn
        from fastapi.staticfiles import StaticFiles
        from app.main import app  # C1

        app.mount("/", StaticFiles(directory=ROOT / "web", html=True), name="web")  # 라우트 뒤에 붙어 /api·/ws 는 그대로
        print(f"\n  dashboard  http://localhost:{PORT}/dev.html\n  simulator  http://localhost:{PORT}/sim/\n")
        uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")
    finally:
        for p in procs:
            p.terminate()


if __name__ == "__main__":
    main()
