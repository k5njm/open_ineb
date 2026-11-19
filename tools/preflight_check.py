#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "eclipse-zenoh>=1.0.0",
#   "rich>=13.7.0",
#   "av>=13.0.0",
#   "numpy>=1.26.0",
# ]
# ///
"""
Read-only pre-flight safety check for Mondo Robotics Beni (Issue #1).

Connects over UCP + Zenoh without sending any drive commands, collects live
telemetry (`PmuOsdStatus`, `RobotStatus`, `CameraStatus`, `StorageStatus`),
and reports battery, speed/sport mode, obstacle distance reading, and UCP
link health before driving.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from deck_controller.client import BeniDeckClient  # noqa: E402
from deck_controller.mock_beni import MockBeniServer  # noqa: E402

SPORT_MODES = {0: "CHILL", 1: "NORMAL", 2: "SPORT"}


def run_preflight(
    robot_ip: str = "192.168.7.1",
    *,
    ucp_port: int = 9007,
    zenoh_mode: str = "peer",
    sample_seconds: float = 2.0,
    mock: bool = False,
) -> int:
    console = Console()
    mock_srv: MockBeniServer | None = None
    if mock:
        mock_srv = MockBeniServer(width=320, height=180, fps=10)
        mock_srv.start()
        robot_ip = "127.0.0.1"
        ucp_port = mock_srv.ucp_port

    client = BeniDeckClient(robot_ip=robot_ip, ucp_port=ucp_port, zenoh_mode=zenoh_mode)
    try:
        console.print(f"[blue]Connecting read-only pre-flight check to {robot_ip}:{ucp_port}...[/blue]")
        client.connect(timeout=6.0)
        deadline = time.monotonic() + sample_seconds
        while time.monotonic() < deadline:
            client.refresh_link_stats()
            time.sleep(0.1)

        t = client.telemetry
        warnings: list[str] = []
        if not t.connected:
            warnings.append("UCP link disconnected.")
        if t.rtt_ms > 120.0:
            warnings.append(f"High UCP RTT ({t.rtt_ms:.1f} ms) — watch for sluggish braking.")
        if t.battery_soc > 0 and t.battery_soc < 20:
            warnings.append(f"Low battery ({t.battery_soc}%).")
        if t.sport_mode >= 2:
            warnings.append("Robot is in SPORT mode — high speeds exceed 2.0 m stereo OA stopping range (Issue #1).")
        if t.obstacle_dis_mm == 0:
            warnings.append("Obstacle distance reading is 0 mm (no active OA telemetry received yet).")
        elif t.obstacle_dis_mm < 600:
            warnings.append(f"Close obstacle detected ahead ({t.obstacle_dis_mm / 1000.0:.2f} m).")

        tbl = Table(title=f"Beni Pre-Flight Safety Check ({'MOCK' if mock else robot_ip})")
        tbl.add_column("Metric")
        tbl.add_column("Value")
        tbl.add_row("UCP Link", "[green]CONNECTED[/green]" if t.connected else "[red]OFFLINE[/red]")
        tbl.add_row("UCP RTT", f"{t.rtt_ms:.1f} ms")
        tbl.add_row("Battery SoC", f"{t.battery_soc}%")
        tbl.add_row("Speed / Sport Mode", SPORT_MODES.get(t.sport_mode, str(t.sport_mode)))
        tbl.add_row("Obstacle Distance", f"{t.obstacle_dis_mm / 1000.0:.2f} m ({t.obstacle_dis_mm} mm)")
        tbl.add_row("Standing Height", f"{t.height_mm} mm")
        tbl.add_row("EIS Stabilization", "ON" if t.eis_enabled else "OFF")
        tbl.add_row("Free Internal Storage", f"{t.storage_free_gb:.1f} GB")
        console.print(tbl)

        if warnings:
            console.print(
                Panel.fit(
                    "\n".join(f"- [yellow]{w}[/yellow]" for w in warnings),
                    title="Pre-Flight Warnings",
                    border_style="yellow",
                )
            )
            return 2

        console.print(
            Panel.fit("[bold green]All pre-flight checks nominal.[/bold green]", border_style="green")
        )
        return 0
    finally:
        client.close()
        if mock_srv is not None:
            mock_srv.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only pre-flight safety check for Beni")
    parser.add_argument("--robot-ip", default="192.168.7.1", help="Beni IP (default: 192.168.7.1)")
    parser.add_argument("--ucp-port", type=int, default=9007, help="Beni UCP UDP port (default: 9007)")
    parser.add_argument("--zenoh-mode", choices=["peer", "client"], default="peer")
    parser.add_argument("--seconds", type=float, default=1.5, help="Telemetry sampling duration (s)")
    parser.add_argument("--mock", action="store_true", help="Run against built-in Mock Beni simulator")
    args = parser.parse_args()
    return run_preflight(
        args.robot_ip,
        ucp_port=args.ucp_port,
        zenoh_mode=args.zenoh_mode,
        sample_seconds=args.seconds,
        mock=args.mock,
    )


if __name__ == "__main__":
    raise SystemExit(main())
