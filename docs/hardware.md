# Beni (`LZ110` / `LZ100`) Hardware Reference

Community hardware reference compiled from official firmware analysis (`V26.10.28.04` / `V26.11.19.01`), calibration files, and user manual specifications. Items marked **TBD (Teardown Needed)** require physical measurements once units are opened.

---

## 1. Compute & Storage

| Subsystem | Chip / Part | Details |
| :--- | :--- | :--- |
| **Primary Compute SoC (`0300`)** | **Horizon / D-Robotics Sunrise X5** | Octa-core ARM Cortex-A55 + 10 TOPS BPU, ARM64 embedded Linux `6.1.83` (same SoC family as the [RDK-X5](https://www.waveshare.com/wiki/RDK-X5)) |
| **Primary Storage** | eMMC (~32 GB class) | Full A/B OTA layout (`boot`, `system`, `hbre`, `mondo` 1 GB × 2, plus ~17 GB `internal` media partition) |
| **Secondary / Camera SoC (`0301`)** | **`lz110_a350` (A3)** | Embedded Linux + DSP + LPDDR5; 256 MB / 512 MB SPI-NAND (UBI); flashed over X5 via fastboot bridge |
| **USB-C Port** | USB Gadget (`0x3a29:0x0010`) | Exposes Mass Storage (MSD), RNDIS Ethernet (`192.168.123.1`), and ADB (`adbd`, gated by `/md/etc/authorized_debug_profile`) |

---

## 2. Cameras & Optics

### Main Camera (Head)
- **Image Sensor:** **OmniVision OV50H** — **1/1.3-inch 50 MP CMOS** (driven by the A3 camera SoC `0301`; same optical format class as DJI Osmo Action 4 / Action 5 Pro / O4 Air Unit Pro).
  - *Note:* X5 `mondo.img` also contains legacy/secondary **SmartSens SC532HS** (`mipi_rx: 0`, `i2c_bus: 4`) profiles for 4K60 / 4K30 DCG-HDR (`v2_sc532hs_*.json`).
- **Lens Specifications:** **155° FOV**, **f/2.8**, fixed-focus ultra-wide, shutter speed `1/8000 s` to frame-rate limit.
- **Physical Construction & Repair Notes:**
  - The curved front optical element sits **exposed** inside a recessed rectangular black bezel (there is **no flat protective cover glass** in front of it).
  - **Lens Thread / Mount:** **TBD (Teardown Needed)** — Not stored in firmware. Needs caliper measurement to confirm whether the barrel is threaded (**M12×0.5**, **M14**, or **M16**) or UV-glued Active Alignment (AA), plus front-element OD, total barrel height, and 650 nm IR-cut filter placement.
  - **Likely Replacement Candidates (if threaded):** 1/1.3"-image-circle, 155° FOV, f/2.8 (~2.7–2.9 mm EFL) action-cam/FPV lenses (e.g. DJI O4 Air Unit Pro / Osmo Action 4/5 replacement lens barrels). Avoid standard 1/2.8" or 1/1.8" M12 lenses, which will heavily vignette on a 1/1.3" sensor.
  - **EIS / GDC Calibration:** Hardware distortion correction and EIS (`eis_config.json`, `distortion_mode: 3`) are tuned to the stock lens curve.

### Front Obstacle-Avoidance Stereo Cameras (Lower Front Body, ×2)
- **Image Sensors:** **2× SmartSens SC035HGS** — `640×480` global-shutter stereo pair (`1/6"` format).
- **Optics & Baseline:** Wide-angle fisheye (`equidistant` distortion model in `camchain-imucam.yaml`), **~60 mm** stereo baseline (`T_cn_cnm1` tx = `-0.06007 m`), factory-calibrated against the IMU via Kalibr (`timeshift_cam_imu ≈ 4.45 ms`).
- **Repair Note:** Because stereo depth and visual-inertial odometry depend on factory intrinsics/extrinsics, replacing a stereo lens barrel requires refocusing and recalibration.

---

## 3. Microcontrollers, Actuators & Buses

| Module | Part ID | Bus / Interface | Notes |
| :--- | :--- | :--- | :--- |
| **Control Board (CB / PMU)** | `0x0503` | UART `/dev/ttyS6` (`460800`) | Central power & chassis MCU |
| **Left Leg ESCs (Hip / Knee / Wheel)** | `0x0200` / `0x0201` / `0x0202` | UART `/dev/ttyS3` (`3000000`) | 3× ARM Cortex-M motor ESCs |
| **Right Leg ESCs (Hip / Knee / Wheel)** | `0x0200` / `0x0201` / `0x0202` | UART `/dev/ttyS2` (`3000000`) | 3× ARM Cortex-M motor ESCs |
| **Gimbal ESC** | `0x023F` | UART `/dev/ttyS4` (`3000000`) | ARM Cortex-M camera tilt/gimbal ESC |
| **Battery BMS (`NB401`)** | `0x0A00` | UART→I2C `0x16` via `/dev/ttyS6` | STM32 (`0x08000000`), 4S smart battery |
| **USB-C PD Charger Controller** | `0x0504` | I2C/PMU | **Injoinic IP2369** 4S 45 W USB-C PD controller |
| **Charging Hub MCU** | `0x0500` | External / Dock | ARM Cortex-M charging hub controller |
| **Motion Tracker RC (×4)** | `0x0701..0x0732` | UART `/dev/ttyS7` (`921600`) | UWB tracker (`07x1`) + STM32 button MCU (`07x2`) |
| **Wireless Dongle** | `0x0800` | External | Companion wireless dongle (85 MB firmware) |

---

## 4. Radios & Connectivity

- **Wi-Fi:** Dual-mode AP (`192.168.7.1`, `dnsmasq`) and Station mode.
- **BLE:** Provisioning & status GATT service (`0000FFF0..FFF3`, `0100FFF0..FFF3`).
- **UWB:** Ultra-wideband localization paired with the Motion Tracker remote (`uwb_robot.bin`, `md_uwb_loc_config.json`).
- **Audio:** Speaker + microphone array (BlueALSA / `md_audio` pipeline).

---

## 5. Community Teardown Wish List (PRs Welcome!)

- [ ] Main camera (`OV50H`) lens mount type (threaded M12/M14/M16 vs. UV-glued AA), thread pitch, front-element OD, and barrel height
- [ ] Main camera FPC connector pinout & PCB markings
- [ ] Stereo camera (`SC035HGS`) lens thread & holder dimensions
- [ ] X5 carrier board UART console / JTAG / SWD test-pad locations
- [ ] Exact Wi-Fi / BLE / UWB chipset part numbers from PCB silkscreen
