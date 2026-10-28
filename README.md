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

## 문서

설계 문서는 [docs/](docs/README.md)에 있습니다. 작업 전에 **[03-messages.md](docs/03-messages.md)** (메시지 계약)를 먼저 읽어주세요.

| 디렉터리 | 내용 | 담당 |
|---|---|---|
| `nginx/`, `web/`, `services/c1_gateway/`, `services/c2_preprocess/` | 인프라, 웹, 스트리밍, 전처리 | A |
| `services/c3_inference/`, `services/c4_visualizer/`, `tools/eval_offline.py` | 추론, 판정, 추적, 평가 | B |
| `shared/schemas.py`, `docs/03-messages.md` | 메시지 계약 | 공동 |
