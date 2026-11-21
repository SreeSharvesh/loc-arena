"""Connect the independent viewer to LOC-Arena's existing isolated analysis runtime."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import uvicorn
from run_insights.server import ServerSettings, create_app


def main() -> None:
    """Serve runs and execute analysis through the reader's recorded model gateway."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    parser.add_argument("--config", type=Path, default=Path("configs/aurora-efficiency.yaml"))
    parser.add_argument("--port", type=int, default=ServerSettings().port)
    args = parser.parse_args()
    settings = ServerSettings(port=args.port)
    command = (sys.executable, "-m", "loc_arena.insights", "--config", str(args.config.resolve()), "--force")
    print(f"Run insights: http://127.0.0.1:{settings.port}")
    uvicorn.run(create_app(args.path, command, settings), host="127.0.0.1", port=settings.port)


if __name__ == "__main__":
    main()
