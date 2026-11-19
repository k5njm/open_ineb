#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "eclipse-zenoh>=1.0.0",
#   "requests>=2.32.0",
# ]
# ///
"""
End-to-end loopback verification of the standalone UCP + Zenoh-over-UCP +
Protobuf + TUS 1.0.0 OTA pipeline implemented in `tools/customize_fw.py`.

Exercises the full wire stack on 127.0.0.1:
  1. UDP UCP handshake (`ucp:sync` <-> `ucp:ack`), reliable channel 3 (DDS)
     framing/fragmentation, and UCP heartbeat + BAK ACKs
  2. `mdZenohOverUcp` `0xa4` (OPEN), `0xa5` (READY), `0xa7` (DATA) framing
  3. Live Eclipse Zenoh 1.x pub/sub over the UCP bridge
  4. `OtaUpgradeControlReq` (START -> grant token, INSTALL -> progress stream)
  5. TUS 1.0.0 `PATCH /v2/upgrades/{id}/upload` with `Upload-Token` & SHA-256 check
"""

from __future__ import annotations

import hashlib
import http.server
import socket
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from pathlib import Path

import zenoh

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

from ota_proto import (  # noqa: E402
    OtaControlCmd,
    OtaUpgradeControlReq,
    OtaUpgradeControlRsp,
    OtaUpgradeControlRspData,
    OtaUpgradeProgress,
    OtaUpgradeProgressData,
    OtaUpgradeTaskResult,
    WireHeader,
)
from ucp_bridge import ZenohOverUcpBridge  # noqa: E402


def _free_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def run_test() -> None:
    with tempfile.TemporaryDirectory(prefix="open_ineb_test_") as td:
        tmp = Path(td)
        # Create a 5.5 MiB test OTA ZIP (forces multi-chunk TUS upload AND UCP fragmentation)
        test_zip = tmp / "test_custom_ota.zip"
        with zipfile.ZipFile(test_zip, "w", compression=zipfile.ZIP_STORED) as zf:
            zf.writestr("config", "[package]\nversion = '26.10.28.04'\n")
            zf.writestr("payload.bin", bytes(range(256)) * (22 * 1024))  # ~5.6 MiB
        expected_sha = hashlib.sha256(test_zip.read_bytes()).hexdigest()
        expected_size = test_zip.stat().st_size

        # Shared mock robot state
        state: dict[str, object] = {
            "grant_token": "mock-grant-token-9f8e7d6c",
            "upgrade_id": None,
            "expected_sha": None,
            "received": bytearray(),
            "start_seen": False,
            "install_seen": False,
        }

        # 1. Start mock robot HTTP TUS 1.0.0 server
        class MockTusHandler(http.server.BaseHTTPRequestHandler):
            def log_message(self, fmt: str, *args: object) -> None:
                pass

            def do_PATCH(self) -> None:
                uid = state["upgrade_id"]
                if self.path != f"/v2/upgrades/{uid}/upload":
                    self.send_response(404)
                    self.end_headers()
                    return
                if self.headers.get("Tus-Resumable") != "1.0.0":
                    self.send_response(412)
                    self.end_headers()
                    return
                if self.headers.get("Upload-Token") != state["grant_token"]:
                    self.send_response(403)
                    self.end_headers()
                    return
                if self.headers.get("Content-Type") != "application/offset+octet-stream":
                    self.send_response(415)
                    self.end_headers()
                    return
                off = int(self.headers.get("Upload-Offset", "-1"))
                buf: bytearray = state["received"]  # type: ignore[assignment]
                if off != len(buf):
                    self.send_response(409)
                    self.send_header("Upgrade-Error-Code", "OFFSET_MISMATCH")
                    self.end_headers()
                    return
                length = int(self.headers.get("Content-Length", "0"))
                chunk = self.rfile.read(length)
                buf.extend(chunk)
                self.send_response(204)
                self.send_header("Tus-Resumable", "1.0.0")
                self.send_header("Upload-Offset", str(len(buf)))
                self.send_header(
                    "Upgrade-State",
                    "READY" if len(buf) == expected_size else "RECEIVING",
                )
                self.end_headers()

        httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), MockTusHandler)
        http_port = httpd.server_address[1]
        http_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        http_thread.start()

        # 2. Start mock robot Zenoh peer + UCP server bridge
        zenoh_tcp_port = _free_tcp_port()
        zcfg = zenoh.Config()
        zcfg.insert_json5("mode", '"peer"')
        zcfg.insert_json5("listen/endpoints", f'["tcp/127.0.0.1:{zenoh_tcp_port}"]')
        zcfg.insert_json5("scouting/multicast/enabled", "false")
        zcfg.insert_json5("scouting/gossip/enabled", "false")
        zsession = zenoh.open(zcfg)

        pub_rsp = zsession.declare_publisher(
            "device/cmd/ota/upgrade_control_rsp",
            encoding=zenoh.Encoding.APPLICATION_PROTOBUF,
        )
        pub_prog = zsession.declare_publisher(
            "device/status/ota/upgrade_progress",
            encoding=zenoh.Encoding.APPLICATION_PROTOBUF,
        )

        def on_req(sample: zenoh.Sample) -> None:
            req = OtaUpgradeControlReq.decode(sample.payload.to_bytes())
            assert req.data is not None
            seq = req.header.seq_num if req.header else 0
            cmd = req.data.control_cmd
            if cmd == OtaControlCmd.START:
                assert req.data.task is not None
                state["start_seen"] = True
                state["upgrade_id"] = req.data.task.upgrade_id
                state["expected_sha"] = req.data.task.sha256
                rsp = OtaUpgradeControlRsp(
                    header=WireHeader(seq_num=seq),
                    data=OtaUpgradeControlRspData(
                        ret_code=0,
                        current_upgrade_stage=1,
                        task=OtaUpgradeTaskResult(
                            protocol_version=2,
                            upgrade_id=req.data.task.upgrade_id,
                            device_id=req.data.task.device_id,
                            state="CREATED",
                            upload_grant_token=str(state["grant_token"]),
                            offset=0,
                            revision=1,
                        ),
                    ),
                )
                pub_rsp.put(rsp.encode(), encoding=zenoh.Encoding.APPLICATION_PROTOBUF)
            elif cmd == OtaControlCmd.INSTALL:
                state["install_seen"] = True
                rsp = OtaUpgradeControlRsp(
                    header=WireHeader(seq_num=seq),
                    data=OtaUpgradeControlRspData(ret_code=0, current_upgrade_stage=3),
                )
                pub_rsp.put(rsp.encode(), encoding=zenoh.Encoding.APPLICATION_PROTOBUF)

                def _emit_progress() -> None:
                    for stage, pct in [(2, 25), (3, 60), (3, 90), (4, 100)]:
                        time.sleep(0.15)
                        prog = OtaUpgradeProgress(
                            header=WireHeader(seq_num=pct),
                            data=OtaUpgradeProgressData(
                                return_code=0,
                                upgrade_stage=stage,
                                progress_percent=pct,
                                current_bytes=expected_size,
                                total_bytes=expected_size,
                            ),
                        )
                        pub_prog.put(prog.encode(), encoding=zenoh.Encoding.APPLICATION_PROTOBUF)

                threading.Thread(target=_emit_progress, daemon=True).start()

        sub_req = zsession.declare_subscriber("app/**", on_req)

        ucp_srv = ZenohOverUcpBridge(
            host="127.0.0.1",
            port=0,
            role="server",
            target_tcp_port=zenoh_tcp_port,
        )
        ucp_srv.start()
        ucp_port = ucp_srv.ucp.port

        try:
            # 3. Run the actual standalone `tools/customize_fw.py --upload-only` CLI!
            cmd = [
                "uv",
                "run",
                str(REPO_ROOT / "tools" / "customize_fw.py"),
                "--upload-only",
                str(test_zip),
                "--robot-ip",
                "127.0.0.1",
                "--ucp-port",
                str(ucp_port),
                "--http-port",
                str(http_port),
                "--zenoh-mode",
                "peer",
            ]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            print("--- customize_fw.py STDOUT ---")
            print(res.stdout)
            if res.stderr:
                print("--- customize_fw.py STDERR ---")
                print(res.stderr)
            assert res.returncode == 0, f"customize_fw.py failed with rc={res.returncode}"
            assert state["start_seen"], "Mock robot never received OtaControlCmd.START"
            assert state["install_seen"], "Mock robot never received OtaControlCmd.INSTALL"
            received_bytes = bytes(state["received"])  # type: ignore[arg-type]
            assert len(received_bytes) == expected_size, (
                f"Uploaded size mismatch: {len(received_bytes)} != {expected_size}"
            )
            got_sha = hashlib.sha256(received_bytes).hexdigest()
            assert got_sha == expected_sha, f"SHA-256 mismatch: {got_sha} != {expected_sha}"
            print(
                f"SUCCESS: Standalone UCP + Zenoh + Protobuf + TUS 1.0.0 OTA verified! "
                f"({expected_size:,} bytes, sha256={got_sha[:16]}...)"
            )
        finally:
            ucp_srv.close()
            sub_req.undeclare()
            pub_rsp.undeclare()
            pub_prog.undeclare()
            zsession.close()
            httpd.shutdown()
            httpd.server_close()


if __name__ == "__main__":
    run_test()
