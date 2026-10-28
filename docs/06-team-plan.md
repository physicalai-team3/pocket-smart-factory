# 06. 3인 작업 계획

## 1. 역할 분담

묶는 기준은 **서로 자주 맞춰봐야 하는 코드는 한 사람에게** 입니다. 사람 사이의 경계는 [03-messages.md](03-messages.md)의 계약으로만 만납니다.

| | **A (나): 인프라 · 스트리밍** | **B: 결과 · 화면** | **C: 비전 모델** |
|---|---|---|---|
| 한 줄 요약 | 프레임을 **받고 · 다듬고 · 나르는** 일 | 판정 결과를 **이어 붙이고 · 세고 · 보여주는** 일 | 프레임 한 장을 **보고 판단하는** 일 |
| 소유 경로 | `docker-compose.yml`, `nginx/`, `.env.example`, `services/c1_gateway/`, `services/c2_preprocess/`, `web/phone.html`, `tools/replay.py` | `web/index.html` (대시보드), `services/c4_visualizer/` | `services/c3_inference/`, `tools/eval_offline.py`, `tools/download_models.sh` |
| 핵심 기술 | asyncio, WebSocket, httpx, Docker/nginx, getUserMedia, OpenCV 원근 변환 | 프론트엔드(JS/Canvas), 다중 객체 추적, 상태 관리 | OpenCV 분할, SAM 2(CPU), 형상 특징, 임계값 튜닝 |
| 왜 이렇게 묶었나 | 폰 ↔ C1 ↔ C2는 같은 프레임 형식과 백프레셔 정책을 공유. C2는 작업량이 가벼워 A에 합침 | C4가 만드는 tracks/counts/마킹을 대시보드가 그대로 표시하고, 검사선·reset 설정 UI가 C4 동작과 1:1로 대응 | 원형도·볼록도 계산과 임계값 평가는 한 사람이 같은 데이터로 반복해야 빠름. SAM 2 설치·속도 위험을 한 곳에 격리 |
| 상대 없이 개발하는 법 | C3 `X-Mode: mock`, C4 스텁(이미지를 그대로 반환) | 저장된 `InferResult` JSON을 C4에 `curl`로 넣기, 대시보드는 가짜 `result` 메시지로 개발 | 시뮬레이터 PNG + 정답 JSON으로 C3만 단독 실행 |

공동 소유: `shared/schemas.py`, `docs/03-messages.md` → 변경 PR은 **세 명 모두** 리뷰합니다.

### 사람 사이 경계 (계약 지점)

```
          A                         │        C        │          B
 phone.html ─▶ C1 ─▶ C2              │                 │
               C1 ── POST /infer ───▶│  C3 (InferResult)│
               C1 ── POST /track ────┼─────────────────┼─▶ C4 (TrackResponse)
               C1 ══ /ws/dashboard ══┼─────────────────┼═▶ 대시보드
```
- A ↔ C: `POST /infer` (§4.2)
- A ↔ B: `POST /track` (§4.3), `/ws/dashboard` (§3.2)
- B ↔ C: 직접 호출은 없고 `InferResult` 스키마만 공유합니다. 판정 기준(`defect`, `partial`)의 의미는 둘이 같이 정합니다.

## 2. 일정 (오늘)

| 단계 | 시간 | A (인프라·스트리밍) | B (결과·화면) | C (비전 모델) | 통합 확인 |
|---|---|---|---|---|---|
| **0. 계약** | 0:00–0:30 | 셋이 함께 `03-messages.md`, `schemas.py` 확정 | ← | ← | 질문이 없을 것 |
| **1. 뼈대 연결** | 0:30–1:30 | compose 기동, 터널 URL, C1 세션/QR API, `phone.html` 송출 | 대시보드 QR·영상 표시, C4 스텁 | C3 `mock`, 시뮬레이터 데이터 30장 수집 시작 | 폰 영상이 대시보드에 그대로 보임 ✅ |
| **2. MVP** | 1:30–3:30 | C1 오케스트레이션·드롭 정책·지표, C2 리사이즈 | C4 추적 + 검사선 카운트 + 마킹, 대시보드 카운트·지표 | C3 fast(OpenCV) + 판정, `eval_offline.py`로 임계값 결정 | **실시간으로 정상/불량 카운트가 올라감** ✅ |
| **3. 정밀화** | 3:30–5:00 | precise 스케줄링, C2 원근 보정 | 대시보드 ROI 4점 UI·config UI, C4 precise 투표, 운동 조건 매칭 | C3 precise(SAM 2), 결손 크기별 검출 하한 측정 | 원근 보정 전후 정확도 비교 |
| **4. Scale-out & 측정** | 5:00–6:00 | C3 복제 1/2/3 처리량 측정 | 대시보드에 처리량·지연 그래프 | SAM 2 ONNX 최적화(선택), C3 단독 처리 시간 표 | 온라인 3분 카운트 오차 (B+C) |
| **5. 마무리** | 6:00– | README 실행 방법, 데모 리허설 | 데모 화면 다듬기, 스크린샷 | 결과 표·그래프 | — |

> 막히면 **MVP(2단계)를 먼저 지킵니다.** SAM 2는 fast 위에 얹는 옵션이라 실패해도 데모는 성립합니다.

## 3. Git 규칙

- `main`은 항상 `docker compose up`이 되는 상태를 유지합니다. 직접 push하지 말고 PR로 올립니다.
- 브랜치: `feat/c1-gateway`, `feat/dashboard`, `feat/c4-tracker`, `feat/c3-opencv`, `contract/<내용>` …
- 커밋 메시지: `c3: add solidity feature`, `web: QR panel` 처럼 `<서비스>: <내용>`
- PR은 작게, 자주 올립니다. 다른 사람 소유 디렉터리를 고칠 때는 그 사람을 리뷰어로 지정합니다.
- **커밋 금지**: `.env`, `models/*.pt`, `data/` (시뮬레이터 캡처), `__pycache__/`

## 4. 통합 체크리스트

- [ ] `docker compose up -d --build` 한 번으로 전체가 기동된다 (A)
- [ ] 모든 서비스 `/health`가 200 (`/api/health`의 `deps`가 전부 `ok`) (A)
- [ ] 대시보드에 QR이 뜨고, 터널이 재시작되면 QR이 자동으로 바뀐다 (A+B)
- [ ] 폰(Android Chrome / iOS Safari)에서 후면 카메라 송출이 된다 (A)
- [ ] 폰을 끊었다 다시 연결해도 카운트가 유지된다 (A+B)
- [ ] 대시보드 `reset`으로 카운트가 0이 된다 (B)
- [ ] 오프라인 판정 정확도와 최적 임계값을 기록했다 (C)
- [ ] C3 복제본을 늘리면 `c3_replicas_seen`이 늘고 fps가 오른다 (A+C)
- [ ] 시뮬레이터 카운트와 대시보드 카운트의 오차를 기록했다 (B+C)
