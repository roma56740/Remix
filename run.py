from __future__ import annotations

import os

import uvicorn

from config import HOST, PORT


if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host=HOST,
        port=PORT,
        reload=os.getenv("APP_RELOAD", "false").strip().lower() in {"1", "true", "yes", "on"},
        workers=1,
        proxy_headers=True,
        forwarded_allow_ips="*",
    )
