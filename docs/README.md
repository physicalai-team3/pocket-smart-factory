# Pocket Smart Factory — 설계 문서

스마트폰 카메라로 컨베이어 시뮬레이터(`web/sim/index.html`) 화면을 촬영하면,
Docker 마이크로서비스 파이프라인(C1~C4)이 과자를 분할·판정해서
대시보드에 **실시간 영상 + 정상/불량 개수**를 보여주는 프로젝트입니다.

| 문서 | 내용 |
|---|---|
| [01-architecture.md](01-architecture.md) | 전체 구조, 데이터 흐름, 설계 결정과 그 이유 |
| [02-containers.md](02-containers.md) | 컨테이너별 책임·입출력·환경변수·담당자 |
| [03-messages.md](03-messages.md) | **메시지 계약** (WebSocket / HTTP / JSON 스키마) |
| [04-deployment.md](04-deployment.md) | cloudflared 무료 터널 배포, 실행 방법, 트러블슈팅 |
| [05-detection.md](05-detection.md) | CPU 환경 추론 전략, 원형도/볼록도 판정, 추적·카운트, 평가 |
| [06-team-plan.md](06-team-plan.md) | 3인 역할 분담, 일정, Git 규칙, 완료 기준 |

## 한 줄 요약

```
폰(카메라) ──WSS──▶ cloudflared ──▶ nginx ──▶ C1 ──▶ C2 ──▶ C3×N ──▶ C4 ──▶ C1 ──WSS──▶ 대시보드
```

- GPU 없음 → **OpenCV 빠른 경로(매 프레임) + SAM 2 정밀 경로(저빈도)** 하이브리드
- 외부 공개 → **cloudflared Quick Tunnel** (무료, 무계정, HTTPS 자동 → 폰 카메라 사용 가능)
- 계약 우선 → [`shared/schemas.py`](../shared/schemas.py)가 메시지 정의의 단일 출처
