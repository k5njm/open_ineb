#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "eclipse-zenoh>=1.0.0",
#   "requests>=2.32.0",
# ]
# ///
"""
Standalone Beni (LZ110) OTA client combining:
  1. UCP-over-UDP (:9007) + `mdZenohOverUcp` (`0xa4`/`0xa5`/`0xa6`/`0xa7`) framing
  2. Stock Eclipse Zenoh 1.x pub/sub over the local loopback TCP bridge:
       - Pub: `app/cmd/ota/upgrade_control_req` (`application/protobuf`)
       - Sub: `device/cmd/ota/upgrade_control_rsp`, `device/status/ota/upgrade_progress`
  3. TUS 1.0.0 resumable chunked HTTP upload (`PATCH :8080/v2/upgrades/{id}/upload`
     with `Upload-Token`, `Tus-Resumable: 1.0.0`, `Upload-Offset`)
  4. Install trigger (`OtaControlCmd.INSTALL` + optional HTTP `/install`) & live
     `/status/ota/upgrade_progress` monitoring.
"""

from __future__ import annotations

import hashlib
import queue
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import requests
import zenoh

# Allow importing sibling modules whether run as script or package
_TOOLS_DIR = Path(__file__).resolve().parent
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))

from ota_proto import (  # noqa: E402
    OtaControlCmd,
    OtaUpgradeControlReq,
    OtaUpgradeControlReqData,
    OtaUpgradeControlRsp,
    OtaUpgradeProgress,
    OtaUpgradeTaskContext,
    WireHeader,
)
from ucp_bridge import UCP_DEFAULT_PORT, ZenohOverUcpBridge  # noqa: E402

TOPIC_OTA_REQ = "app/cmd/ota/upgrade_control_req"
TOPIC_OTA_RSP = "device/cmd/ota/upgrade_control_rsp"
TOPIC_OTA_PROGRESS = "device/status/ota/upgrade_progress"

STAGE_NAMES = {
    0: "IDLE",
    1: "PREPARING",
    2: "VERIFYING",
    3: "INSTALLING",
    4: "COMPLETED",
    5: "FAILED",
}


@dataclass
class PrepareResult:
    upgrade_id: str
    upload_grant_token: str
    offset: int
    state: str
    ret_code: int
    raw_rsp: OtaUpgradeControlRsp


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


class BeniOtaClient:
    """Standalone UCP + Zenoh + TUS 1.0.0 OTA client for Mondo Robotics Beni."""

    def __init__(
        self,
        robot_ip: str = "192.168.7.1",
        *,
        ucp_port: int = UCP_DEFAULT_PORT,
        http_port: int = 8080,
        zenoh_mode: str = "peer",
        connect_timeout: float = 8.0,
    ) -> None:
        self.robot_ip = robot_ip
        self.ucp_port = ucp_port
        self.http_port = http_port
        self.zenoh_mode = zenoh_mode
        self.connect_timeout = connect_timeout

        self._bridge: ZenohOverUcpBridge | None = None
        self._session: zenoh.Session | None = None
        self._pub: zenoh.Publisher | None = None
        self._sub: zenoh.Subscriber | None = None
        self._seq = 0
        self._rsp_queue: queue.Queue[OtaUpgradeControlRsp] = queue.Queue()
        self._progress_queue: queue.Queue[OtaUpgradeProgress] = queue.Queue()
        self._progress_cb: Callable[[OtaUpgradeProgress], None] | None = None

    def connect(self) -> None:
        self._bridge = ZenohOverUcpBridge(
            host=self.robot_ip,
            port=self.ucp_port,
            role="client",
            session_id=1,
        )
        self._bridge.start(timeout=self.connect_timeout)

        cfg = zenoh.Config()
        cfg.insert_json5("mode", f'"{self.zenoh_mode}"')
        cfg.insert_json5("connect/endpoints", f'["{self._bridge.local_endpoint}"]')
        cfg.insert_json5("scouting/multicast/enabled", "false")
        cfg.insert_json5("scouting/gossip/enabled", "false")

        self._session = zenoh.open(cfg)
        self._pub = self._session.declare_publisher(
            TOPIC_OTA_REQ,
            encoding=zenoh.Encoding.APPLICATION_PROTOBUF,
        )
        self._sub = self._session.declare_subscriber("device/**", self._on_zenoh_sample)
        # Notify Beni's mdZenohOverUcp server that our publishers & subscribers are declared
        self._bridge.send_ready()
        # Give Zenoh interest propagation a brief moment to settle
        time.sleep(0.25)

    def close(self) -> None:
        if self._sub is not None:
            try:
                self._sub.undeclare()
            except Exception:
                pass
            self._sub = None
        if self._pub is not None:
            try:
                self._pub.undeclare()
            except Exception:
                pass
            self._pub = None
        if self._session is not None:
            try:
                self._session.close()
            except Exception:
                pass
            self._session = None
        if self._bridge is not None:
            self._bridge.close()
            self._bridge = None

    def __enter__(self) -> "BeniOtaClient":
        self.connect()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def _on_zenoh_sample(self, sample: zenoh.Sample) -> None:
        key = str(sample.key_expr)
        payload = sample.payload.to_bytes()
        if key == TOPIC_OTA_RSP:
            try:
                rsp = OtaUpgradeControlRsp.decode(payload)
                self._rsp_queue.put(rsp)
            except Exception:
                pass
        elif key == TOPIC_OTA_PROGRESS:
            try:
                prog = OtaUpgradeProgress.decode(payload)
                self._progress_queue.put(prog)
                if self._progress_cb is not None:
                    self._progress_cb(prog)
            except Exception:
                pass

    def send_control(
        self,
        cmd: OtaControlCmd,
        task: OtaUpgradeTaskContext | None = None,
        *,
        timeout: float = 10.0,
    ) -> OtaUpgradeControlRsp:
        if self._pub is None:
            raise RuntimeError("BeniOtaClient is not connected")
        seq = self._next_seq()
        req = OtaUpgradeControlReq(
            header=WireHeader(seq_num=seq),
            data=OtaUpgradeControlReqData(control_cmd=cmd, task=task),
        )
        # Drain stale responses before publishing
        while not self._rsp_queue.empty():
            try:
                self._rsp_queue.get_nowait()
            except queue.Empty:
                break

        self._pub.put(req.encode(), encoding=zenoh.Encoding.APPLICATION_PROTOBUF)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            remaining = max(0.05, deadline - time.monotonic())
            try:
                rsp = self._rsp_queue.get(timeout=remaining)
                return rsp
            except queue.Empty:
                break
        raise TimeoutError(
            f"Timed out after {timeout}s waiting for {TOPIC_OTA_RSP} (cmd={cmd.name}, seq={seq})"
        )

    def prepare_upgrade(
        self,
        zip_path: Path,
        *,
        version: str,
        device_id: str = "",
        upgrade_id: str | None = None,
        timeout: float = 15.0,
    ) -> tuple[PrepareResult, OtaUpgradeTaskContext]:
        uid = upgrade_id or str(uuid.uuid4())
        digest = sha256_file(zip_path)
        size = zip_path.stat().st_size
        ctx = OtaUpgradeTaskContext(
            protocol_version=2,
            upgrade_id=uid,
            device_id=device_id,
            sha256=digest,
            size=size,
            version=version,
            original_filename=zip_path.name,
        )
        rsp = self.send_control(OtaControlCmd.START, ctx, timeout=timeout)
        data = rsp.data
        if data is None:
            raise RuntimeError("Robot returned empty OtaUpgradeControlRspData")
        if data.ret_code != 0:
            err = data.task.error_code if data.task else ""
            raise RuntimeError(
                f"Robot rejected OTA START (ret_code={data.ret_code}, error_code={err!r})"
            )
        if data.task is None or not data.task.upload_grant_token:
            raise RuntimeError(
                f"Robot did not return an upload_grant_token in OtaUpgradeTaskResult: {data}"
            )
        return (
            PrepareResult(
                upgrade_id=data.task.upgrade_id or uid,
                upload_grant_token=data.task.upload_grant_token,
                offset=data.task.offset,
                state=data.task.state,
                ret_code=data.ret_code,
                raw_rsp=rsp,
            ),
            ctx,
        )

    def upload_tus(
        self,
        zip_path: Path,
        *,
        upgrade_id: str,
        upload_token: str,
        start_offset: int = 0,
        chunk_size: int = 2 * 1024 * 1024,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> dict[str, str]:
        """
        Upload `zip_path` using `md_upgrade`'s TUS 1.0.0 resumable endpoint:
          `PATCH http://{robot_ip}:{http_port}/v2/upgrades/{upgrade_id}/upload`
        """
        total = zip_path.stat().st_size
        url = f"http://{self.robot_ip}:{self.http_port}/v2/upgrades/{upgrade_id}/upload"
        offset = start_offset
        last_headers: dict[str, str] = {}

        with zip_path.open("rb") as f:
            if offset > 0:
                f.seek(offset)
            while offset < total:
                chunk = f.read(chunk_size)
                if not chunk:
                    break
                resp = requests.patch(
                    url,
                    data=chunk,
                    headers={
                        "Tus-Resumable": "1.0.0",
                        "Upload-Offset": str(offset),
                        "Upload-Token": upload_token,
                        "Content-Type": "application/offset+octet-stream",
                    },
                    timeout=120,
                )
                if resp.status_code not in (200, 201, 204):
                    err_code = resp.headers.get("Upgrade-Error-Code", "")
                    raise RuntimeError(
                        f"TUS upload failed at offset {offset}/{total}: "
                        f"HTTP {resp.status_code} {err_code} {resp.text[:200]}"
                    )
                last_headers = dict(resp.headers)
                srv_off = resp.headers.get("Upload-Offset")
                offset = int(srv_off) if srv_off is not None else offset + len(chunk)
                if on_progress is not None:
                    on_progress(offset, total)
        return last_headers

    def trigger_install(
        self,
        *,
        upgrade_id: str,
        upload_token: str,
        task_ctx: OtaUpgradeTaskContext,
        timeout: float = 15.0,
    ) -> OtaUpgradeControlRsp:
        """Trigger installation via Zenoh `OtaControlCmd.INSTALL` (with HTTP fallback)."""
        try:
            return self.send_control(OtaControlCmd.INSTALL, task_ctx, timeout=timeout)
        except Exception:
            # Fallback to HTTP v2 install endpoint
            url = f"http://{self.robot_ip}:{self.http_port}/v2/upgrades/{upgrade_id}/install"
            resp = requests.post(
                url,
                headers={"Upload-Token": upload_token},
                timeout=30,
            )
            if resp.status_code not in (200, 202, 204):
                raise RuntimeError(
                    f"HTTP install trigger failed: HTTP {resp.status_code} {resp.text[:200]}"
                )
            return OtaUpgradeControlRsp()

    def monitor_progress(
        self,
        *,
        on_update: Callable[[OtaUpgradeProgress], None] | None = None,
        timeout: float = 180.0,
    ) -> OtaUpgradeProgress | None:
        """Stream `/status/ota/upgrade_progress` updates until 100% / terminal stage."""
        self._progress_cb = on_update
        last: OtaUpgradeProgress | None = None
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            remaining = max(0.1, min(1.0, deadline - time.monotonic()))
            try:
                prog = self._progress_queue.get(timeout=remaining)
            except queue.Empty:
                continue
            last = prog
            if prog.data is not None:
                if prog.data.return_code != 0:
                    return prog
                if prog.data.upgrade_stage >= 4 or prog.data.progress_percent >= 100:
                    return prog
        return last
