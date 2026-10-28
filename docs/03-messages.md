# 03. 메시지 계약

> 이 문서와 [`shared/schemas.py`](../shared/schemas.py)는 **항상 함께** 수정합니다. 계약 변경 PR은 두 사람 모두 리뷰합니다.

## 0. 공통 규칙

| 항목 | 규칙 |
|---|---|
| `session` | C1이 발급하는 8자 영숫자 ID (예: `k3x9q2ab`). URL 쿼리 `s=` 로 전달 |
| `seq` | **C1이 세션별로 매기는 단조 증가 정수** (0부터). 폰이 다시 연결돼도 이어서 증가. 폰이 보낸 번호는 `client_seq`로 따로 보관 |
| 시간 | 모두 **epoch 밀리초 (float)**. 지연 계산은 폰 시계가 아닌 C1의 수신 시각 `t_recv` 기준 (폰과 노트북의 시계 차이 때문) |
| 좌표 | **C2 출력 이미지의 픽셀 좌표** (정수). 응답에 `w`, `h` 함께 포함 |
| 정규화 좌표 | ROI, 검사선처럼 **해상도와 무관한 설정**은 0~1 비율 |
| 판정 값 | 객체: `defect: true/false/null`(null = 판정 불가) · 트랙: `verdict: "ok" / "ng" / "pending"` |
| 인코딩 | JSON은 UTF-8, 키는 snake_case. 이미지는 JPEG |

### 바이너리 봉투(Envelope) — WebSocket에서 JSON과 JPEG를 한 메시지로 보낼 때

```
┌───────────────┬──────────────────────┬────────────────────┐
│ uint32 BE (4B)│ header JSON (UTF-8)  │ JPEG bytes          │
│ = header 길이  │ header_len 바이트      │ 나머지 전부           │
└───────────────┴──────────────────────┴────────────────────┘
```

JS 인코딩 예:
```js
function pack(header, jpegBlob) {
  const h = new TextEncoder().encode(JSON.stringify(header));
  const len = new Uint8Array(4); new DataView(len.buffer).setUint32(0, h.length);
  return new Blob([len, h, jpegBlob]);           // ws.send(blob)
}
```
Python 디코딩은 `shared/schemas.py`의 `unpack_envelope()` / `pack_envelope()` 를 사용합니다.

---

## 1. 전체 흐름 (시퀀스)

```
대시보드        C1                    폰            C2        C3(LB)       C4
  │ POST /api/sessions                                                      
  │──────────▶│ session, phone_url, qr                                       
  │◀──────────│                                                              
  │ WS /ws/dashboard?s=                                                      
  │══════════▶│                                                              
  │           │◀════ WS /ws/phone?s= ══│                                     
  │◀─ phone{connected:true} ─│          │                                     
  │           │◀── hello ───────────────│                                     
  │           │◀── [frame envelope] ────│   (반복)                             
  │           │── POST /preprocess ───────────▶│                              
  │           │◀────────── JPEG ───────────────│                              
  │           │── POST /infer (X-Mode) ──────────────────▶│                   
  │           │◀────────── InferResult ───────────────────│                   
  │           │── POST /track ───────────────────────────────────────▶│       
  │           │◀────────── TrackResponse ─────────────────────────────│       
  │◀─[result envelope]─│                                                     
  │           │── stats (1초마다) ─────▶│                                     
```

---

## 2. REST API — 대시보드 ↔ C1 (nginx `/api/*`)

### `POST /api/sessions`
세션을 새로 만듭니다. 대시보드가 열릴 때 호출하며, 기존 세션을 계속 쓰려면 `localStorage`의 ID로 `GET`을 먼저 시도합니다.

응답 `201`:
```json
{
  "session": "k3x9q2ab",
  "public_url": "https://brave-cat-1234.trycloudflare.com",
  "phone_url": "https://brave-cat-1234.trycloudflare.com/phone.html?s=k3x9q2ab",
  "qr_url": "/api/sessions/k3x9q2ab/qr.png",
  "created_at": 1761620000123.0
}
```
`public_url`을 아직 모르면(터널 기동 중) `null`이 오고, 나중에 WS `session` 메시지로 알려줍니다.

### `GET /api/sessions/{session}`
위와 같은 형식. 없으면 `404`.

### `GET /api/sessions/{session}/qr.png`
`phone_url`을 담은 QR PNG(300×300). `public_url`이 없으면 `503`.

### `GET /api/public-url`
```json
{ "public_url": "https://brave-cat-1234.trycloudflare.com", "source": "quicktunnel" }
```
`source`: `"env"` | `"quicktunnel"` | `"none"`

### `GET /api/health`
```json
{ "status": "ok", "service": "c1-gateway",
  "deps": { "c2": "ok", "c3": "ok", "c4": "ok", "tunnel": "ok" } }
```

---

## 3. WebSocket

### 3.1 `/ws/phone?s={session}` — 폰 ↔ C1

**폰 → C1, 텍스트 (연결 직후 1회)**
```json
{ "type": "hello",
  "device": { "ua": "Mozilla/5.0 ...", "w": 1280, "h": 720 },
  "send_w": 640, "send_h": 360, "target_fps": 10 }
```

**폰 → C1, 바이너리 (프레임마다)** — 봉투 header:
```json
{ "type": "frame", "client_seq": 512, "ts_capture": 1761620001234.5, "w": 640, "h": 360 }
```
+ JPEG (q≈0.7). 폰은 `ws.bufferedAmount > 256KB`이면 이번 프레임을 건너뜁니다.

**C1 → 폰, 텍스트**
```json
{ "type": "welcome", "session": "k3x9q2ab", "target_fps": 10 }
{ "type": "stats", "fps_in": 9.8, "fps_out": 7.5, "dropped": 41, "latency_ms": 180 }
{ "type": "throttle", "target_fps": 6 }
{ "type": "error", "code": "SESSION_NOT_FOUND", "message": "..." }
```
- `throttle`: 드롭률이 높으면 C1이 폰의 전송 fps를 낮추도록 요청합니다(선택 구현).
- 세션이 없으면 `error` 후 close code `4404`. 같은 세션에 폰이 새로 연결되면 이전 폰 연결은 `4409`로 닫습니다.

### 3.2 `/ws/dashboard?s={session}` — 대시보드 ↔ C1

**C1 → 대시보드, 텍스트**
```json
{ "type": "session", "session": "k3x9q2ab", "public_url": "...", "phone_url": "...", "qr_url": "..." }
{ "type": "phone", "connected": true, "device": { "ua": "...", "w": 1280, "h": 720 } }
{ "type": "stats",
  "fps_in": 9.8, "fps_out": 7.5, "dropped": 41, "errors": { "c2": 0, "c3": 1, "c4": 0 },
  "latency_ms": { "c2": 9, "c3": 22, "c4": 8, "total": 61, "p95_total": 140 },
  "precise": { "fps": 1.2, "last_ms": 830 },
  "c3_replicas_seen": 2 }
{ "type": "config", "config": { ...현재 SessionConfig... } }
```

**C1 → 대시보드, 바이너리 (처리된 프레임마다)** — 봉투 header:
```json
{
  "type": "result",
  "seq": 1042,
  "w": 640, "h": 360,
  "counts": { "total": 37, "ok": 26, "ng": 11 },
  "tracks": [
    { "track_id": 88, "bbox": [412, 40, 76, 77], "verdict": "ng", "counted": false }
  ],
  "events": [
    { "type": "counted", "track_id": 85, "verdict": "ok", "seq": 1042 }
  ],
  "latency_ms": { "c2": 9, "c3": 22, "c4": 8, "total": 61 }
}
```
+ JPEG (C4가 마킹한 결과 이미지)

**대시보드 → C1, 텍스트**
```json
{ "type": "config", "config": { "line_x": 0.5, "direction": "rtl", "circ_th": 0.8, "solid_th": 0.95, "precise_interval_ms": 700 } }
{ "type": "roi", "points": [[0.08,0.12],[0.93,0.10],[0.95,0.88],[0.06,0.90]] }
{ "type": "roi", "points": null }
{ "type": "reset" }
```
- `config`는 **보낸 필드만** 바꿉니다(부분 업데이트). C1은 반영 후 `config` 메시지로 전체 값을 다시 보냅니다.
- `roi`의 4점 순서는 **좌상 → 우상 → 우하 → 좌하**이고, **폰 원본 프레임 기준 비율**입니다. 대시보드에서는 ROI를 지정하는 동안 C2를 거치지 않은 원본을 보여줘야 하므로, C1은 ROI 편집 중일 때 `X-Roi` 헤더를 빼고 보냅니다.
- `reset`: C4 카운트와 트랙을 초기화합니다(시뮬레이터 "라인 비우기"와 같이 누릅니다).

#### SessionConfig (C1이 보관, 각 단계 요청에 실어 보냄)
| 필드 | 타입 | 기본값 | 사용처 |
|---|---|---|---|
| `roi` | `[[x,y]×4] \| null` | `null` | C2 (`X-Roi`) |
| `line_x` | float 0~1 | 0.5 | C4 |
| `direction` | `"rtl" \| "ltr"` | `"rtl"` | C4 |
| `circ_th` | float | 0.80 | C3 (`X-Circ-Th`) |
| `solid_th` | float | 0.95 | C3 (`X-Solid-Th`) |
| `precise_interval_ms` | int | 700 (0=끔) | C1 |

→ 이 구조 덕분에 **C2와 C3는 상태 없이** 동작합니다. 설정은 C1이 갖고 있고, 요청마다 함께 전달됩니다.

---

## 4. 내부 HTTP (C1 → 각 서비스)

공통 요청 헤더:
| 헤더 | 예 | 설명 |
|---|---|---|
| `X-Session` | `k3x9q2ab` | |
| `X-Seq` | `1042` | |
| `X-Ts-Recv` | `1761620001250.1` | C1 수신 시각 |

공통 에러 응답 (`4xx`/`5xx`):
```json
{ "error": "DECODE_FAILED", "message": "cv2.imdecode returned None" }
```
C1은 한 단계라도 실패하거나 타임아웃이 나면 **그 프레임을 버리고** `errors[stage]`를 1 올립니다. 재시도는 하지 않습니다.

### 4.1 C2 `POST /preprocess`

요청: `Content-Type: image/jpeg`, body = 폰 JPEG 원본
| 추가 헤더 | 예 | 설명 |
|---|---|---|
| `X-Roi` | `0.08,0.12,0.93,0.10,0.95,0.88,0.06,0.90` | 없으면 원근 보정 안 함 |
| `X-Target-Width` | `640` | 생략하면 C2 기본값 |

응답 `200`: `Content-Type: image/jpeg`, body = 처리된 JPEG
| 응답 헤더 | 예 |
|---|---|
| `X-Width` / `X-Height` | `640` / `360` |
| `X-Roi-Applied` | `1` / `0` |
| `X-Proc-Ms` | `8.4` |

### 4.2 C3 `POST /infer`

요청: `Content-Type: image/jpeg`, body = C2 출력 JPEG
| 추가 헤더 | 값 | 설명 |
|---|---|---|
| `X-Mode` | `fast` \| `precise` | 생략하면 `C3_BACKEND` |
| `X-Circ-Th` | `0.80` | 생략하면 환경변수 값 |
| `X-Solid-Th` | `0.95` | |

응답 `200` **InferResult**:
```json
{
  "session": "k3x9q2ab",
  "seq": 1042,
  "mode": "fast",
  "backend": "opencv",
  "replica": "c3-inference-2",
  "w": 640, "h": 360,
  "infer_ms": 14.2,
  "objects": [
    {
      "bbox": [412, 40, 76, 77],
      "centroid": [450.1, 78.4],
      "area": 4512.0,
      "perimeter": 241.7,
      "circularity": 0.971,
      "solidity": 0.994,
      "contour": [[412,78],[418,55],"... 최대 64점 (approxPolyDP)"],
      "partial": false,
      "defect": false,
      "score": 1.0
    }
  ]
}
```
- `partial=true`(프레임 경계에 닿음)이면 `defect=null`입니다.
- `score`: fast는 1.0, precise는 SAM 2가 예측한 IoU입니다.
- `mode=precise`인데 SAM 2가 꺼져 있으면 `409 {"error":"PRECISE_UNAVAILABLE"}`을 반환하고, C1은 precise 스케줄링을 멈춥니다.
- `replica`에는 컨테이너 hostname이 들어갑니다. 로드밸런싱이 실제로 분산되는지 대시보드에서 확인할 수 있습니다.

### 4.3 C4 `POST /track`

요청 `application/json` **TrackRequest**:
```json
{
  "session": "k3x9q2ab",
  "seq": 1042,
  "mode": "fast",
  "config": { "line_x": 0.5, "direction": "rtl" },
  "infer": { "...InferResult 그대로..." },
  "image_jpeg_b64": "/9j/4AAQ..."
}
```
- `mode=precise`일 때는 `image_jpeg_b64`를 생략할 수 있습니다(마킹하지 않고 트랙 판정만 갱신).

응답 `200` **TrackResponse**:
```json
{
  "session": "k3x9q2ab",
  "seq": 1042,
  "stale": false,
  "counts": { "total": 37, "ok": 26, "ng": 11 },
  "tracks": [
    { "track_id": 88, "bbox": [412,40,76,77], "verdict": "ng", "counted": false,
      "votes": { "fast_ok": 2, "fast_ng": 5, "precise_ok": 0, "precise_ng": 1 } }
  ],
  "events": [ { "type": "counted", "track_id": 85, "verdict": "ok", "seq": 1042 } ],
  "image_jpeg_b64": "/9j/4AAQ...",
  "proc_ms": 7.9
}
```
- `stale=true`: 이미 더 최신 fast 프레임을 처리한 상태입니다. C1은 이 결과를 **대시보드에 보내지 않습니다**.
- precise 응답에는 `image_jpeg_b64: null`이 옵니다. C1은 대시보드로 영상을 보내지 않고, 다음 `result`에 갱신된 카운트가 반영됩니다.

### 4.4 C4 `POST /reset/{session}` · `DELETE /sessions/{session}`
응답 `200 {"ok": true}`. `DELETE`는 C1이 세션을 정리할 때(대시보드·폰 모두 끊기고 10분이 지났을 때) 호출합니다.

---

## 5. 에러 코드

| 코드 | 위치 | 의미 |
|---|---|---|
| `SESSION_NOT_FOUND` | C1 | 없는 세션 (WS close `4404`) |
| `PHONE_REPLACED` | C1 | 같은 세션에 새 폰이 연결됨 (WS close `4409`) |
| `DECODE_FAILED` | C2, C3 | JPEG 디코드 실패 (`422`) |
| `BAD_ROI` | C2 | 점이 4개가 아니거나 범위를 벗어남 (`400`) |
| `PRECISE_UNAVAILABLE` | C3 | SAM 2 미로드 상태 (`409`) |
| `MODEL_ERROR` | C3 | 추론 중 예외 발생 (`500`) |
