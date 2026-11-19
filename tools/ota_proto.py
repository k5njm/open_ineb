#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""
Wire-exact, zero-dependency Protobuf encoder/decoder for Beni (LZ110) OTA control
and progress messages, matching the FileDescriptorProto definitions extracted from
`md_videotrans` / `md_upgrade` into `open_ineb/proto/md/protocol/`.

Wire messages:
  - `md.protocol.cmd.OtaUpgradeControlReq`  (topic: `app/cmd/ota/upgrade_control_req`)
  - `md.protocol.cmd.OtaUpgradeControlRsp`  (topic: `device/cmd/ota/upgrade_control_rsp`)
  - `md.protocol.status.OtaUpgradeProgress` (topic: `device/status/ota/upgrade_progress`)
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum


class OtaControlCmd(IntEnum):
    """Exact `control_cmd` enum values verified from `md_upgrade` (`controller.rs`)."""

    START = 0
    CANCEL = 1
    CONFIRM_COMPLETE = 2
    QUERY = 3
    INSTALL = 4
    CAPABILITY_PROBE = 0x7F


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


def _field_varint(field_num: int, val: int, *, emit_zero: bool = False) -> bytes:
    if not val and not emit_zero:
        return b""
    return _encode_varint((field_num << 3) | 0) + _encode_varint(val)


def _field_bytes(field_num: int, data: bytes) -> bytes:
    if not data:
        return b""
    return _encode_varint((field_num << 3) | 2) + _encode_varint(len(data)) + data


def _field_str(field_num: int, text: str) -> bytes:
    if not text:
        return b""
    return _field_bytes(field_num, text.encode("utf-8"))


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


@dataclass
class WireHeader:
    seq_num: int = 0

    def encode(self) -> bytes:
        return _field_varint(1, self.seq_num)

    @classmethod
    def decode(cls, buf: bytes) -> "WireHeader":
        obj = cls()
        for fnum, _wtype, val in _parse_fields(buf):
            if fnum == 1 and isinstance(val, int):
                obj.seq_num = val
        return obj


@dataclass
class OtaUpgradeTaskContext:
    protocol_version: int = 2
    upgrade_id: str = ""
    device_id: str = ""
    sha256: str = ""
    size: int = 0
    version: str = ""
    original_filename: str = ""

    def encode(self) -> bytes:
        return b"".join([
            _field_varint(1, self.protocol_version),
            _field_str(2, self.upgrade_id),
            _field_str(3, self.device_id),
            _field_str(5, self.sha256),
            _field_varint(6, self.size),
            _field_str(7, self.version),
            _field_str(8, self.original_filename),
        ])

    @classmethod
    def decode(cls, buf: bytes) -> "OtaUpgradeTaskContext":
        obj = cls(protocol_version=0)
        for fnum, _wtype, val in _parse_fields(buf):
            if fnum == 1 and isinstance(val, int):
                obj.protocol_version = val
            elif fnum == 2 and isinstance(val, bytes):
                obj.upgrade_id = val.decode("utf-8", errors="replace")
            elif fnum == 3 and isinstance(val, bytes):
                obj.device_id = val.decode("utf-8", errors="replace")
            elif fnum == 5 and isinstance(val, bytes):
                obj.sha256 = val.decode("utf-8", errors="replace")
            elif fnum == 6 and isinstance(val, int):
                obj.size = val
            elif fnum == 7 and isinstance(val, bytes):
                obj.version = val.decode("utf-8", errors="replace")
            elif fnum == 8 and isinstance(val, bytes):
                obj.original_filename = val.decode("utf-8", errors="replace")
        return obj


@dataclass
class OtaUpgradeControlReqData:
    control_cmd: int = OtaControlCmd.START
    version_field1: int = 0
    version_field2: int = 0
    reserved: int = 0
    task: OtaUpgradeTaskContext | None = None

    def encode(self) -> bytes:
        parts = [
            _field_varint(1, int(self.control_cmd)),
            _field_varint(2, self.version_field1),
            _field_varint(3, self.version_field2),
            _field_varint(4, self.reserved),
        ]
        if self.task is not None:
            parts.append(_field_bytes(5, self.task.encode()))
        return b"".join(parts)

    @classmethod
    def decode(cls, buf: bytes) -> "OtaUpgradeControlReqData":
        obj = cls()
        for fnum, _wtype, val in _parse_fields(buf):
            if fnum == 1 and isinstance(val, int):
                obj.control_cmd = val
            elif fnum == 2 and isinstance(val, int):
                obj.version_field1 = val
            elif fnum == 3 and isinstance(val, int):
                obj.version_field2 = val
            elif fnum == 4 and isinstance(val, int):
                obj.reserved = val
            elif fnum == 5 and isinstance(val, bytes):
                obj.task = OtaUpgradeTaskContext.decode(val)
        return obj


@dataclass
class OtaUpgradeControlReq:
    header: WireHeader | None = None
    data: OtaUpgradeControlReqData | None = None

    def encode(self) -> bytes:
        parts = []
        if self.header is not None:
            parts.append(_field_bytes(1, self.header.encode()))
        if self.data is not None:
            parts.append(_field_bytes(2, self.data.encode()))
        return b"".join(parts)

    @classmethod
    def decode(cls, buf: bytes) -> "OtaUpgradeControlReq":
        obj = cls()
        for fnum, _wtype, val in _parse_fields(buf):
            if fnum == 1 and isinstance(val, bytes):
                obj.header = WireHeader.decode(val)
            elif fnum == 2 and isinstance(val, bytes):
                obj.data = OtaUpgradeControlReqData.decode(val)
        return obj


@dataclass
class OtaUpgradeTaskResult:
    protocol_version: int = 0
    upgrade_id: str = ""
    device_id: str = ""
    state: str = ""
    upload_grant_token: str = ""
    offset: int = 0
    revision: int = 0
    error_code: str = ""

    def encode(self) -> bytes:
        return b"".join([
            _field_varint(1, self.protocol_version),
            _field_str(2, self.upgrade_id),
            _field_str(3, self.device_id),
            _field_str(4, self.state),
            _field_str(5, self.upload_grant_token),
            _field_varint(6, self.offset),
            _field_varint(7, self.revision),
            _field_str(8, self.error_code),
        ])

    @classmethod
    def decode(cls, buf: bytes) -> "OtaUpgradeTaskResult":
        obj = cls()
        for fnum, _wtype, val in _parse_fields(buf):
            if fnum == 1 and isinstance(val, int):
                obj.protocol_version = val
            elif fnum == 2 and isinstance(val, bytes):
                obj.upgrade_id = val.decode("utf-8", errors="replace")
            elif fnum == 3 and isinstance(val, bytes):
                obj.device_id = val.decode("utf-8", errors="replace")
            elif fnum == 4 and isinstance(val, bytes):
                obj.state = val.decode("utf-8", errors="replace")
            elif fnum == 5 and isinstance(val, bytes):
                obj.upload_grant_token = val.decode("utf-8", errors="replace")
            elif fnum == 6 and isinstance(val, int):
                obj.offset = val
            elif fnum == 7 and isinstance(val, int):
                obj.revision = val
            elif fnum == 8 and isinstance(val, bytes):
                obj.error_code = val.decode("utf-8", errors="replace")
        return obj


@dataclass
class OtaUpgradeControlRspData:
    ret_code: int = 0
    current_upgrade_stage: int = 0
    reserved: bytes = b""
    task: OtaUpgradeTaskResult | None = None
    task_context: OtaUpgradeTaskContext | None = None
    task_legacy: int = 0

    def encode(self) -> bytes:
        parts = [
            _field_varint(1, self.ret_code),
            _field_varint(2, self.current_upgrade_stage),
            _field_bytes(3, self.reserved),
        ]
        if self.task is not None:
            parts.append(_field_bytes(4, self.task.encode()))
        if self.task_context is not None:
            parts.append(_field_bytes(5, self.task_context.encode()))
        parts.append(_field_varint(6, self.task_legacy))
        return b"".join(parts)

    @classmethod
    def decode(cls, buf: bytes) -> "OtaUpgradeControlRspData":
        obj = cls()
        for fnum, _wtype, val in _parse_fields(buf):
            if fnum == 1 and isinstance(val, int):
                obj.ret_code = val
            elif fnum == 2 and isinstance(val, int):
                obj.current_upgrade_stage = val
            elif fnum == 3 and isinstance(val, bytes):
                obj.reserved = val
            elif fnum == 4 and isinstance(val, bytes):
                obj.task = OtaUpgradeTaskResult.decode(val)
            elif fnum == 5 and isinstance(val, bytes):
                obj.task_context = OtaUpgradeTaskContext.decode(val)
            elif fnum == 6 and isinstance(val, int):
                obj.task_legacy = val
        return obj


@dataclass
class OtaUpgradeControlRsp:
    header: WireHeader | None = None
    data: OtaUpgradeControlRspData | None = None

    def encode(self) -> bytes:
        parts = []
        if self.header is not None:
            parts.append(_field_bytes(1, self.header.encode()))
        if self.data is not None:
            parts.append(_field_bytes(2, self.data.encode()))
        return b"".join(parts)

    @classmethod
    def decode(cls, buf: bytes) -> "OtaUpgradeControlRsp":
        obj = cls()
        for fnum, _wtype, val in _parse_fields(buf):
            if fnum == 1 and isinstance(val, bytes):
                obj.header = WireHeader.decode(val)
            elif fnum == 2 and isinstance(val, bytes):
                obj.data = OtaUpgradeControlRspData.decode(val)
        return obj


@dataclass
class OtaUpgradeProgressData:
    return_code: int = 0
    upgrade_stage: int = 0
    progress_percent: int = 0
    current_bytes: int = 0
    total_bytes: int = 0
    reserved: bytes = b""

    def encode(self) -> bytes:
        return b"".join([
            _field_varint(1, self.return_code),
            _field_varint(2, self.upgrade_stage),
            _field_varint(3, self.progress_percent),
            _field_varint(4, self.current_bytes),
            _field_varint(5, self.total_bytes),
            _field_bytes(6, self.reserved),
        ])

    @classmethod
    def decode(cls, buf: bytes) -> "OtaUpgradeProgressData":
        obj = cls()
        for fnum, _wtype, val in _parse_fields(buf):
            if fnum == 1 and isinstance(val, int):
                obj.return_code = val
            elif fnum == 2 and isinstance(val, int):
                obj.upgrade_stage = val
            elif fnum == 3 and isinstance(val, int):
                obj.progress_percent = val
            elif fnum == 4 and isinstance(val, int):
                obj.current_bytes = val
            elif fnum == 5 and isinstance(val, int):
                obj.total_bytes = val
            elif fnum == 6 and isinstance(val, bytes):
                obj.reserved = val
        return obj


@dataclass
class OtaUpgradeProgress:
    header: WireHeader | None = None
    data: OtaUpgradeProgressData | None = None

    def encode(self) -> bytes:
        parts = []
        if self.header is not None:
            parts.append(_field_bytes(1, self.header.encode()))
        if self.data is not None:
            parts.append(_field_bytes(2, self.data.encode()))
        return b"".join(parts)

    @classmethod
    def decode(cls, buf: bytes) -> "OtaUpgradeProgress":
        obj = cls()
        for fnum, _wtype, val in _parse_fields(buf):
            if fnum == 1 and isinstance(val, bytes):
                obj.header = WireHeader.decode(val)
            elif fnum == 2 and isinstance(val, bytes):
                obj.data = OtaUpgradeProgressData.decode(val)
        return obj


if __name__ == "__main__":
    ctx = OtaUpgradeTaskContext(
        protocol_version=2,
        upgrade_id="11111111-2222-3333-4444-555555555555",
        device_id="LZ100TEST",
        sha256="deadbeef" * 8,
        size=539412774,
        version="26.10.28.04",
        original_filename="open_ineb_custom.zip",
    )
    req = OtaUpgradeControlReq(
        header=WireHeader(seq_num=1),
        data=OtaUpgradeControlReqData(control_cmd=OtaControlCmd.START, task=ctx),
    )
    raw = req.encode()
    dec = OtaUpgradeControlReq.decode(raw)
    assert dec.header is not None and dec.header.seq_num == 1
    assert dec.data is not None and dec.data.control_cmd == OtaControlCmd.START
    assert dec.data.task is not None and dec.data.task.upgrade_id == ctx.upgrade_id
    print(f"ota_proto self-test OK ({len(raw)} bytes)")
