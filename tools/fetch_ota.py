#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "requests>=2.31.0",
#   "cryptography>=42.0.0",
# ]
# ///
"""
open_ineb Firmware OTA Archiver CLI for Mondo Robotics Beni (LZ100).

Uses your own Mondo account credentials to query official firmware metadata
and download a local backup copy of the firmware package from Mondo's OTA API.
"""

from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import json
import os
import pathlib
import sys
from typing import Any

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

DEFAULT_BASE_URL = "https://global-api.mondorobotics.com"
DEFAULT_TOKEN_FILE = pathlib.Path(".mondo_token.json")


class MondoClient:
    def __init__(self, base_url: str = DEFAULT_BASE_URL, token: str | None = None, verbose: bool = False):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.verbose = verbose
        self.session = requests.Session()
        self.session.headers.update({
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-App-Package": "com.mondorobotics.mondo.app",
            "X-App-Version": "1.0.85",
            "X-App-Build-Id": "1000085",
        })

    def _headers(self) -> dict[str, str]:
        h: dict[str, str] = {}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        if self.verbose:
            redacted = dict(payload or {})
            if "password" in redacted:
                redacted["password"] = "<redacted>"
            print(f"[HTTP] {method} {url} body={json.dumps(redacted)}", file=sys.stderr)
        resp = self.session.request(method, url, json=payload, headers=self._headers(), timeout=30)
        if self.verbose:
            print(f"[HTTP] <- {resp.status_code} {resp.text[:500]}", file=sys.stderr)
        resp.raise_for_status()
        return resp.json()

    def get_rsa_public_key(self) -> str:
        res = self._request("GET", "/account/v1/rsa/pub-key")
        if res.get("code") != 0 or not res.get("data", {}).get("public_key"):
            raise RuntimeError(f"Failed to fetch RSA public key: {res}")
        return res["data"]["public_key"]

    @staticmethod
    def encrypt_password(password: str, pem_public_key: str) -> str:
        # Verified from RsaCrypto at 0x1488034 (`sub x0, x2, #0x42`, i.e., key_bytes - 66 = 2*32 + 2):
        # RSA-OAEP with SHA-256 + MGF1(SHA-256).
        pub = serialization.load_pem_public_key(pem_public_key.encode("utf-8"))
        ct = pub.encrypt(
            password.encode("utf-8"),
            padding.OAEP(
                mgf=padding.MGF1(algorithm=hashes.SHA256()),
                algorithm=hashes.SHA256(),
                label=None,
            ),
        )
        return base64.b64encode(ct).decode("ascii")

    def login(self, account: str, password: str, region: str = "US", scene: str = "login") -> dict[str, Any]:
        pem = self.get_rsa_public_key()
        enc_pwd = self.encrypt_password(password, pem)

        verify_res = self._request(
            "POST",
            "/account/v1/verify/pwd",
            {
                "account": account,
                "password": enc_pwd,
                "region": region,
                "scene": scene,
            },
        )
        if verify_res.get("code") != 0:
            raise RuntimeError(f"Password verification failed: {verify_res}")

        flow_token = (verify_res.get("data") or {}).get("flow_token")
        if not flow_token:
            raise RuntimeError(f"Missing flow_token in verify/pwd response: {verify_res}")

        login_res = self._request("POST", "/account/v1/login", {"flow_token": flow_token})
        if login_res.get("code") != 0:
            raise RuntimeError(f"Login exchange failed: {login_res}")

        data = login_res.get("data") or {}
        access_token = data.get("access_token")
        if not access_token:
            raise RuntimeError(f"Missing access_token in login response: {login_res}")
        self.token = access_token
        return data

    def get_latest_package(
        self,
        board_id: str = "0x3001",
        batch_id: str = "lz100-PRE-20260601-001",
        mondo_partition_size: str = "1GB",
        product_code: str | None = None,
        device_sn: str | None = None,
        firmware_version: str | None = None,
        package_type: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "board_id": board_id,
            "batch_id": batch_id,
            "mondo_partition_size": mondo_partition_size,
            "app_build_number": "1000085",
            "os_version": "Android 15",
        }
        if product_code:
            body["product_code"] = product_code
        if device_sn:
            body["device_sn"] = device_sn
        if firmware_version:
            body["firmware_version"] = firmware_version
        if package_type:
            body["package_type"] = package_type
        return self._request("POST", "/app/v1/files/package/latest", body)

    def list_packages(
        self,
        board_id: str = "",
        batch_id: str = "",
        mondo_partition_size: str = "",
        page: int = 1,
        page_size: int = 20,
    ) -> dict[str, Any]:
        body = {
            "board_id": board_id,
            "batch_id": batch_id,
            "mondo_partition_size": mondo_partition_size,
            "page": page,
            "page_size": page_size,
        }
        return self._request("POST", "/app/v1/files/package/list", body)

    def get_download_url(self, sub_path: str, file_type: int = 1) -> dict[str, Any]:
        return self._request(
            "POST",
            "/app/v1/files/getDownloadUrl",
            {"type": file_type, "sub_path": sub_path},
        )

    def download_file(
        self,
        download_url: str,
        out_path: pathlib.Path,
        signed_headers: dict[str, str] | None = None,
        expected_sha256: str | None = None,
    ) -> str:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        headers = dict(signed_headers or {})
        hasher = hashlib.sha256()
        total = 0
        with requests.get(download_url, headers=headers, stream=True, timeout=120) as r:
            r.raise_for_status()
            content_len = int(r.headers.get("Content-Length", "0"))
            with out_path.open("wb") as f:
                for chunk in r.iter_content(chunk_size=1024 * 256):
                    if chunk:
                        f.write(chunk)
                        hasher.update(chunk)
                        total += len(chunk)
                        if content_len:
                            pct = (total / content_len) * 100
                            print(f"\rDownloading {out_path.name}: {total:,}/{content_len:,} bytes ({pct:.1f}%)", end="", file=sys.stderr)
        print("", file=sys.stderr)
        digest = hasher.hexdigest()
        if expected_sha256 and digest.lower() != expected_sha256.lower():
            raise RuntimeError(f"SHA256 mismatch! expected={expected_sha256} actual={digest}")
        return digest


def load_token(token_arg: str | None, token_file: pathlib.Path) -> str | None:
    if token_arg:
        return token_arg
    if os.environ.get("MONDO_TOKEN"):
        return os.environ["MONDO_TOKEN"]
    if token_file.exists():
        data = json.loads(token_file.read_text())
        return data.get("access_token")
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Mondo Robotics Beni (LZ100) Firmware OTA Archiver")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help=f"API base URL (default: {DEFAULT_BASE_URL})")
    parser.add_argument("--token", default=None, help="Bearer access token (or set MONDO_TOKEN)")
    parser.add_argument("--token-file", type=pathlib.Path, default=DEFAULT_TOKEN_FILE, help="Local token cache file")
    parser.add_argument("-v", "--verbose", action="store_true", help="Print HTTP requests/responses")

    sub = parser.add_subparsers(dest="cmd", required=True)

    p_login = sub.add_parser("login", help="Log in with your Mondo account and cache access token")
    p_login.add_argument("--account", required=True, help="Account email or phone")
    p_login.add_argument("--password", default=None, help="Password (prompted securely if omitted)")
    p_login.add_argument("--region", default="US", help="Region code (e.g. US, CN, EU)")

    p_latest = sub.add_parser("latest", help="Query latest firmware package metadata")
    p_latest.add_argument("--board-id", default="0x3001")
    p_latest.add_argument("--batch-id", default="lz100-PRE-20260601-001")
    p_latest.add_argument("--partition-size", default="1GB")
    p_latest.add_argument("--product-code", default=None)
    p_latest.add_argument("--device-sn", default=None)
    p_latest.add_argument("--firmware-version", default=None)
    p_latest.add_argument("--package-type", default=None)

    p_list = sub.add_parser("list", help="List available firmware packages")
    p_list.add_argument("--board-id", default="")
    p_list.add_argument("--batch-id", default="")
    p_list.add_argument("--partition-size", default="")
    p_list.add_argument("--page", type=int, default=1)
    p_list.add_argument("--page-size", type=int, default=20)

    p_dl = sub.add_parser("download", help="Resolve presigned URL and download firmware package")
    p_dl.add_argument("--sub-path", default=None, help="OSS/S3 path (if omitted, queries `latest` first)")
    p_dl.add_argument("--type", dest="file_type", type=int, default=1, help="Integer download type (1=firmware, 3=device-data, 4=hms)")
    p_dl.add_argument("--board-id", default="0x3001")
    p_dl.add_argument("--batch-id", default="lz100-PRE-20260601-001")
    p_dl.add_argument("--partition-size", default="1GB")
    p_dl.add_argument("--product-code", default=None)
    p_dl.add_argument("--device-sn", default=None)
    p_dl.add_argument("--out", type=pathlib.Path, default=None, help="Output file path")
    p_dl.add_argument("--url-only", action="store_true", help="Print presigned URL JSON without downloading")

    args = parser.parse_args()
    token = load_token(args.token, args.token_file)
    client = MondoClient(base_url=args.base_url, token=token, verbose=args.verbose)

    if args.cmd == "login":
        pwd = args.password or getpass.getpass("Mondo account password: ")
        data = client.login(args.account, pwd, region=args.region)
        args.token_file.write_text(json.dumps(data, indent=2))
        os.chmod(args.token_file, 0o600)
        print(f"Login successful (uuid={data.get('uuid')}). Token saved to {args.token_file}")
        return 0

    if not client.token:
        print("Error: No access token found. Run `login` first or pass --token.", file=sys.stderr)
        return 1

    if args.cmd == "latest":
        res = client.get_latest_package(
            board_id=args.board_id,
            batch_id=args.batch_id,
            mondo_partition_size=args.partition_size,
            product_code=args.product_code,
            device_sn=args.device_sn,
            firmware_version=args.firmware_version,
            package_type=args.package_type,
        )
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0

    if args.cmd == "list":
        res = client.list_packages(
            board_id=args.board_id,
            batch_id=args.batch_id,
            mondo_partition_size=args.partition_size,
            page=args.page,
            page_size=args.page_size,
        )
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0

    if args.cmd == "download":
        sub_path = args.sub_path
        expected_sha256 = None
        default_name = "beni_firmware.bin"
        if not sub_path:
            latest = client.get_latest_package(
                board_id=args.board_id,
                batch_id=args.batch_id,
                mondo_partition_size=args.partition_size,
                product_code=args.product_code,
                device_sn=args.device_sn,
            )
            pkg = latest.get("data") or {}
            sub_path = pkg.get("oss_path")
            expected_sha256 = pkg.get("sha256")
            if not sub_path:
                print(f"Error: Could not find oss_path in latest response:\n{json.dumps(latest, indent=2)}", file=sys.stderr)
                return 1
            ver = pkg.get("package_version") or "unknown"
            default_name = pathlib.Path(sub_path).name or f"beni_fw_{ver}.bin"
            print(f"Found latest firmware: version={ver} size={pkg.get('file_size')} sha256={expected_sha256}", file=sys.stderr)

        dl_res = client.get_download_url(sub_path=sub_path, file_type=args.file_type)
        if args.url_only:
            print(json.dumps(dl_res, indent=2, ensure_ascii=False))
            return 0

        dl_data = dl_res.get("data") or {}
        url = dl_data.get("download_url")
        if not url:
            print(f"Error: Missing download_url in response:\n{json.dumps(dl_res, indent=2)}", file=sys.stderr)
            return 1

        out_path = args.out or pathlib.Path(default_name)
        digest = client.download_file(
            download_url=url,
            out_path=out_path,
            signed_headers=dl_data.get("signed_headers"),
            expected_sha256=expected_sha256,
        )
        print(f"Saved firmware to {out_path} (sha256={digest})")
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
