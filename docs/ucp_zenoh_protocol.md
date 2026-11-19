# UCP + Zenoh-over-UCP & Local OTA Wire Protocol

This document specifies the exact local wire protocol used between the companion app (`libmondo_native.so`) and Beni's X5 SoC (`md_videotrans` + `md_upgrade`) over Wi-Fi (`192.168.7.1`), enabling **100% standalone** firmware uploads and robot control without the official app.

---

## 1. Layer 1 — UCP over UDP (`192.168.7.1:9007`)

### Connection Handshake
- **Transport:** UDP, server listens on port **`9007`** (`0x232f`).
- **Connect:** Client sends `"ucp:sync"` (8 bytes) → Server replies `"ucp:ack"` (7 bytes).
- **Disconnect:** Either side sends `"ucp:leav"` (8 bytes).
- **Liveness Timeout:** `3.5s` (`0x3567e0` µs) without receiving a heartbeat (`is_hb`) or unreliable packet; clients should emit a heartbeat every ~250 ms.

### UCP Packet Header (12 bytes, little-endian)
```c
struct ucp_hdr {
    uint16_t flags;   // bits  0..3 : version = 1
                      // bits  4..7 : magic   = 5  (low byte always 0x51)
                      // bit   8    : confirm (1 = reliable/ordered, 0 = unreliable)
                      // bit   9    : more_fragments (1 on non-final MTU segments)
                      // bits 10..11: channel (0=msg, 1=video, 2=audio, 3=DDS/Zenoh)
                      // bit  12    : is_hb (carries 11-byte BAK trailer)
    uint16_t una;     // next expected reliable sequence number from peer
    uint16_t sn;      // this packet's reliable sequence number (starts at 0)
    uint16_t len;     // total packet length in bytes (12 + payload [+ 11 if is_hb])
    uint32_t ts;      // CLOCK_MONOTONIC_RAW timestamp (microseconds)
};
```
- **MTU:** `1460` (`0x05b4`) bytes payload per segment.
- **Reliable DDS/Zenoh packets:** `flags = 0x0d51` (`0x0051 | (3 << 10) | 0x0100`, plus `0x0200` on intermediate fragments), 200 ms RTO retransmission.
- **Heartbeat + Selective ACK (`BAK`) trailer (11 bytes, little-endian):**
  Appended to packets with `flags & 0x1000`:
  ```c
  struct ucp_bak {
      uint8_t  magic;     // 0x05
      uint16_t una;       // next expected reliable sn
      uint32_t bitmap0;   // selective ACK bits for una+1 .. una+32
      uint32_t bitmap1;   // selective ACK bits for una+33 .. una+64
  };
  ```

---

## 2. Layer 2 — `mdZenohOverUcp` Framing

Carried as a byte stream over UCP **channel 3 (DDS, reliable)** and UCP **heartbeat (`is_hb`)** packets:

| Frame Type | Tag | Layout | Purpose |
| :--- | :---: | :--- | :--- |
| **OPEN** | `0xa4` | `[0xa4, uint32_be(session_id)]` (5 B) | Client initiates Zenoh session (`session_id = 1, 2, ...`) |
| **READY** | `0xa5` | `[0xa5, uint32_be(session_id)]` (5 B) | Peer declares publishers & subscribers are ready |
| **RESET** | `0xa6` | `[0xa6, uint32_be(session_id)]` (5 B) | Close / reset current Zenoh session |
| **DATA** | `0xa7` | `[0xa7, uint16_be(session_id), uint16_le(zlen), zenoh_msg[zlen]]` | Wraps one standard Zenoh 1.x stream frame |

- **KeepAlive / Ping optimization:** If a `0xa7` frame carries a Zenoh KeepAlive or Ping (`(zenoh_msg[0] >> 5) & 1 == 0` and `(zenoh_msg[0] & 0x1f) in {5, 6}`), it is sent in a UCP `is_hb` packet instead of channel 3.
- **Stock Zenoh compatibility:** Because Zenoh 1.x TCP stream transport (`zenoh-pico` 1.9.0 / `eclipse-zenoh` 1.x) natively frames every message as `[uint16_le(zlen), zenoh_msg[zlen]]`, a thin loopback TCP bridge (`tools/ucp_bridge.py`) lets unmodified `eclipse-zenoh` talk directly to Beni over UCP.

---

## 3. Layer 3 — Zenoh Topics & Protobuf OTA Control

- **Client → Robot namespace:** `app/<topic>` (e.g. `app/cmd/ota/upgrade_control_req`)
- **Robot → Client namespace:** `device/<topic>` (client subscribes to `device/**`)
- **Encoding:** `application/protobuf`

### Protobuf Messages (`proto/md/protocol/`)
All OTA Zenoh messages wrap a `WireHeader` (`uint32 seq_num = 1`) and a payload `data` field:
- `OtaUpgradeControlReq` (`app/cmd/ota/upgrade_control_req`): `header = 1`, `data = 2` (`OtaUpgradeControlReqData`)
- `OtaUpgradeControlRsp` (`device/cmd/ota/upgrade_control_rsp`): `header = 1`, `data = 2` (`OtaUpgradeControlRspData`)
- `OtaUpgradeProgress` (`device/status/ota/upgrade_progress`): `header = 1`, `data = 2` (`OtaUpgradeProgressData`)

### `OtaUpgradeControlReqData.control_cmd` Enum (verified from `md_upgrade`)
| Value | Variant | Meaning |
| :---: | :--- | :--- |
| `0` | `Start` | Register OTA task (`OtaUpgradeTaskContext`) & obtain `upload_grant_token` + resume `offset` |
| `1` | `Cancel` | Cancel active OTA task |
| `2` | `ConfirmComplete` | Confirm completed upgrade |
| `3` | `Query` | Query current OTA task snapshot |
| `4` | `Install` | Trigger verification & A/B slot flashing of uploaded ZIP |
| `127` (`0x7f`) | `CapabilityProbe` | Probe OTA v2 capability |

---

## 4. Layer 4 — Local HTTP TUS 1.0.0 Upload (`192.168.7.1:8080`)

Once `OtaUpgradeControlRsp.data.task.upload_grant_token` is obtained:

1. **Chunked Resumable Upload (TUS 1.0.0):**
   ```http
   PATCH /v2/upgrades/{upgrade_id}/upload HTTP/1.1
   Host: 192.168.7.1:8080
   Tus-Resumable: 1.0.0
   Upload-Offset: <byte_offset>
   Upload-Token: <upload_grant_token>
   Content-Type: application/offset+octet-stream
   ```
   Returns `204 No Content` with updated `Upload-Offset` and `Upgrade-State` (`RECEIVING` → `READY`).
2. **Install Trigger:**
   Send `OtaUpgradeControlReq` with `control_cmd = 4` (`Install`) over Zenoh (or `POST /v2/upgrades/{upgrade_id}/install` with `Upload-Token`), then monitor `device/status/ota/upgrade_progress` until `100%` / `COMPLETED`.

---

## 5. Reference Implementation & Loopback Test

- `tools/ucp_bridge.py` — Pure-Python UCP-over-UDP + `mdZenohOverUcp` loopback TCP bridge
- `tools/ota_proto.py` — Zero-dependency wire-exact Protobuf encoder/decoder
- `tools/ota_client.py` — `BeniOtaClient` (UCP + Zenoh + TUS 1.0.0 + Install + Progress)
- `tools/customize_fw.py` — TUI builder + standalone OTA uploader (`--upload` / `--upload-only`)
- `tests/test_standalone_ota.py` — End-to-end loopback test (`uv run tests/test_standalone_ota.py`)
