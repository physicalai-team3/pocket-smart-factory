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
        prev_x, cur_x 가 line_x·w 를 direction 방향으로 넘었으면:
            verdict = precise 투표가 있으면 precise 다수결, 없으면 fast 다수결 (동률이면 ng)
            counts 갱신, counted=true, events에 추가

for 각 precise 결과 (seq = s, 늦게 도착):
    history[s]의 bbox들과 precise 객체를 IoU로 매칭 → 해당 track_id에 precise 투표 +1
    (이미 counted인 트랙은 카운트를 수정하지 않음: 확정 후에는 바꾸지 않는다)
```

- 시뮬레이터는 여러 라인이 같은 방향으로 움직입니다. 트랙 매칭에 "x는 감소만 함(rtl), y는 거의 그대로"라는 운동 조건을 넣으면 오매칭이 크게 줄어듭니다.
- 검사선은 C1이 precise를 보낸 뒤 결과가 돌아올 시간을 벌 수 있도록, **진행 방향 기준으로 화면 중앙보다 약간 뒤쪽**(rtl이면 `line_x≈0.35~0.45`)에 두는 것을 권장합니다.

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

## 7. 완료 기준 (목표치)

| 지표 | MVP | 목표 |
|---|---|---|
| 처리 fps (fast) | ≥ 5 | ≥ 10 |
| p95 지연 (total) | < 1 s | < 300 ms (터널 구간 제외) |
| 오프라인 객체 판정 정확도 (결손 55%) | ≥ 85% | ≥ 95% |
| 온라인 카운트 오차 (3분) | ≤ 15% | ≤ 5% |
