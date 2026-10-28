# 04. 배포 — cloudflared Quick Tunnel

## 1. 비용과 제약

**무료입니다.** Cloudflare 계정, 도메인, 카드 등록이 모두 필요 없습니다.

| 항목 | Quick Tunnel (우리가 쓰는 방식) | Named Tunnel (참고) |
|---|---|---|
| 비용 | 무료 | 무료 (단, 도메인을 Cloudflare에 연결해야 하고 도메인은 유료) |
| 계정 | 불필요 | 필요 |
| URL | `https://<랜덤>.trycloudflare.com`, **재시작할 때마다 바뀜** | 고정 (`app.내도메인.com`) |
| HTTPS | 자동 | 자동 |
| WebSocket | 지원 | 지원 |
| 제약 | 동시 진행 요청 200개 제한, SSE 미지원, 가용성 보장 없음(테스트·데모용) | 운영용 |

→ URL이 바뀌는 문제는 **C1이 터널 주소를 자동으로 조회해서 QR을 다시 만들기 때문에** 신경 쓰지 않아도 됩니다.

## 2. 동작 원리

```
폰 ──HTTPS──▶ Cloudflare 엣지 ◀══ 아웃바운드 연결 ══ cloudflared 컨테이너 ──HTTP──▶ web:80 (nginx)
```
- cloudflared가 노트북에서 Cloudflare로 **나가는 방향**으로 연결하므로, 공유기 포트포워딩이나 방화벽 설정이 필요 없습니다.
- 공개 호스트명 조회: `cloudflared --metrics 0.0.0.0:2000`으로 띄우면 `GET http://cloudflared:2000/quicktunnel` → `{"hostname":"brave-cat-1234.trycloudflare.com"}`
- 이 조회가 안 되면(버전 차이 등) `docker compose logs cloudflared`에서 URL을 찾아 `.env`의 `PUBLIC_URL`에 넣고 `docker compose up -d c1-gateway`를 실행하면 됩니다.

## 3. 사전 준비 (각자 노트북에서 1회)

1. **Docker Desktop** (WSL2 백엔드) 설치 후 실행
   - Settings → Resources에서 CPU 4개 이상, 메모리 6GB 이상 권장 (SAM 2를 쓸 때)
2. repo 클론
   ```bash
   git clone https://github.com/physicalai-team3/pocket-smart-factory.git
   cd pocket-smart-factory
   cp .env.example .env
   ```
3. (SAM 2를 쓸 사람만) 체크포인트 다운로드 (~150MB)
   ```bash
   bash tools/download_models.sh
   ```

## 4. 실행

```bash
# 전체 기동 (C3는 fast 모드, SAM 2 없이)
docker compose up -d --build

# 공개 URL 확인
docker compose logs cloudflared | grep trycloudflare
```

1. 노트북 브라우저에서 **`http://localhost:8080`** (대시보드)을 엽니다. 터널 URL로 열어도 됩니다.
2. 대시보드에 표시된 **QR을 폰으로 스캔** → `phone.html` → 카메라 권한 허용 → 후면 카메라 송출이 시작됩니다.
3. 다른 모니터나 창에서 **`/sim/`** (시뮬레이터)을 열고 폰으로 그 화면을 촬영합니다.

### SAM 2 켜기
```bash
# .env
WITH_SAM2=1
SAM2_ENABLED=1
docker compose up -d --build c3-inference
```

### C3 scale-out
```bash
# .env 에서 C3_REPLICAS=3, OMP_NUM_THREADS=<코어수/3> 으로 바꾼 뒤
docker compose up -d c3-inference
docker compose restart web          # nginx가 새 복제본 IP를 다시 찾도록
```
대시보드의 `c3_replicas_seen`과 fps로 before/after를 측정합니다 (발표 자료 7쪽 그래프).

### 개발 중 단일 서비스 재시작
```bash
docker compose up -d --build c4-visualizer
docker compose logs -f c4-visualizer
```

## 5. 보안 메모

- 터널 URL을 아는 사람은 누구나 접속할 수 있습니다. 세션 ID(무작위 8자)를 모르면 영상을 보내거나 볼 수 없게 C1에서 검사합니다.
- 터널에는 nginx `:80`만 연결됩니다. 내부 LB `:8090`과 각 서비스 포트는 공개되지 않습니다.
- 데모가 끝나면 `docker compose down`으로 터널을 닫습니다.

## 6. 트러블슈팅

| 증상 | 원인 / 해결 |
|---|---|
| 폰에서 카메라 권한 창이 안 뜸 | `http://`로 접속한 경우입니다. 반드시 `https://...trycloudflare.com`으로 접속하세요 (QR은 자동으로 HTTPS) |
| iOS에서 영상이 검게 나옴 | `<video playsinline muted autoplay>` 속성이 필요합니다 |
| QR이 안 뜨고 503 | 터널이 아직 기동 중입니다 (5~15초). 계속 안 되면 `PUBLIC_URL`을 수동으로 지정 |
| `ERR 429` 또는 연결 끊김 | Quick Tunnel 요청 제한입니다. 폰 fps를 낮추거나 cloudflared를 재시작 (URL이 바뀜) |
| C3 복제본 하나만 일함 | scale 후 `docker compose restart web`을 안 한 경우입니다 |
| 복제본을 늘렸는데 더 느려짐 | `OMP_NUM_THREADS`가 커서 스레드끼리 다투는 상황입니다. 코어 수 / 복제본 수로 맞추세요 |
| WS가 60초마다 끊김 | nginx `proxy_read_timeout` 확인. 폰과 대시보드는 20초마다 `{"type":"ping"}`을 보냅니다 |
| 지연이 큼 (>1초) | 폰 → Cloudflare → 노트북 왕복 구간입니다. 전송 해상도 640px, JPEG q=0.6~0.7 유지. 같은 Wi-Fi에서 `http://<노트북IP>:8080`은 카메라가 안 되므로 터널을 유지하세요 |
