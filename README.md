# open_ineb

Community reverse-engineering research and open-source companion tooling for the **Mondo Robotics Beni** (`LZ100`) camera robot.

> **Disclaimer:** This repository is an independent, unofficial interoperability and research project by hardware owners. It does not distribute proprietary firmware, APKs, or copyrighted manuals.

> **⚠️ Caution Before Accepting Official OTA Updates:**
> As of `V26.10.28.04` and `V26.11.19.01`, `md_upgrade` does not yet enforce RSA package signatures, and `vbmeta.img` (AVB 1.0) only covers `boot` and `system` (not `mondo.img`). Future official OTA updates from Mondo Robotics **could** close these doors—for example, by enabling RSA signature verification in `md_upgrade`, extending verified boot / integrity checks to `mondo.img`, locking down the local HTTP/Zenoh OTA endpoints, or removing debug/autostart hooks (`authorized_debug_profile` / `md_app_autostart.conf`).
>
> If you want to keep your Beni customizable, **think twice before accepting automatic OTA updates**, archive a backup of your current firmware first (`tools/fetch_ota.py`), and inspect new releases before flashing them. Also note that `customize_fw.py` has been verified offline and via loopback tests, and has not yet been tested on live hardware—use at your own risk!

## Goals

1. Document Beni's local communication protocols (BLE provisioning, local Wi-Fi HTTP APIs, UCP transport, and Zenoh-over-UCP Protobuf topics).
2. Build an open-source companion app / CLI controller for local control, telemetry, media download, and video streaming without cloud lock-in.
3. Investigate firmware packaging, update verification, and hardware interfaces.

## Documentation & Tools

- [Hardware Reference](docs/hardware.md) — SoCs (Horizon X5 + A3), cameras & optics (`OV50H` 1/1.3" 155° f/2.8 main + `SC035HGS` stereo pair), MCUs, buses, and community teardown wish list.
- [System & Protocol Architecture](docs/architecture.md) — Reverse-engineering findings covering BLE `ProtocolV0`, local HTTP endpoints (`192.168.7.1:8010` / `:8080`), UCP + Eclipse Zenoh (`zenoh-pico`), and OTA update architecture.
- [Firmware & Hardware Architecture](docs/firmware_architecture.md) — Complete dual-SoC (`X5` + `A3`) and multi-MCU firmware breakdown from `V26.10.28.04`.
- [X5 Linux OS, Services, AI Models & Hardware Buses](docs/x5_os_and_services.md) — On-robot daemons, Iceoryx2/FastDDS + cross-chip bus, USB-C gadget/SSH/ADB config, RL locomotion policies, and UART/I2C bus map.
- [Boot Verification Audit & Brick-Resistant Custom Firmware Guide](docs/safe_custom_firmware_guide.md) — `vbmeta.img`, `ota_process`, and `md_upgrade` audit plus step-by-step safe A/B `mondo.img` customization plan.
- [UCP + Zenoh-over-UCP & Local OTA Protocol](docs/ucp_zenoh_protocol.md) — Complete byte-level specification of UCP over UDP (`:9007`), `mdZenohOverUcp` (`0xa4`–`0xa7`), Zenoh 1.x Protobuf OTA control, and TUS 1.0.0 resumable upload (`:8080`).
- [Firmware OTA API Specification](docs/ota_api.md) — Cloud OTA API endpoints and JSON schemas recovered from `libapp.so`.
- [Firmware OTA Archiver (`tools/fetch_ota.py`)](tools/fetch_ota.py) — Standalone `uv`-runnable CLI to log in with your own Mondo account, query firmware metadata, and archive a verified local backup copy of official firmware packages.
- [Custom Firmware TUI Builder & Standalone OTA Uploader (`tools/customize_fw.py`)](tools/customize_fw.py) — Interactive TUI (`rich` + `questionary`) to build a brick-resistant, `mondo.img`-only A/B OTA package (optional root password, SSH authorized key, generated Beni Ed25519 host/client keys, USB-C ADB unlock) and upload/install it **100% standalone** over UCP + Zenoh + TUS 1.0.0 without the official Mondo app.

## Status

- [x] Initial APK & transport stack identification (`LZ100`, Flutter AOT + `libmondo_native.so`)
- [x] Zenoh `/cmd/*` and `/status/*` topic inventory + all 434 `.proto` schemas extracted
- [x] BLE UUIDs (`0000FFF0..FFF3`, `0100FFF0..FFF3`) & local HTTP endpoint inventory
- [x] Full Dart AOT (`libapp.so`) & native (`libmondo_native.so`) protocol schema recovery
- [x] Firmware package container & X5/A3 OS inspection (`V26.10.28.04`, `V26.11.19.01`, `sisyphus` debug)
- [x] Standalone UCP + Zenoh-over-UCP + TUS 1.0.0 OTA controller (`tools/ucp_bridge.py`, `tools/ota_client.py`, verified via `uv run tests/test_standalone_ota.py`)
