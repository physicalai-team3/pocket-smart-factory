# 06. 2인 작업 계획

## 1. 역할 분담

| | **A (나): 인프라 · 웹 · 스트리밍** | **B (팀원): 비전 · 판정 · 평가** |
|---|---|---|
| 소유 경로 | `docker-compose.yml`, `nginx/`, `web/`, `services/c1_gateway/`, `services/c2_preprocess/`, `tools/replay.py` | `services/c3_inference/`, `services/c4_visualizer/`, `tools/eval_offline.py`, `tools/download_models.sh` |
| 공동 소유 | `shared/schemas.py`, `docs/03-messages.md` (변경 시 둘 다 리뷰) | ← |
| 상대 없이 개발하는 법 | C3 `X-Mode: mock`, C4는 입력 이미지를 그대로 반환하는 스텁 | 시뮬레이터 PNG로 `curl`/pytest 직접 호출, `tools/replay.py`로 C1 경유 |

## 2. 일정 (오늘)

| 단계 | 시간 | A | B | 통합 확인 |
|---|---|---|---|---|
| **0. 계약** | 0:00–0:30 | 함께 `03-messages.md`, `schemas.py` 읽고 확정 | ← | 서로 질문이 없을 것 |
| **1. 뼈대 연결** | 0:30–1:30 | compose 전체 기동, 터널 URL 확인, C1 세션/QR, `phone.html` 카메라 송출 | C3 `mock` 응답, C4 스텁(이미지를 그대로 반환, counts 0) | 폰 영상이 대시보드에 그대로 보이면 성공 ✅ |
| **2. MVP** | 1:30–3:30 | C1 오케스트레이션과 드롭 정책, 대시보드 결과 표시/카운트/지표, C2 리사이즈 | C3 fast(OpenCV) + 판정, 오프라인 평가로 임계값 결정, C4 추적+검사선 카운트+마킹 | **실시간으로 정상/불량 카운트가 올라감** ✅ |
| **3. 정밀화** | 3:30–5:00 | precise 스케줄링, ROI 4점 UI + C2 원근 보정, `config` 실시간 조정 UI | C3 precise(SAM 2), C4 precise 투표 반영, 운동 조건을 넣은 매칭 | 원근 보정 전후 정확도 비교 |
| **4. Scale-out & 측정** | 5:00–6:00 | C3 복제 1/2/3 처리량 측정, 대시보드 그래프 | 온라인 3분 카운트 오차 측정 | 발표용 수치 확보 |
| **5. 마무리** | 6:00– | README 실행 방법, 데모 리허설 | 결과 표·스크린샷 | — |

> 막히면 **MVP(2단계)를 먼저 지킵니다.** SAM 2는 fast 위에 얹는 옵션이라 실패해도 데모는 성립합니다.

## 3. Git 규칙

- `main`은 항상 `docker compose up`이 되는 상태를 유지합니다.
- 브랜치: `feat/c1-gateway`, `feat/web-dashboard`, `feat/c3-opencv`, `feat/c4-tracker`, `contract/<내용>` …
- 커밋 메시지: `c3: add solidity feature`, `web: QR panel` 처럼 `<서비스>: <내용>`
- PR은 작게, 자주 올립니다. 상대 소유 디렉터리를 고칠 때는 PR에 상대를 리뷰어로 지정합니다.
- **커밋 금지**: `.env`, `models/*.pt`, `data/` (시뮬레이터 캡처), `__pycache__/`

## 4. 통합 체크리스트

- [ ] `docker compose up -d --build` 한 번으로 전체가 기동된다
- [ ] 모든 서비스 `/health`가 200을 반환한다 (`/api/health`의 `deps`가 전부 `ok`)
- [ ] 대시보드에 QR이 뜨고, 터널이 재시작되면 QR이 자동으로 바뀐다
- [ ] 폰(Android Chrome / iOS Safari)에서 후면 카메라 송출이 된다
- [ ] 폰을 끊었다 다시 연결해도 카운트가 유지된다
- [ ] 대시보드 `reset`으로 카운트가 0이 된다
- [ ] C3 복제본을 늘리면 `c3_replicas_seen`이 늘고 fps가 오른다
- [ ] 시뮬레이터 카운트와 대시보드 카운트의 오차를 기록했다
