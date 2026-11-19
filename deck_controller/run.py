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
"""CLI entry point for the open_ineb native Steam Deck FPV controller."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from deck_controller.app import DeckControllerApp  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description="open_ineb Native Steam Deck FPV Controller for Mondo Robotics Beni"
    )
    parser.add_argument("--robot-ip", default="192.168.7.1", help="Beni IP (default: 192.168.7.1)")
    parser.add_argument("--ucp-port", type=int, default=9007, help="Beni UCP UDP port (default: 9007)")
    parser.add_argument("--media-port", type=int, default=8084, help="Beni media HTTP port (default: 8084)")
    parser.add_argument("--zenoh-mode", choices=["peer", "client"], default="peer", help="Zenoh mode")
    parser.add_argument("--mock", action="store_true", help="Run built-in loopback Mock Beni simulator")
    parser.add_argument("--fullscreen", action="store_true", help="Launch fullscreen (recommended on Steam Deck)")
    parser.add_argument("--download-dir", type=Path, default=None, help="Directory for downloaded media")
    parser.add_argument("--max-seconds", type=float, default=None, help="Auto-exit after N seconds (for testing)")
    args = parser.parse_args()

    app = DeckControllerApp(
        robot_ip=args.robot_ip,
        ucp_port=args.ucp_port,
        media_port=args.media_port,
        zenoh_mode=args.zenoh_mode,
        mock=args.mock,
        fullscreen=args.fullscreen,
        download_dir=args.download_dir,
    )
    return app.run(max_seconds=args.max_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
