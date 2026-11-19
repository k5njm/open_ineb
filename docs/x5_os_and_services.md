# X5 Linux OS, Services, AI Models & Hardware Buses

Findings from inspecting `system.img` and `mondo.img` from the `0300` (`X5`) firmware module (`V26.10.28.04`, verified against `V26.11.19.01`).

---

## 1. Filesystem & Mount Layout (`/etc/hb-fstab`, `S65mountall`)

| Partition | Mount Point | FS | Options | AVB / Verity | Role |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `system_a/b` | `/` | ext4 | `ro` (`dm-verity`) | **Yes** (`vbmeta.img`) | Base Linux 6.1.83 rootfs, BusyBox/SysV init, `sshd`, `adbd`, `ota_tool` |
| `hbre_a/b` | `/usr/hobot` | ext4 | `ro,noatime` | No | Horizon Robotics BPU runtime & libraries |
| `mondo_a/b` | `/md` | ext4 | `rw` (`e2fsck` on boot) | **No** | All Mondo robotics daemons (`/md/bin`), configs (`/md/etc`), libs, AI models |
| `private` | `/private` | ext4 | `rw` (`e2fsck` on boot) | No | **Critical — never wipe!** Device SN tree (`lz100_sn_tree.json`), factory calibration, certs, `version.toml` |
| `blackbox` | `/blackbox` | ext4 | `rw` (auto-reformatted on err) | No | Runtime logs, OTA staging (`/blackbox/firmware`), RL rollouts (`/blackbox/real_rl_spool`) |
| `internal` | `/mnt/internal` | exFAT | `rw` | No | User media storage (~17 GB) |
| microSD | `/mnt/sdcard` | exFAT | `rw` | No | Removable media storage (up to 1 TB) |

---

## 2. Boot & Init Sequence (`/etc/init.d/`)

1. **`S50sshd` / `S50telnet`:**
   - `S50telnet` is disabled (`exit 0`).
   - `sshd` (`/usr/sbin/sshd`) is installed with `PermitRootLogin yes`, `PasswordAuthentication yes`, `AuthorizedKeysFile .ssh/authorized_keys`.
   - However, `S50sshd` checks `[ -f /md/etc/authorized_debug_profile ]`. Because `/md/etc/authorized_debug_profile` exists (contents: `auth_only_service`), `S50sshd` leaves `sshd` closed at boot and delegates debug access to `md_sys` / `md_debug_authd`.
2. **`S65mountall`:** Runs `e2fsck` on `private` and `mondo`, then mounts `/usr/hobot`, `/md`, `/private`, `/blackbox`.
3. **`S99md_adb_guard.sh`:** Prepares USB configfs (`libcomposite`) for `md_sys`. Like `S50sshd`, legacy fallback ADB recovery is disabled when `/md/etc/authorized_debug_profile` is present.
4. **`S99md_app_startup.sh`:** Uses `/usr/bin/mdctl.sh` in `parallel` mode to launch all 18 services listed in `/etc/init.d/md_app_autostart.conf`.
5. **`S99ota_update_check.sh`:** Runs `/usr/hobot/bin/ota_tool -b` in the background to mark the current A/B slot as booted successfully in `misc`.

---

## 3. On-Robot Services & IPC Architecture

### Autostarted Services (`md_app_autostart.conf`)
`md_sys`, `md_relay`, `md_hmsd`, `md_blackbox`, `md_analytics`, `md_netmanager`, `md_videotrans`, `md_camera`, `md_audio`, `md_control`, `md_pnl`, `md_function_v2`, `md_media_server`, `md_pose_estimate`, `md_aruco_localization`, `md_gateway`, `md_upgrade`, `md_snapback`.

### Internal & Cross-Chip Bus
- **Intra-X5 IPC:** Shared-memory **Iceoryx2** / **FastDDS** (`ring://...`, configured in `/md/etc/*_dds_topic_config.json` and `/md/etc/fastdds_shm_profile.xml`).
- **Cross-Chip Bus (X5 ↔ A350):** `xchip://` routes defined in `/md/etc/md_bus_routing.json` (`origin=1` for X5, `origin=2` for A350), carrying H.265 camera frames (`/data/image/head_front_encoded`), audio commands, and media recording events.
- **External App Interface:**
  - `md_videotrans`: Handles UCP + **Zenoh-over-UCP** (`ZENOHVT`) and H.265 streaming (Agora backend disabled by default: `agora backend disabled (enable=0), use ucp`). Contains **embedded Protobuf descriptors** (`md/protocol/...proto`) for all Zenoh command and status messages.
  - `md_gateway`: Cloud MQTT (`emqx`) + local service routing (`/md/etc/md_gateway.json`).
  - **Local HTTP/HTTPS Ports:**
    - `:8010` — Local config service
    - `:8080` — `md_blackbox` logs & `md_upgrade` OTA upload (`/v2/upgrades/{id}/upload`)
    - `:8083` — `param_center` (`/private/param_center/`, HTTPS via TEE)
    - `:8084` — `md_media_server` (`/media/files/{id}/content`, SQLite DB at `/mnt/internal/.md_media_server/media.db`)

---

## 4. Networking & USB-C Gadget Config

- **Wi-Fi AP (`wlan1`, `/md/etc/dnsmasq.conf`):** `192.168.7.1/24` (DHCP `192.168.7.2`–`254`)
- **USB-C Gadget (`35300000.usb`, `/md/etc/md_usb_gadget.json`):**
  - VID/PID on `LZ110` (`board_id=0x3001`): `0x3a29:0x0010` (`Mondo BeniLite ADB MSD RNDIS (USB2.0)`)
  - **RNDIS (`usb0`):** `192.168.123.1/24` (DHCP `192.168.123.2`)
  - **MSD:** Exports `/dev/block/platform/by-name/internal` and `/dev/mmcblk1p1` (microSD)
  - **ADB:** `/usr/bin/adbd` (gated by `md_sys` when `/md/etc/authorized_debug_profile` exists)
- **Secondary USB (`35100000.usb`):** `0x3a29:0x0021`, bulk transport (`/md/bin/md_usb_device`).

---

## 5. RL Locomotion & Vision AI Models

Configured in `/md/etc/md_control/pnc_config_p0.json` using **MNN** (`loco_engine`, `nav_engine`, `vae_engine`) and **ONNX Runtime** / **Horizon BPU (`.bin`/`.hbm`)**:
- **Locomotion & Skill Policies (`/md/etc/onnx/p0/`):** `rnn_3in1`, `rnn_flat`, `rnn_standing`, `rnn_recover`, `rnn_jump_stair`, `rnn_loco_nav`, `rnn_loco_for_edge_protection`, `real_rl_loco`, `jump_auto`, `jump_front`, `jump_back`, `jump_left`, `jump_right_big_wheel`, `edge_protection`, `moe`, `skill_gate`, `nav_diff`, `nav_omni`, `pointnav`, `tracking` (`policy_tk_v0..v3`), `vae`.
- **Vision & Audio Models (`/md/etc/models/` & `/md/etc/`):** `BeniFocus275.onnx/.mnn`, `YOLOv11n_hands_*_x5_nv12.bin`, `dogpose_B3_qat_x5_*`, `dogreid_*`, `stereo_resunet_480x640.bin`, `osnet_*`, `lightfc_*`, `v333`/`v873` audio router/suppressor models.

---

## 6. Hardware Bus Map (`md_upgrade_config.LZ110.toml`)

| Device | Host ID | Interface | Port / Baud | Protocol / Method |
| :--- | :--- | :--- | :--- | :--- |
| **X5Core** | `0x0300` | Local | — | `ota_tool` (`/usr/hobot/bin/ota_tool`) |
| **A350 (Vision SoC)** | `0x0301` | USB/GPIO | `adb` + `fastboot` | `a3` (GPIO reset → fastboot flash over X5) |
| **PMU** | `0x0500` | UART | `/dev/ttyS6` @ `460800` | `v1` serial DFU |
| **BatteryOS (NB401)** | `0x0A00` | UART→I2C `0x16` | `/dev/ttyS6` @ `460800` | `battery` (I2C OTA `0x90`–`0x95` via PMU) |
| **Motion Tracker RC (×4)** | `0x0701..0x0732` | UART | `/dev/ttyS7` @ `921600` | `v1` serial DFU (`07x2` MCU before `07x1` UWB) |
| **Left Leg ESCs (Hip/Knee/Wheel)** | `0x0200/01/02` | UART | `/dev/ttyS3` @ `3000000` | `v1` serial DFU |
| **Right Leg ESCs (Hip/Knee/Wheel)** | `0x0200/01/02` | UART | `/dev/ttyS2` @ `3000000` | `v1` serial DFU |
| **Gimbal ESC** | `0x023F` | UART | `/dev/ttyS4` @ `3000000` | `v1` serial DFU |
