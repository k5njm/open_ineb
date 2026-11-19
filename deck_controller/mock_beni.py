"""
Interactive loopback `--mock` Beni simulator for testing the Steam Deck FPV
controller on macOS or SteamOS before unboxing hardware.

Runs on `127.0.0.1` using the exact same wire stack as a real Beni:
  - UCP UDP server + `mdZenohOverUcp` bridge + real `eclipse-zenoh` peer
  - Live 30 FPS encoded H.264 Annex-B FPV stream over UCP channel 1 reacting
    to drive velocity, yaw, gimbal pitch, standing height, jump, recording, and
    incoming TTS speech bubbles
  - Live Opus audio packets over UCP channel 2 (soft ambient chirp / TTS echo)
  - Live telemetry (`PmuOsdStatus`, `RobotStatus`, `CameraStatus`, `StorageStatus`)
  - Local HTTP `:8084` media gallery server with sample JPEG/MP4 files
"""

from __future__ import annotations

import http.server
import io
import json
import math
import socket
import sys
import threading
import time
from fractions import Fraction
from pathlib import Path

import av
import numpy as np
import zenoh

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "tools"))

from ucp_bridge import ZenohOverUcpBridge  # noqa: E402

from .beni_proto import (
    AudioPlaybackData,
    CameraCaptureCmd,
    CameraRecordReq,
    CameraStatus,
    CameraUnifiedCmd,
    ControlCtrlCmd,
    PmuOsdStatus,
    RobotStatus,
    StorageStatus,
    TOPIC_AUDIO_PLAYBACK,
    TOPIC_CAMERA_CAPTURE,
    TOPIC_CAMERA_RECORD_REQ,
    TOPIC_CAMERA_UNIFIED,
    TOPIC_CTRL_CMD,
    TOPIC_STATUS_CAMERA,
    TOPIC_STATUS_PMU,
    TOPIC_STATUS_ROBOT,
    TOPIC_STATUS_STORAGE,
)
from .tts import FRAME_SAMPLES, SAMPLE_RATE, encode_opus_frames


def _free_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _make_sample_jpeg(color_rgb: tuple[int, int, int], width: int = 320, height: int = 180) -> bytes:
    """Create a valid JPEG image in memory using PyAV (zero Pillow dependency)."""
    arr = np.zeros((height, width, 3), dtype=np.uint8)
    arr[:, :] = color_rgb
    # Add a simple diagonal stripe for visual texture
    ys, xs = np.ogrid[:height, :width]
    mask = ((xs + ys) // 16) % 2 == 0
    arr[mask] = np.clip(arr[mask].astype(np.int16) + 35, 0, 255).astype(np.uint8)
    frame = av.VideoFrame.from_ndarray(arr, format="rgb24")
    buf = io.BytesIO()
    with av.open(buf, mode="w", format="mjpeg") as container:
        stream = container.add_stream("mjpeg", rate=1)
        stream.width = width
        stream.height = height
        stream.pix_fmt = "yuvj420p"
        for pkt in stream.encode(frame):
            container.mux(pkt)
        for pkt in stream.encode(None):
            container.mux(pkt)
    return buf.getvalue()


class MockBeniServer:
    def __init__(self, *, width: int = 640, height: int = 360, fps: int = 25) -> None:
        self.width = width
        self.height = height
        self.fps = fps

        self.ucp_port = 0
        self.media_port = 0
        self._running = False

        # Simulated robot state
        self.vx = 0.0
        self.vy = 0.0
        self.yaw_rate = 0.0
        self.gimbal_pitch = 0.0
        self.height_norm = 0.5
        self.sport_mode = 1
        self.jump_t = 0.0
        self.recording = False
        self.rec_start_t = 0.0
        self.eis_enabled = True
        self.battery_soc = 87
        self.photo_count = 0
        self.tts_rx_packets = 0
        self.ctrl_rx_count = 0
        self.last_event = "MOCK BENI READY"

        self._zsession: zenoh.Session | None = None
        self._bridge: ZenohOverUcpBridge | None = None
        self._httpd: http.server.ThreadingHTTPServer | None = None
        self._threads: list[threading.Thread] = []

        # Sample media files for gallery testing
        self._media_blobs: dict[str, bytes] = {
            "101": _make_sample_jpeg((45, 110, 180)),
            "102": _make_sample_jpeg((180, 90, 50)),
            "103": _make_sample_jpeg((55, 155, 95)),
        }
        self._media_index = [
            {
                "id": "101",
                "filename": "BENI_20261225_090100.jpg",
                "media_type": "photo",
                "size": len(self._media_blobs["101"]),
                "created_at": "2026-12-25 09:01:00",
                "storage": "internal",
            },
            {
                "id": "102",
                "filename": "BENI_20261225_091522.mp4",
                "media_type": "video",
                "size": len(self._media_blobs["102"]),
                "duration_s": 14.5,
                "created_at": "2026-12-25 09:15:22",
                "storage": "internal",
            },
            {
                "id": "103",
                "filename": "BENI_20261225_093045.jpg",
                "media_type": "photo",
                "size": len(self._media_blobs["103"]),
                "created_at": "2026-12-25 09:30:45",
                "storage": "sdcard",
            },
        ]

    def start(self) -> None:
        self._running = True
        self._start_http_media()
        self._start_zenoh_ucp()
        for target, name in (
            (self._video_loop, "mock-video"),
            (self._telemetry_loop, "mock-telem"),
            (self._audio_loop, "mock-audio"),
        ):
            t = threading.Thread(target=target, name=name, daemon=True)
            t.start()
            self._threads.append(t)

    def close(self) -> None:
        self._running = False
        if self._bridge is not None:
            self._bridge.close()
        if self._zsession is not None:
            try:
                self._zsession.close()
            except Exception:
                pass
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()

    def _start_http_media(self) -> None:
        sim = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, fmt: str, *args: object) -> None:
                pass

            def do_GET(self) -> None:
                if self.path == "/media/files":
                    body = json.dumps({"files": sim._media_index}).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if self.path.startswith("/media/files/"):
                    parts = self.path.strip("/").split("/")
                    if len(parts) == 4 and parts[3] in ("thumbnail", "content"):
                        fid = parts[2]
                        blob = sim._media_blobs.get(fid)
                        if blob is not None:
                            self.send_response(200)
                            self.send_header("Content-Type", "image/jpeg")
                            self.send_header("Content-Length", str(len(blob)))
                            self.end_headers()
                            self.wfile.write(blob)
                            return
                self.send_response(404)
                self.end_headers()

        self._httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.media_port = self._httpd.server_address[1]
        t = threading.Thread(target=self._httpd.serve_forever, name="mock-http", daemon=True)
        t.start()
        self._threads.append(t)

    def _start_zenoh_ucp(self) -> None:
        zport = _free_tcp_port()
        cfg = zenoh.Config()
        cfg.insert_json5("mode", '"peer"')
        cfg.insert_json5("listen/endpoints", f'["tcp/127.0.0.1:{zport}"]')
        cfg.insert_json5("scouting/multicast/enabled", "false")
        cfg.insert_json5("scouting/gossip/enabled", "false")
        self._zsession = zenoh.open(cfg)

        self._pub_pmu = self._zsession.declare_publisher(TOPIC_STATUS_PMU)
        self._pub_robot = self._zsession.declare_publisher(TOPIC_STATUS_ROBOT)
        self._pub_cam = self._zsession.declare_publisher(TOPIC_STATUS_CAMERA)
        self._pub_storage = self._zsession.declare_publisher(TOPIC_STATUS_STORAGE)

        def on_app_cmd(sample: zenoh.Sample) -> None:
            key = str(sample.key_expr)
            raw = sample.payload.to_bytes()
            if key == TOPIC_CTRL_CMD:
                cmd = ControlCtrlCmd.decode(raw)
                self.ctrl_rx_count += 1
                self.vx = cmd.velocity_x
                self.vy = cmd.velocity_y
                self.yaw_rate = cmd.yaw_rate
                self.gimbal_pitch = cmd.gimbal_pitch
                self.height_norm = cmd.height
                self.sport_mode = cmd.loco_profile
                if cmd.action_trigger == 1:
                    self.jump_t = time.monotonic()
                    self.last_event = "JUMP!"
            elif key == TOPIC_CAMERA_RECORD_REQ:
                req = CameraRecordReq.decode(raw)
                self.recording = req.action == 1
                if self.recording:
                    self.rec_start_t = time.monotonic()
                    self.last_event = "RECORDING STARTED"
                else:
                    self.last_event = "RECORDING STOPPED"
            elif key == TOPIC_CAMERA_CAPTURE:
                _cap = CameraCaptureCmd.decode(raw)
                self.photo_count += 1
                fid = str(200 + self.photo_count)
                blob = _make_sample_jpeg((90, 140, 210))
                self._media_blobs[fid] = blob
                self._media_index.insert(
                    0,
                    {
                        "id": fid,
                        "filename": f"BENI_SNAP_{self.photo_count:03d}.jpg",
                        "media_type": "photo",
                        "size": len(blob),
                        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                        "storage": "internal",
                    },
                )
                self.last_event = f"PHOTO #{self.photo_count} SAVED"
            elif key == TOPIC_CAMERA_UNIFIED:
                uc = CameraUnifiedCmd.decode(raw)
                self.eis_enabled = bool(uc.eis_enable)
            elif key == TOPIC_AUDIO_PLAYBACK:
                _ap = AudioPlaybackData.decode(raw)
                self.tts_rx_packets += 1
                self.last_event = f"SPEAKING ({self.tts_rx_packets} pkts)"

        self._sub = self._zsession.declare_subscriber("app/**", on_app_cmd)

        self._bridge = ZenohOverUcpBridge(
            host="127.0.0.1",
            port=0,
            role="server",
            target_tcp_port=zport,
        )
        self._bridge.start()
        self.ucp_port = self._bridge.ucp.port

    def _telemetry_loop(self) -> None:
        seq = 0
        while self._running:
            time.sleep(0.25)
            if self._bridge is None or not self._bridge.ucp.connected:
                continue
            seq += 1
            rec_dur = int(time.monotonic() - self.rec_start_t) if self.recording else 0
            try:
                self._pub_pmu.put(PmuOsdStatus(seq_num=seq, soc=self.battery_soc).encode())
                self._pub_robot.put(
                    RobotStatus(
                        seq_num=seq,
                        robot_state=1,
                        velocity=int(self.vx * 1000),
                        obstacle_dis=1450,
                        gimbal_angle=int(self.gimbal_pitch * 10),
                        height=int(180 + self.height_norm * 140),
                        sport_mode=self.sport_mode,
                    ).encode()
                )
                self._pub_cam.put(
                    CameraStatus(
                        seq_num=seq,
                        rec_state=1 if self.recording else 0,
                        recording_state=1 if self.recording else 0,
                        rec_duration_s=rec_dur,
                        eis_status=1 if self.eis_enabled else 0,
                        vt_width=self.width,
                        vt_height=self.height,
                        vt_frame_rate=self.fps,
                    ).encode()
                )
                self._pub_storage.put(
                    StorageStatus(
                        seq_num=seq,
                        internal_used_space=3200,
                        internal_total_space=17000,
                    ).encode()
                )
            except Exception:
                pass

    def _video_loop(self) -> None:
        codec = av.CodecContext.create("libx264", "w")
        codec.width = self.width
        codec.height = self.height
        codec.pix_fmt = "yuv420p"
        codec.time_base = Fraction(1, self.fps)
        codec.bit_rate = 1_500_000
        codec.gop_size = self.fps
        codec.options = {"preset": "ultrafast", "tune": "zerolatency", "repeat-headers": "1"}
        codec.open()

        pts = 0
        grid_phase = 0.0
        interval = 1.0 / self.fps
        while self._running:
            t0 = time.monotonic()
            if self._bridge is None or not self._bridge.ucp.connected:
                time.sleep(0.05)
                continue

            # Render synthetic FPV frame reacting to drive, yaw, gimbal pitch, height & jump
            h, w = self.height, self.width
            img = np.zeros((h, w, 3), dtype=np.uint8)

            jump_off = 0
            jt = time.monotonic() - self.jump_t
            if 0.0 <= jt < 0.6:
                jump_off = int(55 * math.sin(math.pi * jt / 0.6))

            horizon = int(h * 0.48 + self.gimbal_pitch * 3.0 + (self.height_norm - 0.5) * 40 - jump_off)
            horizon = max(30, min(h - 30, horizon))

            # Sky gradient
            img[:horizon, :, 0] = 18
            img[:horizon, :, 1] = 28
            img[:horizon, :, 2] = 52
            # Ground plane
            img[horizon:, :, 0] = 24
            img[horizon:, :, 1] = 42
            img[horizon:, :, 2] = 34

            # Moving perspective ground grid driven by vx & yaw_rate
            grid_phase = (grid_phase + self.vx * 0.12 + 0.015) % 1.0
            for i in range(1, 9):
                y = horizon + int(((i - grid_phase) / 8.0) ** 1.8 * (h - horizon))
                if horizon <= y < h:
                    img[y : min(h, y + 2), :, :] = (55, 115, 95)

            cx = int(w * 0.5 - self.yaw_rate * 70 - self.vy * 45)
            cx = max(20, min(w - 20, cx))
            cy = horizon
            # Horizon line
            img[max(0, cy - 1) : min(h, cy + 1), :, :] = (90, 210, 175)
            # Moving crosshair
            img[max(0, cy - 18) : min(h, cy + 18), max(0, cx - 2) : min(w, cx + 2)] = (255, 220, 60)
            img[max(0, cy - 2) : min(h, cy + 2), max(0, cx - 18) : min(w, cx + 18)] = (255, 220, 60)

            # Red border pulse when recording
            if self.recording and int(time.monotonic() * 2) % 2 == 0:
                img[10:24, 10:24] = (240, 45, 45)

            frame = av.VideoFrame.from_ndarray(img, format="rgb24")
            frame.pts = pts
            pts += 1
            for pkt in codec.encode(frame):
                self._bridge.ucp.send_video(bytes(pkt))

            elapsed = time.monotonic() - t0
            if elapsed < interval:
                time.sleep(interval - elapsed)

    def _audio_loop(self) -> None:
        """Emit gentle periodic 48 kHz Opus heartbeat chime packets on UCP channel 2."""
        # Pre-generate 0.2s soft 520 Hz chime once
        t = np.linspace(0.0, 0.2, int(SAMPLE_RATE * 0.2), endpoint=False, dtype=np.float32)
        env = np.sin(math.pi * t / 0.2)
        chime = (np.sin(2.0 * math.pi * 520.0 * t) * env * 4000.0).astype(np.int16)
        opus_pkts = list(encode_opus_frames(chime))
        while self._running:
            time.sleep(4.0)
            if self._bridge is None or not self._bridge.ucp.connected:
                continue
            for pkt in opus_pkts:
                if not self._running:
                    break
                self._bridge.ucp.send_audio(pkt)
                time.sleep(0.02)
