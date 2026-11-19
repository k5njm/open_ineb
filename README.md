# open_ineb

Community reverse-engineering research and open-source companion tooling for the **Mondo Robotics Beni** (`LZ100`) camera robot.

> **Disclaimer:** This repository is an independent, unofficial interoperability and research project by hardware owners. It does not distribute proprietary firmware, APKs, or copyrighted manuals.

## Goals

1. Document Beni's local communication protocols (BLE provisioning, local Wi-Fi HTTP APIs, UCP transport, and Zenoh-over-UCP Protobuf topics).
2. Build an open-source companion app / CLI controller for local control, telemetry, media download, and video streaming without cloud lock-in.
3. Investigate firmware packaging, update verification, and hardware interfaces.

## Documentation

- [System & Protocol Architecture](docs/architecture.md) — Current reverse-engineering findings covering BLE `ProtocolV0`, local HTTP endpoints (`192.168.7.1:8010` / `:8080`), UCP + Eclipse Zenoh (`zenoh-pico`), and OTA update architecture.

## Status

- [x] Initial APK & transport stack identification (`LZ100`, Flutter AOT + `libmondo_native.so`)
- [x] Zenoh `/cmd/*` and `/status/*` topic inventory
- [x] BLE UUIDs (`0000FFF0..FFF3`, `0100FFF0..FFF3`) & local HTTP endpoint inventory
- [ ] Full Dart AOT (`libapp.so`) & native (`libmondo_native.so`) protocol schema recovery
- [ ] Firmware package container inspection
- [ ] Minimal open-source local controller PoC
