"""
Live Beni client for the Steam Deck controller:
  - UCP channel 1 (video) -> PyAV HEVC/H.264 auto-detect decoder -> latest RGB frame
  - UCP channel 2 (audio) -> PyAV Opus decoder -> callback / playback queue
  - UCP channel 3 (Zenoh) -> 50 Hz drive/gimbal/posture/jump commands, camera
    capture/record/EIS controls, telemetry subscriptions, and TTS Opus uplink
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import av
import numpy as np
import zenoh

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "tools"))

from ucp_bridge import UCP_DEFAULT_PORT, ZenohOverUcpBridge  # noqa: E402

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
    TOPIC_VT_REQ_IFRAME,
    VtReqIframe,
)
from .tts import FRAME_MS, SAMPLE_RATE, encode_opus_frames, play_over_ssh, synthesize_pcm

log = logging.getLogger("open_ineb.deck.client")


def _detect_video_codec(annexb: bytes) -> str | None:
    """Detect HEVC vs H.264 from Annex-B start code (`00 00 00 01 <nal>`)."""
    idx = annexb.find(b"\x00\x00\x00\x01")
    if idx < 0 or idx + 5 > len(annexb):
        return None
    b0 = annexb[idx + 4]
    hevc_type = (b0 >> 1) & 0x3F
    h264_type = b0 & 0x1F
    # HEVC VPS (32), SPS (33), PPS (34), IDR (19/20), CRA (21)
    if hevc_type in (32, 33, 34, 19, 20, 21) and (b0 & 0x80) == 0:
        # Disambiguate H.264 SPS (0x67 -> hevc_type=51) / IDR (0x65 -> hevc_type=50)
        return "hevc"
    if h264_type in (7, 8, 5, 1):
        return "h264"
    return "hevc"


@dataclass
class TelemetryState:
    connected: bool = False
    battery_soc: int = 0
    robot_state: int = 0
    velocity_mm_s: int = 0
    obstacle_dis_mm: int = 0
    gimbal_angle_deg: float = 0.0
    height_mm: int = 0
    sport_mode: int = 1
    recording: bool = False
    rec_duration_s: int = 0
    eis_enabled: bool = True
    vt_width: int = 1280
    vt_height: int = 720
    video_fps: float = 0.0
    video_codec: str = "hevc"
    rtt_ms: float = 0.0
    storage_free_gb: float = 0.0
    last_tts_text: str = ""
    last_tts_engine: str = ""
    extra_status: str = ""
    _fps_frames: int = 0
    _fps_window_t: float = field(default_factory=time.monotonic)


class BeniDeckClient:
    def __init__(
        self,
        robot_ip: str = "192.168.7.1",
        *,
        ucp_port: int = UCP_DEFAULT_PORT,
        zenoh_mode: str = "peer",
        ssh_port: int = 2222,
        on_audio_pcm: Callable[[np.ndarray], None] | None = None,
    ) -> None:
        self.robot_ip = robot_ip
        self.ucp_port = ucp_port
        self.zenoh_mode = zenoh_mode
        self.ssh_port = ssh_port
        self.on_audio_pcm = on_audio_pcm

        self.telemetry = TelemetryState()
        self._lock = threading.Lock()
        self._latest_rgb: np.ndarray | None = None
        self._frame_seq = 0

        self._video_decoder: av.CodecContext | None = None
        self._video_codec_name: str | None = None
        self._audio_decoder: av.CodecContext | None = None
        self._audio_resampler = av.AudioResampler(format="s16", layout="mono", rate=SAMPLE_RATE)

        self._bridge: ZenohOverUcpBridge | None = None
        self._session: zenoh.Session | None = None
        self._pubs: dict[str, zenoh.Publisher] = {}
        self._sub: zenoh.Subscriber | None = None
        self._seq = 0
        self._audio_seq = 0
        self._tts_lock = threading.Lock()
        self.audio_muted = False

    def connect(self, timeout: float = 6.0) -> None:
        self._bridge = ZenohOverUcpBridge(
            host=self.robot_ip,
            port=self.ucp_port,
            role="client",
            session_id=1,
            on_video=self._on_video_frame,
            on_audio=self._on_audio_packet,
        )
        self._bridge.start(timeout=timeout)

        cfg = zenoh.Config()
        cfg.insert_json5("mode", f'"{self.zenoh_mode}"')
        cfg.insert_json5("connect/endpoints", f'["{self._bridge.local_endpoint}"]')
        cfg.insert_json5("scouting/multicast/enabled", "false")
        cfg.insert_json5("scouting/gossip/enabled", "false")

        self._session = zenoh.open(cfg)
        for topic in (
            TOPIC_CTRL_CMD,
            TOPIC_CAMERA_UNIFIED,
            TOPIC_CAMERA_RECORD_REQ,
            TOPIC_CAMERA_CAPTURE,
            TOPIC_VT_REQ_IFRAME,
            TOPIC_AUDIO_PLAYBACK,
        ):
            self._pubs[topic] = self._session.declare_publisher(
                topic, encoding=zenoh.Encoding.APPLICATION_PROTOBUF
            )
        self._sub = self._session.declare_subscriber("device/**", self._on_zenoh_sample)
        self._bridge.send_ready()
        self.telemetry.connected = True
        self.request_iframe()

    def close(self) -> None:
        self.telemetry.connected = False
        if self._sub is not None:
            try:
                self._sub.undeclare()
            except Exception:
                pass
            self._sub = None
        for pub in self._pubs.values():
            try:
                pub.undeclare()
            except Exception:
                pass
        self._pubs.clear()
        if self._session is not None:
            try:
                self._session.close()
            except Exception:
                pass
            self._session = None
        if self._bridge is not None:
            self._bridge.close()
            self._bridge = None

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def _put(self, topic: str, payload: bytes) -> None:
        pub = self._pubs.get(topic)
        if pub is not None:
            try:
                pub.put(payload, encoding=zenoh.Encoding.APPLICATION_PROTOBUF)
            except Exception as exc:
                log.debug("zenoh put %s failed: %s", topic, exc)

    # ---- Video & Audio RX --------------------------------------------------

    def _on_video_frame(self, annexb: bytes) -> None:
        codec_name = self._video_codec_name or _detect_video_codec(annexb)
        if codec_name is None:
            return
        try:
            if self._video_decoder is None or self._video_codec_name != codec_name:
                self._video_decoder = av.CodecContext.create(codec_name, "r")
                self._video_decoder.thread_type = "SLICE"
                self._video_codec_name = codec_name
                self.telemetry.video_codec = codec_name
            pkt = av.Packet(annexb)
            for frame in self._video_decoder.decode(pkt):
                rgb = frame.to_ndarray(format="rgb24")
                with self._lock:
                    self._latest_rgb = rgb
                    self._frame_seq += 1
                    self.telemetry._fps_frames += 1
                    now = time.monotonic()
                    dt = now - self.telemetry._fps_window_t
                    if dt >= 0.5:
                        self.telemetry.video_fps = self.telemetry._fps_frames / dt
                        self.telemetry._fps_frames = 0
                        self.telemetry._fps_window_t = now
        except Exception as exc:
            log.debug("video decode error: %s", exc)

    def _on_audio_packet(self, payload: bytes) -> None:
        if self.audio_muted or self.on_audio_pcm is None:
            return
        try:
            if self._audio_decoder is None:
                self._audio_decoder = av.CodecContext.create("libopus", "r")
                self._audio_decoder.sample_rate = SAMPLE_RATE
                self._audio_decoder.open()
            pkt = av.Packet(payload)
            for frame in self._audio_decoder.decode(pkt):
                for rf in self._audio_resampler.resample(frame):
                    pcm = rf.to_ndarray().reshape(-1).astype(np.int16)
                    self.on_audio_pcm(pcm)
        except Exception as exc:
            log.debug("audio decode error: %s", exc)

    def get_latest_frame(self) -> tuple[np.ndarray | None, int]:
        with self._lock:
            return self._latest_rgb, self._frame_seq

    def refresh_link_stats(self) -> None:
        if self._bridge is not None:
            st = self._bridge.ucp.get_stats()
            self.telemetry.connected = bool(st.get("connected", False))
            self.telemetry.rtt_ms = float(st.get("rtt_ms", 0.0))

    # ---- Telemetry RX ------------------------------------------------------

    def _on_zenoh_sample(self, sample: zenoh.Sample) -> None:
        key = str(sample.key_expr)
        raw = sample.payload.to_bytes()
        try:
            if key == TOPIC_STATUS_PMU:
                msg = PmuOsdStatus.decode(raw)
                self.telemetry.battery_soc = msg.soc
            elif key == TOPIC_STATUS_ROBOT:
                msg = RobotStatus.decode(raw)
                self.telemetry.robot_state = msg.robot_state
                self.telemetry.velocity_mm_s = msg.velocity
                self.telemetry.obstacle_dis_mm = msg.obstacle_dis
                self.telemetry.gimbal_angle_deg = msg.gimbal_angle / 10.0
                self.telemetry.height_mm = msg.height
                self.telemetry.sport_mode = msg.sport_mode
            elif key == TOPIC_STATUS_CAMERA:
                msg = CameraStatus.decode(raw)
                self.telemetry.recording = bool(msg.rec_state or msg.recording_state)
                self.telemetry.rec_duration_s = msg.rec_duration_s
                self.telemetry.eis_enabled = bool(msg.eis_status)
                if msg.vt_width:
                    self.telemetry.vt_width = msg.vt_width
                    self.telemetry.vt_height = msg.vt_height
            elif key == TOPIC_STATUS_STORAGE:
                msg = StorageStatus.decode(raw)
                free_mb = max(0, msg.internal_total_space - msg.internal_used_space)
                self.telemetry.storage_free_gb = free_mb / 1024.0
        except Exception as exc:
            log.debug("telemetry decode error on %s: %s", key, exc)

    # ---- Control TX --------------------------------------------------------

    def send_drive(
        self,
        *,
        vx: float,
        vy: float,
        yaw_rate: float,
        gimbal_pitch: float,
        height: float,
        loco_profile: int = 1,
        jump: bool = False,
    ) -> None:
        cmd = ControlCtrlCmd(
            seq_num=self._next_seq(),
            velocity_x=vx,
            velocity_y=vy,
            yaw_rate=yaw_rate,
            gimbal_pitch=gimbal_pitch,
            height=height,
            loco_profile=loco_profile,
            action_trigger=1 if jump else 0,
            timestamp_ns=time.time_ns(),
        )
        self._put(TOPIC_CTRL_CMD, cmd.encode())

    def toggle_record(self, start: bool) -> None:
        req = CameraRecordReq(
            seq_num=self._next_seq(),
            action=1 if start else 2,
            request_id=int(time.time() * 1000),
        )
        self._put(TOPIC_CAMERA_RECORD_REQ, req.encode())

    def take_photo(self) -> None:
        cmd = CameraCaptureCmd(seq_num=self._next_seq(), camera_capture_data=1)
        self._put(TOPIC_CAMERA_CAPTURE, cmd.encode())

    def set_camera_settings(self, *, eis_enable: bool, eis_level: int = 1, vt_index: int = 0) -> None:
        cmd = CameraUnifiedCmd(
            seq_num=self._next_seq(),
            cmd_type=1,
            eis_enable=1 if eis_enable else 0,
            eis_level=eis_level,
            vt_config_index=vt_index,
        )
        self._put(TOPIC_CAMERA_UNIFIED, cmd.encode())

    def request_iframe(self) -> None:
        self._put(TOPIC_VT_REQ_IFRAME, VtReqIframe(seq_num=self._next_seq()).encode())

    def speak_tts_async(
        self,
        text: str,
        *,
        robot_voice: bool = True,
        ssh_fallback: bool = True,
        local_preview_cb: Callable[[np.ndarray], None] | None = None,
    ) -> None:
        """Synthesize `text` offline and stream Opus frames to Beni (+ optional SSH/local preview)."""
        threading.Thread(
            target=self._speak_tts_worker,
            args=(text, robot_voice, ssh_fallback, local_preview_cb),
            name="beni-tts",
            daemon=True,
        ).start()

    def _speak_tts_worker(
        self,
        text: str,
        robot_voice: bool,
        ssh_fallback: bool,
        local_preview_cb: Callable[[np.ndarray], None] | None,
    ) -> None:
        with self._tts_lock:
            pcm, engine = synthesize_pcm(text, robot_voice=robot_voice)
            self.telemetry.last_tts_text = text
            self.telemetry.last_tts_engine = engine
            if local_preview_cb is not None:
                try:
                    local_preview_cb(pcm)
                except Exception:
                    pass

            # Stream 20 ms Opus frames over Zenoh `app/data/audio/playback_data` & UCP chan 2
            ts_ms = int(time.monotonic() * 1000) & 0xFFFFFFFF
            for opus_pkt in encode_opus_frames(pcm):
                self._audio_seq += 1
                msg = AudioPlaybackData(
                    seq_num=self._next_seq(),
                    encoding_type=1,  # Opus
                    sample_rate=SAMPLE_RATE,
                    channels=1,
                    frame_duration_ms=FRAME_MS,
                    timestamp=ts_ms,
                    seq_num_audio=self._audio_seq,
                    data=opus_pkt,
                )
                self._put(TOPIC_AUDIO_PLAYBACK, msg.encode())
                if self._bridge is not None:
                    self._bridge.ucp.send_audio(opus_pkt)
                ts_ms = (ts_ms + FRAME_MS) & 0xFFFFFFFF
                time.sleep(FRAME_MS / 1000.0)

            # Optional SSH ALSA fallback for custom /md firmware when not on loopback
            if ssh_fallback and self.robot_ip not in ("127.0.0.1", "localhost"):
                play_over_ssh(pcm, robot_ip=self.robot_ip, ssh_port=self.ssh_port)
