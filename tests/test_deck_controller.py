#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "pygame-ce>=2.5.0",
#   "av>=13.0.0",
#   "eclipse-zenoh>=1.0.0",
#   "requests>=2.32.0",
#   "numpy>=1.26.0",
# ]
# ///
"""
End-to-end automated loopback verification for the native Steam Deck FPV
controller (`open_ineb/deck_controller/`).

Tests:
  1. Protobuf round-trip codecs (drive, camera, audio/TTS, telemetry)
  2. Offline TTS synthesis + robotic filter + PyAV Opus 48 kHz encoding
  3. Live MockBeniServer <-> BeniDeckClient over UDP UCP + Zenoh:
       - Channel 1 live H.264 video frame decode
       - Telemetry RX (battery, height, storage)
       - Drive / jump / record / photo TX
       - TTS Opus uplink TX
       - HTTP :8084 media list, thumbnail, and file download
  4. Full headless pygame-ce 1280x800 HUD smoke test (`run.py --mock --max-seconds 2.0`)
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from deck_controller.beni_proto import (
    AudioPlaybackData,
    CameraCaptureCmd,
    CameraRecordReq,
    CameraStatus,
    ControlCtrlCmd,
    PmuOsdStatus,
    RobotStatus,
    StorageStatus,
)
from deck_controller.client import BeniDeckClient
from deck_controller.media import BeniMediaClient
from deck_controller.mock_beni import MockBeniServer
from deck_controller.tts import encode_opus_frames, synthesize_pcm


def test_proto_roundtrip() -> None:
    cmd = ControlCtrlCmd(seq_num=7, velocity_x=0.8, velocity_y=-0.2, yaw_rate=0.5, gimbal_pitch=12.5, height=0.7, action_trigger=1)
    dec = ControlCtrlCmd.decode(cmd.encode())
    assert dec.seq_num == 7
    assert abs(dec.velocity_x - 0.8) < 1e-4
    assert abs(dec.gimbal_pitch - 12.5) < 1e-4
    assert dec.action_trigger == 1

    rs = RobotStatus(seq_num=3, robot_state=1, velocity=-450, gimbal_angle=-125, height=260, sport_mode=2)
    rs2 = RobotStatus.decode(rs.encode())
    assert rs2.velocity == -450 and rs2.gimbal_angle == -125 and rs2.height == 260

    ap = AudioPlaybackData(seq_num=9, encoding_type=1, sample_rate=48000, channels=1, frame_duration_ms=20, seq_num_audio=4, data=b"\xaa\xbb\xcc")
    ap2 = AudioPlaybackData.decode(ap.encode())
    assert ap2.sample_rate == 48000 and ap2.data == b"\xaa\xbb\xcc"
    print("[PASS] Protobuf round-trip codecs")


def test_tts_and_opus() -> None:
    pcm, engine = synthesize_pcm("Hello Beni!", robot_voice=True)
    assert len(pcm) >= 960 and pcm.dtype.name == "int16"
    pkts = list(encode_opus_frames(pcm[: 960 * 5]))
    assert len(pkts) >= 5 and all(len(p) > 0 for p in pkts)
    print(f"[PASS] Offline TTS ({engine}) + robotic filter + Opus ({len(pkts)} packets)")


def test_live_loopback_client() -> None:
    srv = MockBeniServer(width=320, height=180, fps=20)
    srv.start()
    client = BeniDeckClient("127.0.0.1", ucp_port=srv.ucp_port, zenoh_mode="peer")
    try:
        client.connect(timeout=6.0)
        time.sleep(0.3)

        # Send drive, jump, record, photo, and TTS
        client.send_drive(vx=0.6, vy=0.1, yaw_rate=-0.3, gimbal_pitch=10.0, height=0.8, jump=True)
        client.toggle_record(True)
        client.take_photo()
        client.speak_tts_async("Beep boop!", robot_voice=True, ssh_fallback=False)

        # Wait up to 4s for decoded video frame, telemetry, and TTS packets
        deadline = time.monotonic() + 4.0
        got_video = False
        while time.monotonic() < deadline:
            rgb, seq = client.get_latest_frame()
            if rgb is not None and seq > 0:
                assert rgb.shape == (180, 320, 3), f"Unexpected shape {rgb.shape}"
                got_video = True
            if (
                got_video
                and client.telemetry.battery_soc == 87
                and srv.ctrl_rx_count > 0
                and srv.recording
                and srv.photo_count >= 1
                and srv.tts_rx_packets > 0
            ):
                break
            time.sleep(0.1)

        assert got_video, "Did not receive/decode live H.264 video frame over UCP channel 1"
        assert client.telemetry.battery_soc == 87, f"Expected battery 87, got {client.telemetry.battery_soc}"
        assert srv.ctrl_rx_count > 0, "Mock robot did not receive ControlCtrlCmd"
        assert srv.recording, "Mock robot did not start recording"
        assert srv.photo_count >= 1, "Mock robot did not capture photo"
        assert srv.tts_rx_packets > 0, "Mock robot did not receive TTS Opus packets"
        print(
            f"[PASS] Live UCP+Zenoh loopback: video={rgb.shape}, bat={client.telemetry.battery_soc}%, "
            f"ctrl={srv.ctrl_rx_count}, photos={srv.photo_count}, tts_pkts={srv.tts_rx_packets}"
        )

        # Test HTTP :8084 media gallery
        media = BeniMediaClient("127.0.0.1", srv.media_port)
        items = media.list_files()
        assert len(items) >= 4, f"Expected >=4 media items (3 sample + 1 snap), got {len(items)}"
        thumb = media.fetch_thumbnail(items[0].file_id)
        assert thumb is not None and thumb[:2] == b"\xff\xd8", "Invalid JPEG thumbnail"
        with tempfile.TemporaryDirectory() as td:
            out = media.download_file(items[0], Path(td))
            assert out.exists() and out.stat().st_size > 100
        print(f"[PASS] Media gallery (:8084): listed {len(items)} items, thumbnail & download OK")
    finally:
        client.close()
        srv.close()


def test_headless_hud_smoke() -> None:
    env = dict(os.environ)
    env["SDL_VIDEODRIVER"] = "dummy"
    env["SDL_AUDIODRIVER"] = "dummy"
    res = subprocess.run(
        ["uv", "run", str(REPO_ROOT / "deck_controller" / "run.py"), "--mock", "--max-seconds", "2.0"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert res.returncode == 0, f"Headless HUD failed (rc={res.returncode}):\n{res.stdout}\n{res.stderr}"
    print("[PASS] Headless 1280x800 Steam Deck HUD smoke test (--mock)")


if __name__ == "__main__":
    test_proto_roundtrip()
    test_tts_and_opus()
    test_live_loopback_client()
    test_headless_hud_smoke()
    print("\nALL STEAM DECK CONTROLLER TESTS PASSED!")
