# Boot Verification Audit & Brick-Resistant Custom Firmware Guide

This guide documents the verified boot chain (`uboot.img`, `vbmeta.img`, `ota_process`, and `md_upgrade`) across `V26.10.28.04` and `V26.11.19.01`, and defines the safest path for customizing Beni without bricking the robot.

---

## 1. Verified Boot & OTA Audit (`vbmeta.img`, `ota_process`, `md_upgrade`)

### 1.1 What `vbmeta.img` Actually Verifies
Parsing `vbmeta.img` (AVB 1.0, RSA-2048 `algo=1`, `flags=0x0`, `rollback_index=0`) reveals **only 3 descriptors**:
1. **Hash descriptor (Tag 2):** `boot` (`sha256`, 13.5 MB)
2. **Hashtree descriptor (Tag 1):** `system` (`sha1`)
3. **Kernel cmdline descriptor (Tag 3):** `dm-mod.create="dm-verity,,,ro,... $(SYSTEM_PART) ... restart_on_corruption"`

**Critical finding:** **`mondo_a` / `mondo_b` (1.0 GB ext4 mounted RW at `/md`) is NOT covered by `vbmeta.img` or `dm-verity`.** Neither is `hbre_a` / `hbre_b`.

### 1.2 What `ota_process` (`/usr/hobot/bin/ota_tool`) Checks
Analysis of `ota_process` (unstripped AArch64 ELF with DWARF debug info) via `rea` shows:
- **A/B Slot Management (`ota_boot_control.c`):** Uses the `misc` partition (`ab_select` in U-Boot) with `tries_remaining` and `bootctrl_mark_boot_successful` (invoked by `S99ota_update_check.sh` via `/usr/hobot/bin/ota_tool -b`).
- **Partial A/B Updates (`up_partition.c`):** `copy_unupgraded_ab_parts` automatically copies any A/B partition *not* listed in `data.json`'s `update_partition` array from the active slot to the inactive slot before switching slots.
- **Anti-Rollback (`ota_antirollback.c`):** Enforces consistency across `miniboot`, `uboot`, and `vbmeta`, and validates `boot` + `vbmeta` + `system` match (`partition boot vbmeta and system not match!`).
- **Integrity:** Validates each partition image against its MD5 sum in `data.json`.

### 1.3 What `md_upgrade` (On-Robot OTA Daemon) Checks
Inspecting `/md/bin/md_upgrade` (Rust binary) in **both `V26.10.28.04` and `V26.11.19.01`** shows:
- `md_upgrade_config.LZ110.toml` sets `enable_signature_check = true` and `signature_public_key = "/etc/md_upgrade/signing_key.pub"`, **but `/etc/md_upgrade/signing_key.pub` does not exist in `system.img`**.
- The binary itself explicitly contains unimplemented stubs:
  - `"RSA decryption not yet implemented"`
  - `"AES-GCM decryption not yet implemented"`
  - `"RSA signature verification not yet implemented"`
  - `"Plain package processed successfully"`
- Official production builds (`V26.10.28.04`, `V26.11.19.01`) ship as `*.plain.unsign` with `enable_security: false`.

> [!WARNING]
> **Do NOT blindly install future official Mondo OTA updates** once you unbox Beni. A future update could implement RSA signature verification in `md_upgrade` or bump `anti_rollback_level` in `miniboot`/`uboot`/`vbmeta`. Always download and audit new releases with `tools/fetch_ota.py` before installing them.

---

## 2. Partition Risk Matrix

| Partition(s) | Risk Level | Why |
| :--- | :--- | :--- |
| **`mondo_a` / `mondo_b`** (`/md`) | **Lowest** | Not covered by `vbmeta`/`dm-verity`; A/B slotted; contains all Mondo daemons, configs, RL policies, and `/md/etc/authorized_debug_profile` |
| `hbre_a` / `hbre_b` | Low | Not covered by `vbmeta`, A/B slotted, rarely needs changes |
| `boot_a/b`, `system_a/b`, `vbmeta_a/b` | **High** | Covered by AVB (`vbmeta` RSA-2048 signature) and `dm-verity` (`restart_on_corruption`); modifying without valid signing breaks boot on that slot (though U-Boot A/B should fall back to the other slot) |
| `miniboot`, `uboot_a/b`, `gpt` | **Critical (Hard-Brick Risk)** | Anti-rollback protected; corrupting early bootloader prevents recovery without UART/USB-boot hardware teardown |
| `private` | **Critical (Data-Loss Risk)** | Unique per-robot SN tree, factory IMU/stereo/motor calibration, and certs — **never format or overwrite** |
| MCU firmwares (`ESC`, `PMU`, `BAT`, `RC`, `A350`) | **High** | Not A/B slotted; failed flash can require physical SWD/UART/fastboot recovery |

---

## 3. Safest Custom Firmware Install Process (Step-by-Step)

### Phase 0: Pre-Flight After Unboxing (Zero Flash Risk)
1. **Stay offline initially** (do not let the official Mondo app auto-upgrade firmware before checking what version shipped from the factory).
2. Connect to Beni over Wi-Fi (`192.168.7.1`) and/or USB-C to record:
   - Exact factory firmware version, `board_id`, `batch_id`, and `mondo_partition_size`.
   - Confirm whether `md_upgrade` on the factory build matches our `V26.10.28.04` / `V26.11.19.01` findings.

### Phase 1: Minimal `mondo.img`-Only Unlock (Enabling SSH & ADB Safely)
Instead of building a full custom OS on day one, make the **smallest possible, reversible modification** to `mondo.img`:
1. Start from the exact stock `mondo.img` matching your robot's current version (or `V26.10.28.04` / `V26.11.19.01`).
2. Inside `mondo.img`:
   - **Remove `/etc/authorized_debug_profile`** — unlocks USB-C ADB (`VID=0x3a29, PID=0x0010`, root `adb shell` with no password required) and allows `S50sshd` to start.
   - **Add key-only SSH using your own public key** (since `/etc/shadow` in read-only `system.img` has a hashed `root` password and no `/root/.ssh/authorized_keys`): place your public key at `/md/etc/user_authorized_keys` and add a lightweight autostart entry in `/md/bin/` (started by `mdctl.sh`) that runs:
     ```sh
     /usr/sbin/sshd -p 2222 -o AuthorizedKeysFile=/md/etc/user_authorized_keys -o PasswordAuthentication=no -o PermitRootLogin=prohibit-password
     ```
     This gives passwordless, key-authenticated root SSH over both Wi-Fi (`192.168.7.1:2222`) and USB-C RNDIS (`192.168.123.1:2222`) without modifying `system.img`.
   - **Leave every other daemon—especially `md_upgrade` and `md_netmanager`—100% untouched** so OTA recovery and Wi-Fi AP stay fully functional.
3. Package a **minimal `0300` (`X5`) OTA ZIP** whose `data.json` sets:
   - `"update_partition": ["mondo"]` (ONLY `mondo` — do NOT include `miniboot`, `uboot`, `vbmeta`, `boot`, `system`, or any MCU modules!).
   - Updated MD5 sum for your modified `mondo.img`.
4. Upload via `http://192.168.7.1:8080/v2/upgrades/{uuid}/upload` and trigger via `/cmd/ota/upgrade_control_req`.
   - `ota_process` copies the untouched, signed `uboot`/`vbmeta`/`boot`/`system`/`hbre` to the inactive slot, flashes only your modified `mondo.img` to the inactive slot, and switches slots.
   - Your original stock slot remains **100% intact** on eMMC.

> [!IMPORTANT]
> **Watch out for one subtlety:** `S99ota_update_check.sh` lives in `system.img`, not `mondo.img`. That means even if a custom `mondo.img` fails to start Mondo daemons, `system.img` will still boot and mark the new slot successful. That is why **Phase 1 must keep `md_upgrade` and `md_netmanager` intact and enable `sshd` + `adbd` first**—once you have root shell access over SSH/ADB, you can inspect logs, back up `/private`, and switch A/B slots manually via `/usr/hobot/bin/ota_tool` at any time.

### Phase 2: Full Backup & Iterative Open-Source Development
Once SSH/ADB is live:
1. **Back up `/private`** (factory calibration, SN tree, certs) and dump the active partition table + `ubootenv`/`misc` to your Mac before changing anything else.
2. Develop and test custom open-source services (e.g., custom Zenoh/ROS 2 bridge, custom RL policies in `/md/etc/onnx/p0/`, or replacing `md_control`/`md_gateway`) live over SSH/ADB or by toggling entries in `/md/etc/.config/md_app_autostart.conf`—no reflashing required for day-to-day iteration!
3. If anything ever goes wrong, you can either revert your changes over SSH/ADB, switch back to the untouched stock A/B slot, or re-flash the archived stock `V26.10.28.04` / `V26.11.19.01` ZIP.
