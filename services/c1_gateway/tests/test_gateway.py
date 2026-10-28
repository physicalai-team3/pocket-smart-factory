import json

import pytest
from starlette.websockets import WebSocketDisconnect

from app import main as c1
from app.pipeline import should_run_precise
from conftest import SAMPLES
from shared.schemas import pack_envelope, unpack_envelope

FRAME = (SAMPLES / "frame.jpg").read_bytes()


def envelope(i: int) -> bytes:
    return pack_envelope({"type": "frame", "client_seq": i, "ts_capture": 0, "w": 960, "h": 328}, FRAME)


@pytest.mark.parametrize("policy,needs,busy,disabled,now,last,expect", [
    ("uncertain", True, False, False, 1000, 0, True),
    ("uncertain", False, False, False, 1000, 0, False),   # 애매한 트랙 없음
    ("uncertain", True, True, False, 1000, 0, False),     # 이미 진행 중
    ("uncertain", True, False, True, 1000, 0, False),     # 409 로 비활성
    ("uncertain", True, False, False, 1000, 900, False),  # 간격 300ms 미달
    ("interval", False, False, False, 1000, 0, True),
    ("off", True, False, False, 1000, 0, False),
])
def test_should_run_precise(policy, needs, busy, disabled, now, last, expect):
    assert should_run_precise(policy, needs, busy, disabled, now, last, 300) is expect


@pytest.mark.parametrize("pts,ok", [
    (None, True),
    ([[0.1, 0.1], [0.9, 0.1], [0.9, 0.9], [0.1, 0.9]], True),
    ([[0.1, 0.1], [0.9, 0.1], [0.9, 0.9]], False),
    ([[0.1, 0.1], [0.9, 0.9], [0.9, 0.1], [0.1, 0.9]], False),
    ([[0.1, 0.1], [0.9, 0.1], [0.9, 0.9], [0.1, 2.0]], False),
])
def test_validate_roi(pts, ok):
    if ok:
        c1.validate_roi(pts)
    else:
        with pytest.raises(ValueError):
            c1.validate_roi(pts)


def test_sessions_and_qr(gateway):
    r = gateway.post("/api/sessions")
    assert r.status_code == 201
    info = r.json()
    assert len(info["session"]) == 8
    assert info["phone_url"] == f"https://test.trycloudflare.com/phone.html?s={info['session']}"
    assert gateway.get(f"/api/sessions/{info['session']}").json() == info
    assert gateway.get("/api/sessions/nope1234").status_code == 404
    qr = gateway.get(info["qr_url"])
    assert qr.status_code == 200 and qr.content[:8] == b"\x89PNG\r\n\x1a\n"
    assert gateway.get("/api/public-url").json() == {"public_url": "https://test.trycloudflare.com", "source": "env"}


def test_health(gateway):
    body = gateway.get("/api/health").json()
    assert body["deps"] == {"c2": "ok", "c3": "ok", "c4": "ok", "tunnel": "ok"}


def test_unknown_session_closes_4404(gateway):
    with gateway.websocket_connect("/ws/phone?s=nope1234") as ws:
        assert json.loads(ws.receive_text())["code"] == "SESSION_NOT_FOUND"
        with pytest.raises(WebSocketDisconnect) as e:
            ws.receive_text()
        assert e.value.code == 4404


def _until_bytes(ws, limit=50):
    for _ in range(limit):
        msg = ws.receive()
        if msg.get("bytes") is not None:
            return msg["bytes"]
    raise AssertionError("no result frame")


def test_frames_flow_to_dashboard(gateway):
    sid = gateway.post("/api/sessions").json()["session"]
    with gateway.websocket_connect(f"/ws/dashboard?s={sid}") as dash:
        kinds = [json.loads(dash.receive_text())["type"] for _ in range(3)]
        assert kinds == ["session", "config", "phone"]
        with gateway.websocket_connect(f"/ws/phone?s={sid}") as phone:
            assert json.loads(phone.receive_text())["type"] == "welcome"
            phone.send_text(json.dumps({"type": "hello", "device": {"ua": "pytest"}, "target_fps": 10}))
            seqs = []
            for i in range(3):
                phone.send_bytes(envelope(i))
                header, jpeg = unpack_envelope(_until_bytes(dash))
                assert header["type"] == "result" and jpeg[:2] == b"\xff\xd8"
                assert set(header["counts"]) == {"total", "ok", "ng"}
                assert set(header["latency_ms"]) == {"c2", "c3", "c4", "total"}
                seqs.append(header["seq"])
            assert seqs == [0, 1, 2]
            assert len(header["tracks"]) >= 5  # 스텁이 sample 프레임에서 과자를 검출


def test_backpressure_drops_instead_of_queueing(gateway, monkeypatch):
    monkeypatch.setattr(c1, "MAX_INFLIGHT", 0)
    sid = gateway.post("/api/sessions").json()["session"]
    with gateway.websocket_connect(f"/ws/phone?s={sid}") as phone:
        phone.receive_text()
        for i in range(5):
            phone.send_bytes(envelope(i))
        phone.send_text(json.dumps({"type": "ping"}))
        assert json.loads(phone.receive_text())["type"] == "pong"
    st = c1.sessions[sid].stats
    assert st.dropped == 5 and c1.sessions[sid].next_seq == 0


def test_corrupt_envelope_keeps_connection(gateway):
    sid = gateway.post("/api/sessions").json()["session"]
    with gateway.websocket_connect(f"/ws/phone?s={sid}") as phone:
        phone.receive_text()
        phone.send_bytes(b"\x00\x00\xff\xffgarbage")
        phone.send_bytes(b"\x01")
        phone.send_text(json.dumps({"type": "ping"}))
        assert json.loads(phone.receive_text())["type"] == "pong"
    assert c1.sessions[sid].stats.errors["envelope"] == 2


def test_second_phone_replaces_first(gateway):
    sid = gateway.post("/api/sessions").json()["session"]
    with gateway.websocket_connect(f"/ws/phone?s={sid}") as first:
        first.receive_text()
        with gateway.websocket_connect(f"/ws/phone?s={sid}") as second:
            assert json.loads(second.receive_text())["type"] == "welcome"
            assert json.loads(first.receive_text())["code"] == "PHONE_REPLACED"
            with pytest.raises(WebSocketDisconnect) as e:
                first.receive_text()
            assert e.value.code == 4409


def test_config_roi_reset(gateway):
    sid = gateway.post("/api/sessions").json()["session"]
    with gateway.websocket_connect(f"/ws/dashboard?s={sid}") as dash:
        for _ in range(3):
            dash.receive_text()
        dash.send_text(json.dumps({"type": "config", "config": {"line_x": 0.5, "precise_policy": "off"}}))
        cfg = json.loads(dash.receive_text())["config"]
        assert cfg["line_x"] == 0.5 and cfg["precise_policy"] == "off" and cfg["circ_th"] == 0.935
        dash.send_text(json.dumps({"type": "config", "config": {"line_x": 3}}))
        assert json.loads(dash.receive_text())["code"] == "BAD_CONFIG"
        dash.send_text(json.dumps({"type": "roi", "points": [[0.1, 0.1], [0.9, 0.1], [0.9, 0.9], [0.1, 0.9]]}))
        assert json.loads(dash.receive_text())["config"]["roi"][2] == [0.9, 0.9]
        dash.send_text(json.dumps({"type": "roi", "points": [[0, 0], [1, 1], [1, 0], [0, 1]]}))
        assert json.loads(dash.receive_text())["code"] == "BAD_ROI"
        dash.send_text(json.dumps({"type": "reset"}))
        dash.send_text(json.dumps({"type": "ping"}))
        assert json.loads(dash.receive_text())["type"] == "pong"
