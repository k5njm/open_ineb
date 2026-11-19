# `open_ineb` Firmware Config Patches

> **⚠️ Experimental / Not Yet Tested on Live Hardware:**
> These patches modify JSON configuration files inside `mondo.img` (`/md/etc/...`) via `tools/customize_fw.py`. They are **strictly more conservative** than stock (never raising speed, torque, or acceleration limits), but have not yet been validated on a physical Beni. Always test at walking speed in an open area first.

## Distribution Model: Declarative Patches + Local OTA

`open_ineb` **never distributes proprietary firmware binaries**. Instead, community improvements are distributed as small, auditable JSON patch files in `patches/`:

1. Archive your own stock firmware ZIP with `uv run tools/fetch_ota.py`.
2. Apply one or more patches with `tools/customize_fw.py` (either via the interactive TUI or `--patch`):
   ```bash
   uv run tools/customize_fw.py \
     --non-interactive \
     -i /path/to/stock_firmware.zip \
     -o /path/to/beni_custom_safe.zip \
     --safety-preset
   ```
   Or combine multiple patches:
   ```bash
   uv run tools/customize_fw.py \
     --non-interactive \
     -i /path/to/stock_firmware.zip \
     -o /path/to/beni_custom_safe.zip \
     --patch patches/safety_conservative_oa.json \
     --patch patches/safety_no_autojump.json
   ```
3. Flash the resulting `mondo.img`-only A/B OTA package over local Wi-Fi (`--upload` / `--upload-only`).

*(Why OTA instead of WebSerial? Beni doesn't expose a USB serial bootloader—its X5 SoC updates natively over Wi-Fi UCP + Zenoh + TUS 1.0.0 HTTP, which is safer, faster, and A/B brick-resistant.)*

---

## Available Patches

| Patch File | Target | What It Changes |
| :--- | :--- | :--- |
| [`safety_conservative_oa.json`](safety_conservative_oa.json) | `md_function_v2_config_p0.json` | Enables `direction_preserving_velocity_safety` (`force_stop_distance_m: 0.55`) & `stationary_target` (`max_decel_mps2: 2.5`); widens `query_half_width_m` `0.03 → 0.12` m (24 cm vs Beni's 20.7 cm URDF width); tightens stale-depth limits (`max_snapshot_age_ms: 500 → 150`, `hard_age_ms: 500 → 180`); boosts stale-depth braking (`1.0 → 2.5 m/s²`); caps `nav_vx_scale` `5.0 → 3.0 m/s` to fit within the 2.0 m stereo depth range. |
| [`safety_no_autojump.json`](safety_no_autojump.json) | `md_function_v2_config_p0.json` | Disables `jump_auto` (`enabled: false`) so Beni stops/avoids rather than attempting to jump over low obstacles/curbs. |
