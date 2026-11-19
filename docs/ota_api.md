# Beni (`LZ100`) Firmware OTA API Specification

Recovered from `libapp.so` (Dart 3.11.5 AOT snapshot, `com.mondorobotics.mondo.app` v1.0.85) via `blutter` object-pool reconstruction.

## Base URLs

- **Production:** `https://global-api.mondorobotics.com`
- **Pre-production:** `https://global-apipre.mondorobotics.com`
- **Test:** `https://global-apitest.mondorobotics.com`, `https://us-apitest.mondorobotics.com`

> All `/app/v1/files/*` endpoints require an authenticated Mondo user session (`HTTP 401 {"message":"Unauthorized"}` without credentials).

---

## 1. Account Authentication Endpoints

- `GET /account/v1/rsa/pub-key` — RSA public key for password encryption
- `POST /account/v1/login` (and `/account/v1/login/refresh`, `/account/v1/verify/email/code/send`, `/account/v1/verify/email/code/verify`)

---

## 2. Query Latest Firmware Package

**Endpoint:** `POST /app/v1/files/package/latest`  
*(Related: `/app/v1/files/package/list`)*

### Request (`GetLatestPackageReq`)

| JSON Field | Required | Notes |
| :--- | :---: | :--- |
| `board_id` | Yes | Default fallback in app: `"0x2005"` |
| `batch_id` | Yes | Default fallback in app: `"unsigned"`; runtime value from Zenoh `/cmd/common/get_crypto_info_rsp` (`CommonGetCryptoInfoRspData.batch_id`) |
| `mondo_partition_size` | Yes | Default fallback in app: `"1GB"`; runtime value from Zenoh `/cmd/common/get_partition_size_rsp` |
| `product_code` | Optional | Product identifier (e.g. `LZ100`) |
| `device_sn` | Optional | Robot serial number (from battery bay label or `/cmd/robot/sn_rsp`) |
| `firmware_version` | Optional | Currently installed firmware version |
| `package_type` | Optional | Package type filter |
| `package_name` | Optional | Package name filter |
| `app_build_number` | Optional | Companion app build number (e.g. `1000085`) |
| `os_version` | Optional | Phone OS version |

### Response (`GetLatestPackageRsp`)

Envelope: `{ "base": { "code", "msg", "timestamp" }, "data": { ... } }`

Data fields:
- `package_id`, `package_name`, `package_version`, `package_status`, `status_name`
- `package_type`, `package_type_name`, `product_code`
- `board_id`, `batch_id`, `mondo_partition_size`
- `file_size`, `sha256`, `firmware_count`
- **`oss_path`** — Passed as `sub_path` to `getDownloadUrl`
- `release_note`, `description`, `operator`, `create_time`, `publish_time`
- `compatibility` (`AppFirmwareCompatibility`: `rule_id`, `rule_revision`, `kind`, `firmware_version`, `target_firmware_version`, `app_update`)

---

## 3. Resolve Presigned Download URL

**Endpoint:** `POST /app/v1/files/getDownloadUrl`

### Request
- `sub_path`: The `oss_path` string returned by `/app/v1/files/package/latest`.

### Response (`GetDownloadUrlResp` → `DownloadUrlData`)
- `download_url` — Presigned Alibaba Cloud OSS URL (`x-oss-signature`, `x-oss-security-token`, `x-oss-expires`)
- `method` — HTTP method (e.g. `GET`)
- `expiration` — URL expiration timestamp
- `signed_headers` — Required signed HTTP headers for OSS request
- `full_path` — Full OSS object path

---

## 4. Device-Bound Security Envelope & Local Installation

Before installing on the robot, the app fetches a device-bound key envelope and pushes the package over Beni's local Wi-Fi AP (`192.168.7.1`):

1. **Cloud Key Envelope:**
   - `/app/v1/user-device/{sn}/security/ota-key-envelope/challenge`
   - `/app/v1/user-device/{sn}/security/ota-key-envelope`
2. **Local Upload (Phone → Beni):**
   - `POST http://192.168.7.1:8080/v2/upgrades/{uuid}/upload`
   - `POST http://192.168.7.1:8080/v2/upgrades/{uuid}/cancel-upload`
3. **Zenoh Orchestration:**
   - Command: `/cmd/ota/upgrade_control_req` / `/cmd/ota/upgrade_control_rsp`
   - Progress: `/status/ota/upgrade_progress`
