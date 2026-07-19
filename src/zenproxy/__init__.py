import argparse
import logging
from pathlib import Path

import uvicorn

from zenproxy.api import create_app
from zenproxy.config import load_config


class _SuppressReportPollLogs(logging.Filter):
    """Drop uvicorn access-log lines for successful report polls.

    Automations poll GET /properties/report every few seconds, so logging
    each 200 OK just drowns out everything else. Anything else (writes,
    errors, other paths) still gets logged.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if not record.args or len(record.args) < 5:
            return True
        _client_addr, method, path, _http_version, status = record.args
        is_report_poll = method == "GET" and str(path).startswith("/properties/report")
        return not (is_report_poll and status == 200)


def main() -> None:
    parser = argparse.ArgumentParser(prog="zenproxy")
    parser.add_argument(
        "--config", type=Path, default=Path("config.yaml"), help="Path to config.yaml"
    )
    args = parser.parse_args()

    config = load_config(args.config)
    app = create_app(config)
    logging.getLogger("uvicorn.access").addFilter(_SuppressReportPollLogs())
    uvicorn.run(app, host=config.server.host, port=config.server.port)
