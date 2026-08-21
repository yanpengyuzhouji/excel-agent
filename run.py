"""Excel Workbook Service launcher.

Run from the project root:
    python run.py
or
    uvicorn app.main:app --host 127.0.0.1 --port 8090
"""

import os

import uvicorn

from app.config import load_config


def main() -> None:
    cfg = load_config()
    server = cfg.get("server", {})
    host = server.get("host", "127.0.0.1")
    port = int(server.get("port", 8090))
    os.environ.setdefault("EXCEL_SERVICE_HOST", host)
    uvicorn.run(
        "app.main:app",
        host=host,
        port=port,
        reload=False,
        timeout_keep_alive=600,
    )


if __name__ == "__main__":
    main()
