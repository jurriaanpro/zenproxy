import argparse
from pathlib import Path

import uvicorn

from zenproxy.api import create_app
from zenproxy.config import load_config


def main() -> None:
    parser = argparse.ArgumentParser(prog="zenproxy")
    parser.add_argument(
        "--config", type=Path, default=Path("config.yaml"), help="Path to config.yaml"
    )
    args = parser.parse_args()

    config = load_config(args.config)
    app = create_app(config)
    uvicorn.run(app, host=config.server.host, port=config.server.port)
