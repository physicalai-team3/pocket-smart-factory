# 05. 검출·판정·카운트 로직 (GPU 없는 환경 기준)

## 1. 왜 하이브리드인가

| 방법 | CPU 처리 시간 (대략, 실측 필요) | 장점 | 단점 |
|---|---|---|---|
| OpenCV 임계값 + 컨투어 | 5~20 ms / 프레임 | 매우 빠름. 시뮬레이터 배경(어두운 벨트)에서 안정적 | 조명·노이즈에 민감, 윤곽이 거침 |
| SAM 2.1 hiera-tiny (box prompt) | 0.5~2 s / 프레임 (인코더 1회 + 디코더 N회) | 정밀한 픽셀 마스크 (발표 자료의 핵심 기술) | 매 프레임에 쓰기엔 너무 느림 |
| SAM 2 자동 마스크 생성 | 수십 초 | 프롬프트가 필요 없음 | **쓰지 않습니다** |

시뮬레이터 기본값(속도 130 px/s, 지름 78 px)에서는 과자가 프레임당 13 px(10fps) 움직입니다.
SAM 2만 쓰면 1fps 근처에서 프레임당 130 px나 움직이므로, 간격(168 px)과 비슷해져 **추적이 깨집니다**.
→ **추적은 fast(OpenCV)로, 정밀 판정은 precise(SAM 2)로** 나눕니다.

SAM 2 인코더는 입력을 항상 1024px로 리사이즈하므로, 과자마다 crop해서 따로 넣으면 오히려 느립니다. 반드시 **프레임 1장 + 여러 box prompt** 방식으로 넣으세요.

### 1.1 SAM 2를 언제 돌리나: 애매할 때만 (`precise_policy=uncertain`)

| 방식 | 동작 | 평가 |
|---|---|---|
| `interval` | 일정 간격마다 무조건 | 명백한 정상·불량에도 CPU를 씀 |
| **`uncertain` (기본)** | C4가 애매한 트랙에 `needs_precise=true` → C1이 다음 프레임을 precise로도 보냄 | SAM 2가 **판정을 실제로 바꿀 수 있는 곳에만** 쓰임 |
| `off` | 안 돌림 | OpenCV 단독 기준선 |

애매함의 기준 (C4, 트랙 단위):
- 아직 카운트 전, 이번 프레임 `partial=false`, precise 투표 0개
- 그리고 `|circularity − circ_th| < 0.02` 또는 `|solidity − solid_th| < 0.02` 또는 fast 투표가 갈림(소수 쪽 ≥ 30%)
- band(`circ_band`·`solid_band`)를 넓게 잡으면 거의 모든 과자가 애매로 분류되어 `uncertain`이 사실상 `interval`과 같아집니다.
  시뮬레이터 캡처 98개 기준 원형도로 애매한 과자: `circ_th=0.935`에서 band 0.05 → 16개, 0.02 → 8개 (`circ_th=0.95`에서는 28개 → 7개).

처리량 확인: 과자 한 개는 화면에 약 7초 머물고, SAM 2 1회로 화면 속 과자 전부를 처리합니다. 그래서 CPU에서 1초에 1회만 돌려도 애매한 과자 전부가 검사선 전에 precise 판정을 받습니다. 검사선이 0.34(rtl)라서 진입 후 화면 폭의 66%를 지나는 동안 여유가 있습니다.

발표용 비교: `off` / `interval` / `uncertain` 세 가지로 **정확도와 SAM 2 호출 횟수**를 표로 만들면 "적은 연산으로 같은 정확도"를 보여줄 수 있습니다.

검토했지만 쓰지 않는 대안:
- SAM 2 영상 추적 모드(memory attention): CPU에서 너무 느림
- MobileSAM / EfficientSAM: 빠르지만 SAM 2가 아니게 되어 발표 내용과 어긋남
- SAM 2 인코더 ONNX/OpenVINO 변환: 효과는 크지만 오늘 일정에서는 선택 과제

## 2. C3 fast 파이프라인 (OpenCV)

```
JPEG → BGR → HSV
  → inRange(갈색 범위)            # 과자 색: 시뮬레이터 기준 RGB(150~255, 93~180, 44~116) 근처
  → morphologyEx(OPEN, CLOSE)    # 초코칩 구멍 메우기, 노이즈 제거
  → findContours(RETR_EXTERNAL)
  → 면적 < MIN_AREA_RATIO × 프레임면적 → 버림
  → 경계에 닿으면 partial=true
  → 특징 계산 → 판정
```
- 초기 HSV 범위는 시뮬레이터 PNG에서 뽑고, 실제 폰 촬영 영상으로 다시 조정합니다(화면 촬영 시 색이 바뀜).
- 화면을 촬영하면 모아레와 줄무늬가 생깁니다. `GaussianBlur(5×5)`를 먼저 적용하세요.
- 벨트 위 클리트 줄(밝은 세로선)은 갈색이 아니라서 임계값에서 걸러집니다.
- 시뮬레이터의 빨간 검사선 점선(RGB 255,90,69)은 색상(hue)이 갈색과 가깝습니다. 범위를 잡을 때 점선이 마스크에 섞이지 않는지 확인하세요. 섞여도 가늘어서 `OPEN` 연산이나 면적 필터로 지워집니다.
- **과자 크기**: 라인 3개를 한 화면에 담고 960px로 보내면 과자 지름이 대략 50px 안팎입니다. `MIN_AREA_RATIO`가 이 크기를 걸러내지 않는지 확인하세요.

## 3. C3 precise 파이프라인 (SAM 2)

```python
predictor = SAM2ImagePredictor(build_sam2(cfg, ckpt, device="cpu"))
with torch.inference_mode():
    predictor.set_image(rgb)                         # 인코더 1회 (대부분의 시간)
    masks, scores, _ = predictor.predict(
        box=np.array(fast_bboxes_xyxy),              # (N,4) 일괄 입력
        multimask_output=False)
→ 마스크마다 findContours → 가장 큰 컨투어 → 특징 계산 → 판정
```
- 설치: `pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu` 후 `SAM2_BUILD_CUDA=0 pip install git+https://github.com/facebookresearch/sam2.git`
- `torch.set_num_threads(int(os.environ["OMP_NUM_THREADS"]))`
- 시간이 남으면: 인코더를 ONNX로 내보내고 onnxruntime이나 OpenVINO로 실행하면 CPU에서 몇 배 빨라질 수 있습니다.

## 4. 형상 특징과 판정

| 특징 | 식 | 완전한 원 |
|---|---|---|
| 원형도 circularity | `4π·A / P²` | 1.0 (디지털 컨투어에서는 0.88~0.95) |
| 볼록도 solidity | `A / convexHullArea` | 1.0 |

시뮬레이터 결함 유형별 특성 (`makeShape()` 분석):
| 유형 | 모양 | 원형도 | 볼록도 | 무엇으로 잡나 |
|---|---|---|---|---|
| `bite` 한쪽 파임 | 원형으로 오목하게 파임 | ↓ | **↓↓** | 볼록도 |
| `wedge` 조각 결손 | 부채꼴 모양으로 깊게 패임 | ↓ | **↓↓** | 볼록도 |
| `cut` 단면 절단 | 직선으로 잘림 (볼록 유지) | **↓** | ≈1 | 원형도 |
| `half` 반쪽 파손 | 반원 | **↓↓** | ≈1 | 원형도 |

→ `defect = circularity < CIRC_TH  OR  solidity < SOLID_TH`

기준값: **`CIRC_TH=0.935`, `SOLID_TH=0.95`** (시뮬레이터 캡처 32장, 과자 493개, `tools/eval_offline.py`)

| 원형도 / 볼록도 | 전체 | 지름 40 (C3 입력 약 33px) | 지름 78 (약 64px) | 지름 140 (약 115px) |
|---|---|---|---|---|
| 0.80 / 0.95 | 85.2% | 86.2% | 85.3% | 81.0% |
| 0.95 / 0.95 | 94.3% | 69.2% | 98.0% | 100% |
| **0.935 / 0.95** | **95.9%** | 93.8% | 96.1% | 100% |

- 작은 과자일수록 정상품의 원형도가 낮게 나옵니다(픽셀 계단). C3 실측 정상품 원형도 최저값은 33px 0.921, 64px 0.970, 115px 0.990입니다.
  0.95로 올리면 작은 정상 과자의 39%가 불량이 됩니다. 0.935는 세 크기 모두에서 무난해서 크기별 기준을 따로 두지 않습니다.
- `SOLID_TH`는 0.95로 둡니다. 0.972로 올리면 정확도는 0.2%p 오르지만 정상품 볼록도(0.97 이상)가 `solid_band` 안에 들어가
  거의 모든 과자가 애매로 분류됩니다.

주의할 점:
- **원근 왜곡**: 폰이 기울면 원이 타원이 되어 원형도가 떨어집니다. ROI 4점 원근 보정(C2)으로 해결합니다. 보정이 없을 때를 위한 보조 특징으로 `fitEllipse` 후 장축/단축 비로 정규화한 원형도를 고려하세요.
- **결손 크기 슬라이더**를 작게(10~20%) 하면 `cut`은 원형도 차이가 아주 작아집니다. 평가 결과로 "검출 가능 하한"을 발표에 포함하면 좋습니다.
- 둘레(P)는 컨투어 노이즈에 민감합니다. `approxPolyDP(ε=0.005·P)` 후에 계산하세요.

## 5. 추적과 카운트 (C4)

```
for 각 fast 결과 (seq 증가 순):
    detections = partial이 아닌 객체 + partial 객체(위치 추적용)
    트랙과 매칭: 거리 행렬(centroid) → 헝가리안 또는 greedy, 임계값 MATCH_MAX_DIST × 지름
    매칭된 트랙: 위치 갱신, defect가 null이 아니면 fast 투표 +1
    매칭 안 된 검출: 새 트랙
    매칭 안 된 트랙: misses +1, MAX_MISSES를 넘으면 삭제
    history[seq] = {track_id: bbox}   # 최근 3초만 보관

    for 각 트랙(counted=false):
        needs_precise = precise 투표 0 and not partial
                        and (특징값이 임계값 band 안 or fast 투표 갈림)
        prev_x, cur_x 가 line_x·w 를 direction 방향으로 넘었으면:
            verdict = precise 투표가 있으면 precise 다수결, 없으면 fast 다수결 (동률이면 ng)
            counts 갱신, counted=true, events에 추가

for 각 precise 결과 (seq = s, 늦게 도착):
    history[s]의 bbox들과 precise 객체를 IoU로 매칭 → 해당 track_id에 precise 투표 +1
    (이미 counted인 트랙은 카운트를 수정하지 않음: 확정 후에는 바꾸지 않는다)
```

- 시뮬레이터는 여러 라인이 같은 방향으로 움직입니다. 트랙 매칭에 "x는 감소만 함(rtl), y는 거의 그대로"라는 운동 조건을 넣으면 오매칭이 크게 줄어듭니다.
- 라인 수(1~6)에 대한 가정은 없습니다. 과자 단위로 추적하고, 세로 검사선 하나가 모든 라인을 가로지르며 총합을 셉니다.
- 검사선 기본값 `line_x=0.34`는 **시뮬레이터가 세는 위치와 같습니다**. ROI로 캔버스만 잘라냈을 때 두 카운트가 거의 동시에 올라갑니다. rtl 진행이라 과자가 검사선까지 화면 폭의 66%를 지나므로, precise 결과가 돌아올 시간도 충분합니다.

## 6. 평가 방법

### 6.1 오프라인 (C3 단독, C 담당)
1. 시뮬레이터에서 `S`(프레임 PNG)와 `L`(정답 JSON)을 같은 순간에 저장합니다. 일시정지 후 두 개 모두 누르세요.
   - 다양한 조건으로 30장 이상: 결손 크기 20/55/90%, 조명 0/50%, 노이즈 0/30%, 지름 40/78/140
2. `python tools/eval_offline.py --data data/sim --c3 http://localhost:8003`
   - 정답 `bbox`(dpr 보정 포함)와 C3 객체를 IoU ≥ 0.5로 매칭
   - 객체 단위 confusion matrix, 정밀도/재현율, `CIRC_TH`·`SOLID_TH` 격자 탐색 → 최적 임계값 출력
   - 정답 JSON의 `roundness` 값과 우리가 계산한 특징의 상관관계도 확인합니다.

### 6.2 온라인 (전체 시스템)
- 시뮬레이터에서 "라인 비우기"와 대시보드 "reset"을 동시에 누르고 3분간 촬영합니다.
- 시뮬레이터 `정상/불량` vs 대시보드 `ok/ng` 비교 → 카운트 오차율
- 함께 기록할 것: fps_in, fps_out, drop률, p95 지연 (C3 복제본 1 / 2 / 3)
- `precise_policy` off / interval / uncertain 별 카운트 오차와 SAM 2 호출 횟수

## 7. 완료 기준 (목표치)

| 지표 | MVP | 목표 |
|---|---|---|
| 처리 fps (fast) | ≥ 5 | ≥ 10 |
| p95 지연 (total) | < 1 s | < 300 ms (터널 구간 제외) |
| 오프라인 객체 판정 정확도 (결손 55%) | ≥ 85% | ≥ 95% |
| 온라인 카운트 오차 (3분) | ≤ 15% | ≤ 5% |
