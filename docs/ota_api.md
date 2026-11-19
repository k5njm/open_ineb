# Beni (`LZ100`) Firmware OTA API Specification

Recovered from `libapp.so` (Dart 3.11.5 AOT snapshot, `com.mondorobotics.mondo.app` v1.0.85) via `blutter` object-pool reconstruction.

## Base URLs

- **Production:** `https://global-api.mondorobotics.com`
- **Pre-production:** `https://global-apipre.mondorobotics.com`
- **Test:** `https://global-apitest.mondorobotics.com`, `https://us-apitest.mondorobotics.com`

> All `/app/v1/files/*` endpoints require an authenticated Mondo user session (`HTTP 401 {"message":"Unauthorized"}` without credentials).

---

## 1. Account Authentication Endpoints

1. `GET /account/v1/rsa/pub-key` — Returns PEM RSA public key (`data.public_key`).
2. `POST /account/v1/verify/pwd` — Body: `{"account", "password", "region", "scene": "login"}` where `password` is Base64-encoded **RSA-OAEP (SHA-256 + MGF1-SHA256)**. Returns `data.flow_token`.
3. `POST /account/v1/login` — Body: `{"flow_token"}`. Returns `data.access_token`, `data.refresh_token`, `data.expires_in`, `data.uuid`.

---

## 2. Query Latest Firmware Package

**Endpoint:** `POST /app/v1/files/package/latest`  
*(Related: `POST /app/v1/files/package/list` — passing empty strings for `board_id`, `batch_id`, and `mondo_partition_size` lists all visible packages)*

### Observed Hardware Profiles

| `product_code` | `board_id` | `batch_id` | `mondo_partition_size` | Notes |
| :--- | :--- | :--- | :--- | :--- |
| `lz110` | `0x3001` | `lz100-PRE-20260601-001` | `1GB` | Production Beni firmware (e.g. `V26.11.19.01`, 11 modules, ~1 GB) |
| `sisyphus` | `0x2005` | `unsigned` | `1GB` | Earlier dev/mass `plain.unsign.zip` builds (6 modules, ~773 MB) |

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
- `type` (integer): `1` = firmware packages (`mondo-package-manager`), `3` = `device-data/prod`, `4` = `hms` catalogs.
- `sub_path` (string): The `oss_path` string returned by `/app/v1/files/package/latest` or `/list`.

### Response (`GetDownloadUrlResp` → `DownloadUrlData`)
- `download_url` — Presigned AWS S3 (`us-west-1`) or Alibaba Cloud OSS URL
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
