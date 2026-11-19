"""
Wire-exact, zero-dependency Protobuf codecs for Beni FPV driving, gimbal,
camera, audio/TTS uplink, and telemetry messages, matching `open_ineb/proto/`.

All Zenoh messages wrap `WireHeader` (field 1) + `<Msg>Data` (field 2).
"""

from __future__ import annotations

import struct
from dataclasses import dataclass


# ---- Low-level Protobuf wire helpers ---------------------------------------

def _encode_varint(val: int) -> bytes:
    if val < 0:
        val &= (1 << 64) - 1
    out = bytearray()
    while True:
        b = val & 0x7F
        val >>= 7
        if val:
            out.append(b | 0x80)
        else:
            out.append(b)
            break
    return bytes(out)


def _decode_varint(buf: bytes, pos: int) -> tuple[int, int]:
    val = 0
    shift = 0
    while True:
        b = buf[pos]
        pos += 1
        val |= (b & 0x7F) << shift
        if not (b & 0x80):
            return val, pos
        shift += 7


def _zigzag_encode(n: int) -> int:
    return (n << 1) ^ (n >> 63)


def _zigzag_decode(n: int) -> int:
    return (n >> 1) ^ -(n & 1)


def _f_varint(num: int, val: int) -> bytes:
    if not val:
        return b""
    return _encode_varint((num << 3) | 0) + _encode_varint(val)


def _f_sint(num: int, val: int) -> bytes:
    if not val:
        return b""
    return _encode_varint((num << 3) | 0) + _encode_varint(_zigzag_encode(val))


def _f_float(num: int, val: float) -> bytes:
    if val == 0.0:
        return b""
    return _encode_varint((num << 3) | 5) + struct.pack("<f", val)


def _f_bytes(num: int, data: bytes) -> bytes:
    if not data:
        return b""
    return _encode_varint((num << 3) | 2) + _encode_varint(len(data)) + data


def _parse_fields(buf: bytes) -> list[tuple[int, int, int | bytes]]:
    pos = 0
    end = len(buf)
    fields: list[tuple[int, int, int | bytes]] = []
    while pos < end:
        tag, pos = _decode_varint(buf, pos)
        fnum = tag >> 3
        wtype = tag & 0x07
        if wtype == 0:
            v, pos = _decode_varint(buf, pos)
            fields.append((fnum, wtype, v))
        elif wtype == 2:
            ln, pos = _decode_varint(buf, pos)
            fields.append((fnum, wtype, buf[pos : pos + ln]))
            pos += ln
        elif wtype == 5:
            fields.append((fnum, wtype, buf[pos : pos + 4]))
            pos += 4
        elif wtype == 1:
            fields.append((fnum, wtype, buf[pos : pos + 8]))
            pos += 8
        else:
            raise ValueError(f"Unsupported wire type {wtype}")
    return fields


def _wrap(seq_num: int, data_bytes: bytes) -> bytes:
    hdr = _f_varint(1, seq_num)
    return _f_bytes(1, hdr) + _f_bytes(2, data_bytes)


def _unwrap(buf: bytes) -> tuple[int, bytes]:
    seq = 0
    data = b""
    for fnum, wtype, val in _parse_fields(buf):
        if fnum == 1 and wtype == 2 and isinstance(val, bytes):
            for hf, hw, hv in _parse_fields(val):
                if hf == 1 and hw == 0 and isinstance(hv, int):
                    seq = hv
        elif fnum == 2 and wtype == 2 and isinstance(val, bytes):
            data = val
    return seq, data


# ---- Zenoh Topic Names -----------------------------------------------------

TOPIC_CTRL_CMD = "app/cmd/control/ctrl_cmd"
TOPIC_CAMERA_UNIFIED = "app/cmd/camera/unified_cmd"
TOPIC_CAMERA_RECORD_REQ = "app/cmd/camera/record_req"
TOPIC_CAMERA_CAPTURE = "app/cmd/camera/capture_cmd"
TOPIC_VT_REQ_IFRAME = "app/cmd/vt/req_iframe"
TOPIC_AUDIO_OP_REQ = "app/cmd/audio/op_req"
TOPIC_AUDIO_PLAYBACK = "app/data/audio/playback_data"

TOPIC_STATUS_PMU = "device/status/pmu/osd_status"
TOPIC_STATUS_ROBOT = "device/status/robot/status"
TOPIC_STATUS_CAMERA = "device/status/camera/status"
TOPIC_STATUS_STORAGE = "device/status/storage/status"


# ---- Command Messages ------------------------------------------------------

@dataclass
class ControlCtrlCmd:
    """`md.protocol.cmd.ControlCtrlCmd` (`app/cmd/control/ctrl_cmd`)."""

    seq_num: int = 0
    horizontal_mode: int = 1
    horizontal_frame: int = 1
    velocity_x: float = 0.0
    velocity_y: float = 0.0
    yaw_mode: int = 1
    yaw_rate: float = 0.0
    gimbal_yaw_mode: int = 0
    gimbal_pitch_mode: int = 1
    gimbal_yaw: float = 0.0
    gimbal_yaw_rate: float = 0.0
    gimbal_pitch: float = 0.0
    height: float = 0.5
    pitch_angle: float = 0.0
    roll_angle: float = 0.0
    loco_profile: int = 1  # 0=chill, 1=normal, 2=sport
    action_trigger: int = 0  # 1=jump
    target_state: int = 0
    jump_height: float = 0.18
    jump_distance: float = 0.0
    timestamp_ns: int = 0

    def encode(self) -> bytes:
        body = b"".join([
            _f_varint(1, self.horizontal_mode),
            _f_varint(2, self.horizontal_frame),
            _f_float(3, self.velocity_x),
            _f_float(4, self.velocity_y),
            _f_varint(7, self.yaw_mode),
            _f_float(8, self.yaw_rate),
            _f_varint(10, self.gimbal_yaw_mode),
            _f_varint(11, self.gimbal_pitch_mode),
            _f_float(12, self.gimbal_yaw),
            _f_float(13, self.gimbal_yaw_rate),
            _f_float(14, self.gimbal_pitch),
            _f_float(15, self.height),
            _f_float(16, self.pitch_angle),
            _f_float(17, self.roll_angle),
            _f_varint(18, self.loco_profile),
            _f_varint(19, self.action_trigger),
            _f_varint(20, self.target_state),
            _f_float(21, self.jump_height),
            _f_float(22, self.jump_distance),
            _f_varint(23, self.timestamp_ns),
        ])
        return _wrap(self.seq_num, body)

    @classmethod
    def decode(cls, buf: bytes) -> "ControlCtrlCmd":
        seq, body = _unwrap(buf)
        obj = cls(seq_num=seq, horizontal_mode=0, horizontal_frame=0, yaw_mode=0, gimbal_pitch_mode=0, height=0.0, loco_profile=0, jump_height=0.0)
        for fnum, wtype, val in _parse_fields(body):
            if fnum == 1 and isinstance(val, int):
                obj.horizontal_mode = val
            elif fnum == 2 and isinstance(val, int):
                obj.horizontal_frame = val
            elif fnum == 3 and isinstance(val, bytes):
                obj.velocity_x = struct.unpack("<f", val)[0]
            elif fnum == 4 and isinstance(val, bytes):
                obj.velocity_y = struct.unpack("<f", val)[0]
            elif fnum == 7 and isinstance(val, int):
                obj.yaw_mode = val
            elif fnum == 8 and isinstance(val, bytes):
                obj.yaw_rate = struct.unpack("<f", val)[0]
            elif fnum == 10 and isinstance(val, int):
                obj.gimbal_yaw_mode = val
            elif fnum == 11 and isinstance(val, int):
                obj.gimbal_pitch_mode = val
            elif fnum == 12 and isinstance(val, bytes):
                obj.gimbal_yaw = struct.unpack("<f", val)[0]
            elif fnum == 13 and isinstance(val, bytes):
                obj.gimbal_yaw_rate = struct.unpack("<f", val)[0]
            elif fnum == 14 and isinstance(val, bytes):
                obj.gimbal_pitch = struct.unpack("<f", val)[0]
            elif fnum == 15 and isinstance(val, bytes):
                obj.height = struct.unpack("<f", val)[0]
            elif fnum == 16 and isinstance(val, bytes):
                obj.pitch_angle = struct.unpack("<f", val)[0]
            elif fnum == 17 and isinstance(val, bytes):
                obj.roll_angle = struct.unpack("<f", val)[0]
            elif fnum == 18 and isinstance(val, int):
                obj.loco_profile = val
            elif fnum == 19 and isinstance(val, int):
                obj.action_trigger = val
            elif fnum == 20 and isinstance(val, int):
                obj.target_state = val
            elif fnum == 21 and isinstance(val, bytes):
                obj.jump_height = struct.unpack("<f", val)[0]
            elif fnum == 22 and isinstance(val, bytes):
                obj.jump_distance = struct.unpack("<f", val)[0]
            elif fnum == 23 and isinstance(val, int):
                obj.timestamp_ns = val
        return obj


@dataclass
class CameraRecordReq:
    """`md.protocol.cmd.CameraRecordReq` (`app/cmd/camera/record_req`)."""

    seq_num: int = 0
    action: int = 1  # 1=start, 2=stop
    request_id: int = 0

    def encode(self) -> bytes:
        body = _f_varint(1, self.action) + _f_varint(2, self.request_id)
        return _wrap(self.seq_num, body)

    @classmethod
    def decode(cls, buf: bytes) -> "CameraRecordReq":
        seq, body = _unwrap(buf)
        obj = cls(seq_num=seq, action=0)
        for fnum, _w, val in _parse_fields(body):
            if fnum == 1 and isinstance(val, int):
                obj.action = val
            elif fnum == 2 and isinstance(val, int):
                obj.request_id = val
        return obj


@dataclass
class CameraCaptureCmd:
    """`md.protocol.cmd.CameraCaptureCmd` (`app/cmd/camera/capture_cmd`)."""

    seq_num: int = 0
    camera_capture_data: int = 1

    def encode(self) -> bytes:
        return _wrap(self.seq_num, _f_varint(1, self.camera_capture_data))

    @classmethod
    def decode(cls, buf: bytes) -> "CameraCaptureCmd":
        seq, body = _unwrap(buf)
        obj = cls(seq_num=seq, camera_capture_data=0)
        for fnum, _w, val in _parse_fields(body):
            if fnum == 1 and isinstance(val, int):
                obj.camera_capture_data = val
        return obj


@dataclass
class CameraUnifiedCmd:
    """`md.protocol.cmd.CameraUnifiedCmd` (`app/cmd/camera/unified_cmd`)."""

    seq_num: int = 0
    cmd_type: int = 0
    trigger_flags: int = 0
    eis_enable: int = 1
    eis_level: int = 1
    record_profile_id: int = 2
    vt_config_index: int = 0

    def encode(self) -> bytes:
        body = b"".join([
            _f_varint(1, self.cmd_type),
            _f_varint(2, self.trigger_flags),
            _f_varint(4, self.eis_enable),
            _f_varint(5, self.eis_level),
            _f_varint(9, self.record_profile_id),
            _f_varint(10, self.vt_config_index),
        ])
        return _wrap(self.seq_num, body)

    @classmethod
    def decode(cls, buf: bytes) -> "CameraUnifiedCmd":
        seq, body = _unwrap(buf)
        obj = cls(seq_num=seq, eis_enable=0, eis_level=0, record_profile_id=0)
        for fnum, _w, val in _parse_fields(body):
            if fnum == 1 and isinstance(val, int):
                obj.cmd_type = val
            elif fnum == 2 and isinstance(val, int):
                obj.trigger_flags = val
            elif fnum == 4 and isinstance(val, int):
                obj.eis_enable = val
            elif fnum == 5 and isinstance(val, int):
                obj.eis_level = val
            elif fnum == 9 and isinstance(val, int):
                obj.record_profile_id = val
            elif fnum == 10 and isinstance(val, int):
                obj.vt_config_index = val
        return obj


@dataclass
class VtReqIframe:
    """`md.protocol.cmd.VtReqIframe` (`app/cmd/vt/req_iframe`)."""

    seq_num: int = 0
    version: int = 1

    def encode(self) -> bytes:
        return _wrap(self.seq_num, _f_varint(1, self.version))


@dataclass
class AudioPlaybackData:
    """`md.protocol.data.AudioPlaybackData` (`app/data/audio/playback_data`)."""

    seq_num: int = 0
    encoding_type: int = 1  # 1=Opus, 0=PCM
    sample_rate: int = 48000
    channels: int = 1
    frame_duration_ms: int = 20
    timestamp: int = 0
    seq_num_audio: int = 0
    data: bytes = b""

    def encode(self) -> bytes:
        body = b"".join([
            _f_varint(1, self.encoding_type),
            _f_varint(2, self.sample_rate),
            _f_varint(3, self.channels),
            _f_varint(4, self.frame_duration_ms),
            _f_varint(5, self.timestamp),
            _f_varint(6, self.seq_num_audio),
            _f_varint(7, len(self.data)),
            _f_bytes(8, self.data),
        ])
        return _wrap(self.seq_num, body)

    @classmethod
    def decode(cls, buf: bytes) -> "AudioPlaybackData":
        seq, body = _unwrap(buf)
        obj = cls(seq_num=seq, encoding_type=0, sample_rate=0, channels=0, frame_duration_ms=0)
        for fnum, _w, val in _parse_fields(body):
            if fnum == 1 and isinstance(val, int):
                obj.encoding_type = val
            elif fnum == 2 and isinstance(val, int):
                obj.sample_rate = val
            elif fnum == 3 and isinstance(val, int):
                obj.channels = val
            elif fnum == 4 and isinstance(val, int):
                obj.frame_duration_ms = val
            elif fnum == 5 and isinstance(val, int):
                obj.timestamp = val
            elif fnum == 6 and isinstance(val, int):
                obj.seq_num_audio = val
            elif fnum == 8 and isinstance(val, bytes):
                obj.data = val
        return obj


# ---- Telemetry Status Messages ---------------------------------------------

@dataclass
class PmuOsdStatus:
    """`md.protocol.status.PmuOsdStatus` (`device/status/pmu/osd_status`)."""

    seq_num: int = 0
    soc: int = 0
    power_bits: int = 0
    abnormal_flags: int = 0

    def encode(self) -> bytes:
        body = _f_varint(1, self.soc) + _f_varint(2, self.power_bits) + _f_varint(3, self.abnormal_flags)
        return _wrap(self.seq_num, body)

    @classmethod
    def decode(cls, buf: bytes) -> "PmuOsdStatus":
        seq, body = _unwrap(buf)
        obj = cls(seq_num=seq)
        for fnum, _w, val in _parse_fields(body):
            if fnum == 1 and isinstance(val, int):
                obj.soc = val
            elif fnum == 2 and isinstance(val, int):
                obj.power_bits = val
            elif fnum == 3 and isinstance(val, int):
                obj.abnormal_flags = val
        return obj


@dataclass
class RobotStatus:
    """`md.protocol.status.RobotStatus` (`device/status/robot/status`)."""

    seq_num: int = 0
    robot_state: int = 0
    velocity: int = 0
    trip_odom_dis: float = 0.0
    obstacle_dis: int = 0
    pitch_angle: int = 0
    gimbal_angle: int = 0
    roll_angle: int = 0
    height: int = 0
    sport_mode: int = 0
    abnormal_flags: int = 0
    session_odom_dis: float = 0.0

    def encode(self) -> bytes:
        body = b"".join([
            _f_varint(1, self.robot_state),
            _f_sint(2, self.velocity),
            _f_float(3, self.trip_odom_dis),
            _f_varint(4, self.obstacle_dis),
            _f_sint(5, self.pitch_angle),
            _f_sint(6, self.gimbal_angle),
            _f_sint(7, self.roll_angle),
            _f_varint(8, self.height),
            _f_varint(9, self.sport_mode),
            _f_varint(10, self.abnormal_flags),
            _f_float(11, self.session_odom_dis),
        ])
        return _wrap(self.seq_num, body)

    @classmethod
    def decode(cls, buf: bytes) -> "RobotStatus":
        seq, body = _unwrap(buf)
        obj = cls(seq_num=seq)
        for fnum, _w, val in _parse_fields(body):
            if fnum == 1 and isinstance(val, int):
                obj.robot_state = val
            elif fnum == 2 and isinstance(val, int):
                obj.velocity = _zigzag_decode(val)
            elif fnum == 3 and isinstance(val, bytes):
                obj.trip_odom_dis = struct.unpack("<f", val)[0]
            elif fnum == 4 and isinstance(val, int):
                obj.obstacle_dis = val
            elif fnum == 5 and isinstance(val, int):
                obj.pitch_angle = _zigzag_decode(val)
            elif fnum == 6 and isinstance(val, int):
                obj.gimbal_angle = _zigzag_decode(val)
            elif fnum == 7 and isinstance(val, int):
                obj.roll_angle = _zigzag_decode(val)
            elif fnum == 8 and isinstance(val, int):
                obj.height = val
            elif fnum == 9 and isinstance(val, int):
                obj.sport_mode = val
            elif fnum == 10 and isinstance(val, int):
                obj.abnormal_flags = val
            elif fnum == 11 and isinstance(val, bytes):
                obj.session_odom_dis = struct.unpack("<f", val)[0]
        return obj


@dataclass
class CameraStatus:
    """`md.protocol.status.CameraStatus` (`device/status/camera/status`)."""

    seq_num: int = 0
    rec_state: int = 0
    cur_rec_width: int = 3840
    cur_rec_height: int = 2160
    rec_frame_rate: int = 30
    vt_width: int = 1280
    vt_height: int = 720
    vt_frame_rate: int = 30
    eis_status: int = 1
    rec_duration_s: int = 0
    recording_state: int = 0

    def encode(self) -> bytes:
        body = b"".join([
            _f_varint(1, self.rec_state),
            _f_varint(2, self.cur_rec_width),
            _f_varint(3, self.cur_rec_height),
            _f_varint(9, self.rec_frame_rate),
            _f_varint(10, self.vt_width),
            _f_varint(11, self.vt_height),
            _f_varint(12, self.vt_frame_rate),
            _f_varint(14, self.eis_status),
            _f_varint(19, self.rec_duration_s),
            _f_varint(26, self.recording_state),
        ])
        return _wrap(self.seq_num, body)

    @classmethod
    def decode(cls, buf: bytes) -> "CameraStatus":
        seq, body = _unwrap(buf)
        obj = cls(seq_num=seq, cur_rec_width=0, cur_rec_height=0, rec_frame_rate=0, vt_width=0, vt_height=0, vt_frame_rate=0, eis_status=0)
        for fnum, _w, val in _parse_fields(body):
            if fnum == 1 and isinstance(val, int):
                obj.rec_state = val
            elif fnum == 2 and isinstance(val, int):
                obj.cur_rec_width = val
            elif fnum == 3 and isinstance(val, int):
                obj.cur_rec_height = val
            elif fnum == 9 and isinstance(val, int):
                obj.rec_frame_rate = val
            elif fnum == 10 and isinstance(val, int):
                obj.vt_width = val
            elif fnum == 11 and isinstance(val, int):
                obj.vt_height = val
            elif fnum == 12 and isinstance(val, int):
                obj.vt_frame_rate = val
            elif fnum == 14 and isinstance(val, int):
                obj.eis_status = val
            elif fnum == 19 and isinstance(val, int):
                obj.rec_duration_s = val
            elif fnum == 26 and isinstance(val, int):
                obj.recording_state = val
        return obj


@dataclass
class StorageStatus:
    """`md.protocol.status.StorageStatus` (`device/status/storage/status`)."""

    seq_num: int = 0
    internal_used_space: int = 0
    internal_total_space: int = 0
    sd_used_space: int = 0
    sd_total_space: int = 0

    def encode(self) -> bytes:
        body = b"".join([
            _f_varint(1, self.internal_used_space),
            _f_varint(2, self.internal_total_space),
            _f_varint(3, self.sd_used_space),
            _f_varint(4, self.sd_total_space),
        ])
        return _wrap(self.seq_num, body)

    @classmethod
    def decode(cls, buf: bytes) -> "StorageStatus":
        seq, body = _unwrap(buf)
        obj = cls(seq_num=seq)
        for fnum, _w, val in _parse_fields(body):
            if fnum == 1 and isinstance(val, int):
                obj.internal_used_space = val
            elif fnum == 2 and isinstance(val, int):
                obj.internal_total_space = val
            elif fnum == 3 and isinstance(val, int):
                obj.sd_used_space = val
            elif fnum == 4 and isinstance(val, int):
                obj.sd_total_space = val
        return obj
