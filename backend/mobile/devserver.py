"""Development only: the /mobile/v1 routes alone, for the phone to reach the PC.

    MOBILE_TOKEN=<token> python -m backend.mobile.devserver     (port 8791)

No Docker, Qdrant, Discord or model loop: just the router from routes.py (and .env for
the speech key), so the M0 probe can be tried on the phone over the home Wi-Fi before
anything is deployed. It listens on all interfaces, so it refuses to start without a
token of 32+ characters.
The real deployment is the backend itself behind `tailscale serve` (DEPLOY.md).
"""
from __future__ import annotations

import logging
import os
import sys

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware

from backend.mobile import routes


def create_app(origins: tuple[str, ...] = ()) -> FastAPI:
    app = FastAPI(title="JARVIS mobile (dev)")
    app.add_middleware(GZipMiddleware, minimum_size=1000)
    if origins:
        app.add_middleware(CORSMiddleware, allow_origins=list(origins), allow_methods=["*"],
                           allow_headers=["*"], expose_headers=["ETag"])
    app.include_router(routes.router)
    return app


def main() -> None:
    load_dotenv(override=False)          # GOOGLE_TTS_API_KEY etc.; the shell environment wins
    token = os.getenv("MOBILE_TOKEN", "").strip()
    if len(token) < 32:
        sys.exit("Set MOBILE_TOKEN to at least 32 characters first.")
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    origins = tuple(o.strip() for o in os.getenv("MOBILE_DEV_ORIGINS", "").split(",") if o.strip())
    app = create_app(origins)
    port = int(os.getenv("MOBILE_DEV_PORT", "8791"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")


if __name__ == "__main__":
    main()
