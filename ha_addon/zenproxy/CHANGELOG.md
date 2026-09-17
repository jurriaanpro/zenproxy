# Changelog

Format loosely based on [Keep a Changelog](https://keepachangelog.com/).
Versions track `version` in [`config.yaml`](config.yaml) (the Home Assistant
addon version) — see `AGENTS.md` at the repo root for the update rule.

## [Unreleased]

## [0.8.0] - 2026-09-17

- Add `leader_rotation` config to spread charge/discharge wear across
  devices instead of draining one pack to its floor before handing off.

## [0.7.0] - 2026-09-10

- Add optional MQTT discovery of per-device sensors in Home Assistant
  (`mqtt_enabled`, auto-detects the Supervisor `mqtt` service).

## [0.6.0] - 2026-08-12

- Add `virtual_product` config option.

## [0.5.0] - 2026-08-01

- Split `chargeMaxLimit`/`inverseMaxPower` evenly across devices instead of
  broadcasting the total (fixes a doubled aggregated read-back).

## [0.4.0] - 2026-07-26

- Route full power to the sole eligible device once its sibling is excluded.

## [0.3.0] - 2026-07-19

- Keep charge/discharge priority sticky instead of re-ranking on every write.

## [0.2.0] - 2026-07-18

- Evenly divide charge/discharge splits once a request is large enough for
  multiple devices.

## [0.1.0] - 2026-07-18

- Initial Home Assistant addon release (amd64).
