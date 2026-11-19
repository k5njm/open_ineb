#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "pygame-ce>=2.5.0",
#   "av>=13.0.0",
#   "eclipse-zenoh>=1.0.0",
#   "requests>=2.32.0",
#   "numpy>=1.26.0",
#   "rich>=13.7.0",
#   "questionary>=2.0.1",
#   "cryptography>=42.0.0",
#   "passlib>=1.7.4",
# ]
# ///
"""Automated tests for Issue #1 safety patches, obstacle governor, and pre-flight check."""

from __future__ import annotations

import copy
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "tools"))

from customize_fw import deep_merge_json_patch, load_patch_targets  # noqa: E402
from deck_controller.app import (  # noqa: E402
    OA_HARD_STOP_MM,
    OA_SLOW_START_MM,
    apply_obstacle_speed_governor,
)

STOCK_SAMPLE = {
    "services": {
        "fpv_moe_router": {"gate": {"depth_stale_ms": 200}},
        "local_mapping": {
            "query": {"query_half_width_m": 0.03, "max_snapshot_age_ms": 500}
        },
        "navigation": {
            "nav_diff": {"processor": {"nav_vx_scale": 5.0}},
            "nav_omni": {"processor": {"nav_vx_scale": 5.0}},
        },
        "jump_auto": {"enabled": True},
        "tracking": {
            "fusion_depth_degrade": {
                "hard_age_ms": 500,
                "persist_ms": 1000,
                "decel_mps2": 1.0,
            },
            "action_guard": {
                "stationary_target": {"enabled": False, "max_decel_mps2": 1.0},
                "direction_preserving_velocity_safety": {
                    "enabled": False,
                    "force_stop_distance_m": 0.3,
                    "force_slow_speed_mps": 0.2,
                    "transition_velocity_mps": 0.2,
                    "release_hysteresis_m": 0.1,
                },
            },
        },
        "edge_brake": {"processor": {"nav_vx_scale": 5.0}},
    }
}


def test_safety_patches() -> None:
    p_oa = REPO_ROOT / "patches" / "safety_conservative_oa.json"
    p_nj = REPO_ROOT / "patches" / "safety_no_autojump.json"
    grouped = load_patch_targets([p_oa, p_nj])
    target_key = "etc/md_function_v2/md_function_v2_config_p0.json"
    assert target_key in grouped

    doc = copy.deepcopy(STOCK_SAMPLE)
    diffs = []
    for _name, merge in grouped[target_key]:
        diffs.extend(deep_merge_json_patch(doc, merge))

    assert len(diffs) >= 10
    s = doc["services"]
    assert s["local_mapping"]["query"]["query_half_width_m"] == 0.12
    assert s["local_mapping"]["query"]["max_snapshot_age_ms"] == 150
    assert s["navigation"]["nav_diff"]["processor"]["nav_vx_scale"] == 3.0
    assert s["tracking"]["action_guard"]["direction_preserving_velocity_safety"]["enabled"] is True
    assert s["tracking"]["action_guard"]["direction_preserving_velocity_safety"]["force_stop_distance_m"] == 0.55
    assert s["tracking"]["action_guard"]["stationary_target"]["enabled"] is True
    assert s["tracking"]["fusion_depth_degrade"]["decel_mps2"] == 2.5
    assert s["jump_auto"]["enabled"] is False
    print(f"[PASS] Declarative JSON patches applied cleanly ({len(diffs)} parameter changes verified)")


def test_obstacle_speed_governor() -> None:
    # Far away: untouched
    vx, braking = apply_obstacle_speed_governor(1.0, 2000)
    assert vx == 1.0 and not braking

    # Mid-ramp: linearly scaled down
    mid_mm = (OA_SLOW_START_MM + OA_HARD_STOP_MM) // 2
    vx, braking = apply_obstacle_speed_governor(1.0, mid_mm)
    assert 0.45 < vx < 0.55 and braking

    # Inside hard stop zone: forward clamped to 0.0
    vx, braking = apply_obstacle_speed_governor(1.0, OA_HARD_STOP_MM)
    assert vx == 0.0 and braking

    # Reverse is always allowed even when close to an obstacle
    vx, braking = apply_obstacle_speed_governor(-0.6, 200)
    assert vx == -0.6 and not braking
    print("[PASS] Progressive obstacle speed governor & hard-stop clamp verified")


def test_preflight_mock() -> None:
    res = subprocess.run(
        ["uv", "run", str(REPO_ROOT / "tools" / "preflight_check.py"), "--mock", "--seconds", "1.0"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert res.returncode == 0, f"preflight_check failed (rc={res.returncode}):\n{res.stdout}\n{res.stderr}"
    assert "CONNECTED" in res.stdout and "87%" in res.stdout
    print("[PASS] Read-only pre-flight safety check (--mock) verified")


if __name__ == "__main__":
    test_safety_patches()
    test_obstacle_speed_governor()
    test_preflight_mock()
    print("\nALL SAFETY PATCH & PRE-FLIGHT TESTS PASSED!")
