# 02. 컨테이너별 명세

> 담당자는 [06-team-plan.md](06-team-plan.md#1-역할-분담)를 보세요 (A 인프라·스트리밍 / B 결과·화면 / C 비전 모델).
>
> | 구성요소 | web(nginx) | cloudflared | C1 | C2 | phone.html | 대시보드 index.html | C4 | C3 | tools |
> |---|---|---|---|---|---|---|---|---|---|
> | 담당 | A | A | A | A | A | B | B | C | replay A · eval/models C |
>
> 모든 Python 서비스: `python:3.11-slim` + FastAPI + uvicorn. 빌드 컨텍스트는 repo 루트(`shared/` 복사 목적).
> 모든 서비스는 `GET /health` → `{"status":"ok","service":"<name>"}` 를 제공합니다.

---

## web (nginx)

| 항목 | 내용 |
|---|---|
| 이미지 | `nginx:1.27-alpine` |
| 경로 | `nginx/nginx.conf`, `web/` (볼륨 마운트 → `/usr/share/nginx/html`) |
| 역할 | 정적 파일 서빙, `/api`·`/ws` → C1 프록시, 내부 `:8090` → C3 라운드로빈 |

정적 파일:
| URL | 파일 | 설명 |
|---|---|---|
| `/` | `web/index.html` | 대시보드 (QR, 결과 영상, 카운트, 지표, 설정) |
| `/phone.html?s=<session>` | `web/phone.html` | 폰 카메라 송출 페이지 |
| `/sim/` | `web/sim/index.html` | 과제 시뮬레이터 원본 |

주의:
- C3 복제본은 nginx 시작 시점에 DNS로 IP를 찾습니다. **C3 개수를 바꾸면 `docker compose restart web`** 을 실행해야 합니다.
- WebSocket 프록시에는 `Upgrade`/`Connection` 헤더와 `proxy_read_timeout 3600s`가 필요합니다.

---

## cloudflared

| 항목 | 내용 |
|---|---|
| 이미지 | `cloudflare/cloudflared:2026.10.5` |
| 명령 | `tunnel --no-autoupdate --metrics 0.0.0.0:2000 --url http://web:80` |
| 출력 | 공개 URL `https://<random>.trycloudflare.com` (로그와 `GET http://cloudflared:2000/quicktunnel`로 확인) |

자세한 내용은 [04-deployment.md](04-deployment.md)를 참고하세요.

---

## C1 Gateway

| 항목 | 내용 |
|---|---|
| 경로 | `services/c1_gateway/` |
| 포트 | 8001 |
| 상태 | 세션 목록, 세션별 연결된 폰/대시보드 WS, 처리 중 프레임 수, 지표 |

책임:
1. **세션**: 대시보드가 세션을 만들면 무작위 ID(8자)를 발급하고, 폰 URL과 QR PNG를 생성합니다.
2. **공개 URL 확인**: `PUBLIC_URL` 환경변수가 있으면 그 값을, 없으면 `CLOUDFLARED_METRICS/quicktunnel`을 주기적으로 조회해 씁니다(URL이 바뀌면 대시보드에 `session` 메시지를 다시 보냅니다).
3. **폰 수신**: `/ws/phone`에서 바이너리 프레임을 받고 백프레셔 정책으로 드롭합니다.
4. **오케스트레이션**: 프레임마다 비동기 task를 만들어 C2 → C3 → C4를 호출합니다(`httpx.AsyncClient`, 타임아웃 적용).
5. **precise 스케줄링**: 마지막 precise 요청 후 `PRECISE_INTERVAL_MS`가 지났고 진행 중인 precise 요청이 없으면 그 프레임을 `mode=precise`로 보냅니다.
6. **푸시**: C4 결과를 대시보드 WS로 보내고, 1초마다 `stats`를 폰과 대시보드에 보냅니다.
7. **설정 보관**: 세션별 `SessionConfig`(ROI, 검사선, 임계값)를 갖고 있다가 C2·C3·C4 요청에 실어 보냅니다. `reset`은 C4 `/reset`으로 전달합니다.

엔드포인트: [03-messages.md §2, §3](03-messages.md)

환경변수:
| 변수 | 기본값 | 설명 |
|---|---|---|
| `PUBLIC_URL` | (비움) | 지정하면 터널 조회 대신 이 값 사용 |
| `CLOUDFLARED_METRICS` | `http://cloudflared:2000` | |
| `C2_URL` | `http://c2-preprocess:8002` | |
| `C3_URL` | `http://web:8090` | nginx 내부 LB |
| `C4_URL` | `http://c4-visualizer:8004` | |
| `MAX_INFLIGHT` | `4` | 동시에 처리 중인 프레임 상한 |
| `PRECISE_INTERVAL_MS` | `700` | precise 요청 최소 간격 (0이면 precise 끔) |
| `STAGE_TIMEOUT_MS` | `3000` | 단계별 HTTP 타임아웃 |

---

## C2 Preprocess

| 항목 | 내용 |
|---|---|
| 경로 | `services/c2_preprocess/` |
| 포트 | 8002 |
| 상태 | **없음** (ROI는 C1이 `X-Roi` 헤더로 매 요청 전달) |
| 의존성 | `opencv-python-headless`, `numpy` |

책임:
1. JPEG 디코드 (실패하면 422)
2. ROI가 있으면 4점 → `cv2.getPerspectiveTransform` → `warpPerspective`로 **시뮬레이터 캔버스를 정면으로 편 영상**을 만듭니다(원근 때문에 원형도가 떨어지는 문제를 해결).
3. 긴 변을 `TARGET_WIDTH`(기본 640)로 리사이즈
4. (선택) 밝기 정규화: CLAHE (시뮬레이터 "조명 불균일" 대응)
5. JPEG(q=85)로 다시 인코딩해서 반환하고, 출력 크기를 헤더로 알려줍니다.

엔드포인트: `POST /preprocess` — [03-messages.md §4.1](03-messages.md)

---

## C3 Inference

| 항목 | 내용 |
|---|---|
| 경로 | `services/c3_inference/` |
| 포트 | 8003 |
| 상태 | **없음** (모델 가중치만 메모리에 로드) |
| 복제 | `C3_REPLICAS` (기본 2) |
| 의존성 | 공통: `opencv-python-headless`, `numpy` / SAM 2: `torch`(CPU wheel), `sam2` |

백엔드 (`C3_BACKEND` 환경변수 또는 요청 헤더 `X-Mode`):
| 이름 | 동작 | 용도 |
|---|---|---|
| `mock` | 고정된 가짜 객체 2개 반환 | A가 C3 없이 파이프라인 개발할 때 |
| `fast` | OpenCV: HSV 임계값 → morphology → `findContours` → 특징 | 매 프레임 |
| `precise` | fast로 bbox 후보 → SAM 2 `SAM2ImagePredictor`에 box prompt 일괄 입력 → 마스크 → 특징 | 저빈도 |

판정 (객체 단위, 무상태):
- `circularity = 4πA / P²`, `solidity = A / hull_area`
- `defect = circularity < CIRC_TH or solidity < SOLID_TH`
- **프레임 가장자리에 닿은 객체는 `partial=true`** 로 표시하고 판정하지 않습니다(잘린 과자는 불량처럼 보이기 때문).

환경변수:
| 변수 | 기본값 | 설명 |
|---|---|---|
| `C3_BACKEND` | `fast` | `X-Mode` 헤더가 없을 때 쓰는 기본 모드 |
| `SAM2_ENABLED` | `0` | 1이면 시작 시 SAM 2 로드 (이미지 빌드 시 `WITH_SAM2=1` 필요) |
| `SAM2_CKPT` | `/models/sam2.1_hiera_tiny.pt` | 볼륨 `./models` 마운트 |
| `SAM2_CFG` | `configs/sam2.1/sam2.1_hiera_t.yaml` | |
| `CIRC_TH` | `0.80` | 초깃값. 오프라인 평가로 조정 |
| `SOLID_TH` | `0.95` | 초깃값. 오프라인 평가로 조정 |
| `MIN_AREA_RATIO` | `0.002` | 프레임 면적 대비 최소 객체 크기 (노이즈 제거) |
| `OMP_NUM_THREADS` | `2` | **코어 수 / 복제본 수**로 맞추세요 |

---

## C4 Visualizer

| 항목 | 내용 |
|---|---|
| 경로 | `services/c4_visualizer/` |
| 포트 | 8004 |
| 상태 | 세션별: 트랙 목록, 최근 seq, 트랙 위치 이력(최근 3초), 누적 카운트 (검사선 설정은 요청 `config`로 받음) |
| 복제 | **반드시 1개** |
| 의존성 | `opencv-python-headless`, `numpy`, (선택) `scipy`(헝가리안 매칭) |

책임:
1. **순서 검사**: `mode=fast` 결과의 `seq`가 세션의 `last_seq` 이하이면 추적에 쓰지 않습니다(`stale=true`로 응답). `mode=precise` 결과는 늦게 오는 것이 정상이므로 위치 이력에서 해당 seq를 찾아 트랙에 붙입니다.
2. **추적**: centroid + IoU 매칭으로 트랙 ID를 유지합니다. `MAX_MISSES` 프레임 동안 안 보이면 트랙을 삭제합니다.
3. **판정 누적**: 트랙마다 fast 투표와 precise 투표를 쌓습니다. 최종 판정은 precise 투표가 있으면 그것을 우선합니다.
4. **카운트**: 트랙 centroid가 검사선(`line_x` 비율, 방향 `rtl`/`ltr`)을 넘을 때 **1회** 확정 카운트합니다.
5. **마킹**: 원본(전처리된) 프레임에 윤곽선(정상 초록 / 불량 빨강 / 미확정 노랑), 트랙 ID, 검사선, 카운트를 그려 JPEG로 반환합니다.

엔드포인트: `POST /track`, `POST /reset/{session}`, `DELETE /sessions/{session}` — [03-messages.md §4.3](03-messages.md)

환경변수:
| 변수 | 기본값 | 설명 |
|---|---|---|
| `LINE_X` | `0.5` | 검사선 위치 (프레임 폭 비율) |
| `DIRECTION` | `rtl` | 시뮬레이터 진행 방향은 ← (right-to-left) |
| `MAX_MISSES` | `5` | |
| `MATCH_MAX_DIST` | `0.6` | 매칭 허용 거리 (객체 지름 대비 비율) |
| `JPEG_QUALITY` | `70` | 대시보드로 보낼 결과 이미지 품질 |

---

## tools/

| 파일 | 용도 |
|---|---|
| `tools/download_models.sh` | SAM 2.1 tiny 체크포인트를 `models/`에 다운로드 |
| `tools/eval_offline.py` | 시뮬레이터에서 저장한 `frame_*.png` + `labels_*.json` → C3 결과와 비교 → 정밀도/재현율, 임계값 탐색 |
| `tools/replay.py` | 폰 없이 PNG 폴더나 동영상을 `/ws/phone`으로 재생해 보냄 (부하 테스트, 데모 예비용) |
