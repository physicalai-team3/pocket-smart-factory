# Pocket Smart Factory

스마트폰 카메라로 컨베이어 시뮬레이터를 촬영하면, Docker 마이크로서비스(C1~C4)가
과자를 분할·판정해서 대시보드에 실시간 영상과 정상/불량 개수를 보여줍니다.
GPU 없이 CPU만으로 동작하도록 설계했습니다 (OpenCV fast + SAM 2 precise 하이브리드).

```
폰 ──WSS──▶ cloudflared ──▶ nginx ──▶ C1 ──▶ C2 ──▶ C3×N ──▶ C4 ──▶ C1 ──WSS──▶ 대시보드
```

## 빠른 시작

```bash
cp .env.example .env
docker compose up -d --build
docker compose logs cloudflared | grep trycloudflare   # 공개 URL
```
- 대시보드: http://localhost:8080 → QR을 폰으로 스캔
- 시뮬레이터: http://localhost:8080/sim/

## C3·C4 없이 개발하기 (스텁 + 더미 데이터)

C3·C4 자리에 계약(`shared/schemas.py`)을 지키는 스텁을 끼워 A 파트(C1·C2·폰)를 먼저 개발·검증합니다.

```bash
# Docker: 스텁 오버라이드로 기동 → http://localhost:8080/dev.html
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d --build

# Docker 없이: C2 + 스텁 + C1 을 로컬 프로세스로 (터널·C3 복제 없음)
pip install -r requirements-dev.txt
python dev/run_local.py

# 폰 대신 합성 프레임 송출 (--camera: 원근·노이즈, --src 로 영상/폴더도 가능)
python tools/replay.py --base http://localhost:8080 --src synthetic --duration 30

# B·C 서비스가 계약을 지키는지 검사
python tools/contract_check.py --c3 http://localhost:8003 --c4 http://localhost:8004

# 테스트 (서비스마다 app 패키지 이름이 같아 따로 실행)
python -m pytest tests && (cd services/c2_preprocess && python -m pytest tests) && (cd services/c1_gateway && python -m pytest tests)
```

| 경로 | 내용 |
|---|---|
| `dev/samples/` | 계약 예제 메시지·프레임 (`python dev/make_samples.py` 로 재생성) |
| `dev/synth.py` | 시뮬레이터와 같은 파라미터의 합성 프레임 + 정답 생성기 |
| `dev/stubs/` | C3·C4 스텁. `STUB_LATENCY_MS`, `STUB_FAIL_RATE`, `STUB_NEEDS_PRECISE_EVERY` 등으로 장애 주입 |
| `web/dev.html` | A 파트용 최소 대시보드 (세션·QR·결과 영상·설정·ROI·stats) |

## 문서

설계 문서는 [docs/](docs/README.md)에 있습니다. 작업 전에 **[03-messages.md](docs/03-messages.md)** (메시지 계약)를 먼저 읽어주세요.

| 디렉터리 | 내용 | 담당 |
|---|---|---|
| `docker-compose.yml`, `docker-compose.dev.yml`, `nginx/`, `services/c1_gateway/`, `services/c2_preprocess/`, `web/phone.html`, `web/dev.html`, `tools/replay.py`, `tools/contract_check.py`, `dev/` | 인프라, 스트리밍, 전처리, 개발용 스텁 | A |
| `web/index.html`, `services/c4_visualizer/` | 대시보드, 추적, 카운트, 마킹 | B |
| `services/c3_inference/`, `tools/eval_offline.py`, `tools/download_models.sh` | 분할, 판정, 평가 | C |
| `shared/schemas.py`, `docs/03-messages.md` | 메시지 계약 | 공동 (3인 리뷰) |
