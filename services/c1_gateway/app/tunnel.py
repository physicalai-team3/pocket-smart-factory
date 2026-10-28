"""공개 URL 확인: PUBLIC_URL 환경변수 → 없으면 cloudflared 의 /quicktunnel 을 주기적으로 조회.

/quicktunnel 은 cloudflared 소스(metrics/metrics.go)에는 있지만 공식 문서에는 없는 경로다.
응답: {"hostname": "brave-cat-1234.trycloudflare.com"}. 조회가 안 되면 PUBLIC_URL 을 수동으로 넣는다.
"""
from __future__ import annotations

from typing import Optional

import httpx


class Tunnel:
    def __init__(self, env_url: Optional[str], metrics_url: str):
        self.env_url = env_url.rstrip("/") if env_url else None
        self.metrics_url = metrics_url.rstrip("/")
        self.discovered: Optional[str] = None
        self.ok = False

    @property
    def public_url(self) -> Optional[str]:
        return self.env_url or self.discovered

    @property
    def source(self) -> str:
        return "env" if self.env_url else ("quicktunnel" if self.discovered else "none")

    async def refresh(self, client: httpx.AsyncClient) -> bool:
        """조회해서 URL 이 바뀌었으면 True."""
        if self.env_url:
            self.ok = True
            return False
        try:
            r = await client.get(f"{self.metrics_url}/quicktunnel", timeout=2.0)
            host = r.json().get("hostname") if r.status_code == 200 else None
        except (httpx.HTTPError, ValueError):
            host = None
        self.ok = bool(host)
        url = f"https://{host}" if host else None
        if url and url != self.discovered:
            self.discovered = url
            return True
        return False
