"""C1 테스트: C2(실제 코드)와 C3·C4 스텁을 ASGITransport 로 프로세스 안에서 연결한다 — 네트워크·Docker 불필요."""
import importlib.util
import os
import sys
from pathlib import Path

import httpx
import pytest

SERVICE = Path(__file__).resolve().parents[1]
ROOT = SERVICE.parents[1]
sys.path[:0] = [str(SERVICE), str(ROOT)]
os.environ.setdefault("PUBLIC_URL", "https://test.trycloudflare.com")
SAMPLES = ROOT / "dev" / "samples"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod  # dataclass 가 모듈을 sys.modules 에서 찾는다
    spec.loader.exec_module(mod)
    return mod


c2_main = _load("c2_main", ROOT / "services" / "c2_preprocess" / "app" / "main.py")
c3_stub = _load("c3_stub", ROOT / "dev" / "stubs" / "c3_stub" / "app" / "main.py")
c4_stub = _load("c4_stub", ROOT / "dev" / "stubs" / "c4_stub" / "app" / "main.py")

from app import main as c1  # noqa: E402
from app.pipeline import Pipeline  # noqa: E402


def routed_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(mounts={
        "http://c2-preprocess:8002": httpx.ASGITransport(app=c2_main.app),
        "http://web:8090": httpx.ASGITransport(app=c3_stub.app),
        "http://c4-visualizer:8004": httpx.ASGITransport(app=c4_stub.app),
    })


@pytest.fixture
def gateway():
    """lifespan 을 돌린 C1 TestClient. 내부 httpx 클라이언트만 스텁으로 향하게 바꾼다."""
    from fastapi.testclient import TestClient
    c1.sessions.clear()
    c4_stub.SESSIONS.clear()
    with TestClient(c1.app) as tc:
        client = routed_client()
        c1._ctx["client"] = client
        c1._ctx["pipeline"] = Pipeline(client, c1.C2_URL, c1.C3_URL, c1.C4_URL, 3.0, 10.0)
        yield tc
