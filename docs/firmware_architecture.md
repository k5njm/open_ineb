# Beni (`LZ110` / `LZ100`) Firmware & Hardware Architecture

Documented from inspecting the official `V26.10.28.04` firmware archive (`lz110`, `board_id=0x3001`, `batch_id=lz100-PRE-20260601-001`, `1GB`, `enable_security: false`).

## 1. Firmware Package Layout

The OTA ZIP contains a TOML manifest (`lz110_0000_*.mass.cfg.plain.unsign`) and 19 unencrypted, unsigned module payloads (`*.mass.fw.plain.unsign`). `V26.11.19.01` adds a 20th (`0900`, Treat Launcher):

| Part ID | `device_type` | Size | Format | Role |
| :--- | :--- | ---: | :--- | :--- |
| `0000` | Manifest | 3.5 KB | TOML | Package version, module SHA-256 checksums, concurrency strategy |
| `0300` | **`X5`** | 624 MB | ZIP | **Primary Compute SoC** — Horizon/D-Robotics X5 (ARM64 Linux 6.1.83, eMMC A/B) |
| `0301` | **`A3`** | 328 MB | ZIP | **Secondary / Camera SoC** — `lz110_a350` (Linux + DSP, SPI-NAND UBI, flashed over X5) |
| `0800` | **`DONGLE`** | 85 MB | RAR5 | **SIMCom SIM7600G-M11** LTE modem firmware (`LE20B01V07SIM7600G-M11_OTA_CUS_MDJQR/`: `sbl1.mbn`, `boot.img`, `system.img`, …) |
| `0200`–`0202`, `023F` | **`ESC`** | 33 KB × 4 | Raw ARM Cortex-M | Motor / joint ESC controllers |
| `0500` | **`CHARGER`** | 43 KB | Raw ARM Cortex-M | Charging Hub / charger MCU |
| `0503` | **`CB`** | 149 KB | Raw MCU | Control Board MCU |
| `0504` | **`IP2369`** | 27 KB | Raw MCU | Injoinic IP2369 4S USB-C PD 45 W charger controller |
| `0701`–`0731` | **`RC-UWB`** | 151 KB × 4 | Raw MKSemi UWB SoC | Motion Tracker UWB SoC (ranging/AoA, joystick ADC, IMU, UWB data link) — see [hardware §4.1](hardware.md#41-motion-tracker-uwb-link) |
| `0702`–`0732` | **`RC-BTN`** | 16 KB × 4 | Raw Cortex-M0 (`0x08000000`, ~3 KB SRAM) | Motion Tracker helper MCU (buttons/power/vibration), UART-bridged to `07x1` |
| `0900` | **`TL`** | 142 KB | Raw MKSemi UWB SoC | UWB Treat Launcher accessory (`UWB Treat Launcher run!`); **new in `V26.11.19.01`** |
| `0A00` | **`BAT`** | 38 KB | Raw STM32 (`0x08000000`) | Smart 4S battery BMS MCU |

---

## 2. Primary Compute SoC (`0300` — Horizon X5)

- **OS:** Linux `6.1.83` (`LNX6.1.83_PL5.1_V1.1.1`)
- **Storage:** eMMC with full **A/B seamless OTA partitioning** (`gpt.conf`):
  - `miniboot` / `miniboot_bak1` (2.3 MB)
  - `uboot_a` / `uboot_b` (2.0 MB) + `ubootenv`
  - `vbmeta_a` / `vbmeta_b` (16 KB, Android Verified Boot metadata)
  - `boot_a` / `boot_b` (15 MB, kernel + DTB)
  - `system_a` / `system_b` (250 MB, base Linux rootfs)
  - `hbre_a` / `hbre_b` (150 MB, Horizon BPU runtime)
  - **`mondo_a` / `mondo_b` (1.0 GB)** — Mondo robotics application stack
  - `private` (20 MB), `internal` (~17 GB user media), `blackbox` (~52 MB logs)
- **On-device Updater:** Includes `ota_process` (274 KB ELF) and `data.json` with MD5 sums and `have_anti_ver: true` flags.

---

## 3. Secondary / Vision SoC (`0301` — `lz110_a350`)

- **Layout:** `common-plus-images-nand-v4` supporting both 256 MB and 512 MB SPI-NAND flash (`nand-256`, `nand-512`)
- **Boot Chain:** SPL → `preboot.itb` (ATF, U-Boot, OP-TEE, SCP) → `boot.itb` + `lpddr5_fw.bin` + `dsp_idram_sram_fw_0.bin`
- **UBI Volumes:** `rootfs.ubi`, `system.ubi`, `md.ubi`, `userdata.ubi`
- **Security Profiles:** Manifest defines both `non-secure` (`preboot.itb`) and `secure-soc` (`preboot_sign.itb`, `*.pro.enc`) profiles.
- **Flashing:** Managed from the X5 host via `flashall_spinand_over_x5.sh`.
