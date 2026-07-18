# zenproxy

Proxy that unifies one or more Zendure home battery devices behind a single
virtual device, using the same local HTTP API shape as a real Zendure device
(`GET /properties/report`, `POST /properties/write`).

## Usage

```bash
mise install
uv run zenproxy --config config.yaml
```

Copy [`config.example.yaml`](config.example.yaml) to `config.yaml` and fill in
the host of each real device. Serial numbers are learned automatically from
each device's `/properties/report` response — no need to specify them.

## Development

```bash
uv run ruff check .
uv run mypy .
uv run pytest
```
