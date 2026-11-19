#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "rich>=13.7.0",
#   "questionary>=2.0.1",
#   "cryptography>=42.0.0",
#   "passlib>=1.7.4",
#   "requests>=2.31.0",
#   "eclipse-zenoh>=1.0.0",
# ]
# ///
"""
open_ineb Custom Firmware TUI Builder & Local OTA Uploader for Mondo Robotics Beni (LZ110 / X5).

Creates a brick-resistant, mondo.img-only A/B OTA package from a stock Beni firmware ZIP:
  - Never modifies miniboot, uboot, vbmeta, boot, system, private, or MCU firmwares
  - Optional custom root password (bind-mounted /etc/shadow patch at boot; never stored in plaintext)
  - Optional SSH authorized_keys + key-only/password sshd on port 2222
  - Optional generated Ed25519 SSH host/client keypair for Beni
  - Optional USB-C ADB unlock (removes /md/etc/authorized_debug_profile + injects adbkey.pub)
  - Optional local HTTP OTA upload to 192.168.7.1:8080/v2/upgrades/{uuid}/upload
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
import zipfile
from dataclasses import dataclass

import questionary
import requests
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from passlib.hash import sha512_crypt
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

console = Console()

STOCK_AUTOSTART_SERVICES = [
    "md_sys",
    "md_relay",
    "md_hmsd",
    "md_blackbox",
    "md_analytics",
    "md_netmanager",
    "md_videotrans",
    "md_camera",
    "md_audio",
    "md_control",
    "md_pnl",
    "md_function_v2",
    "md_media_server",
    "md_pose_estimate",
    "md_aruco_localization",
    "md_gateway",
    "md_upgrade",
    "md_snapback",
]


@dataclass
class CustomizationConfig:
    input_zip: pathlib.Path
    output_zip: pathlib.Path
    keys_out_dir: pathlib.Path
    root_password_hash: str | None = None
    ssh_authorized_key: str | None = None
    generate_beni_host_key: bool = True
    generate_beni_client_key: bool = False
    ssh_port: int = 2222
    enable_adb: bool = True
    adb_pubkey: str | None = None
    upload_ota: bool = False
    robot_ip: str = "192.168.7.1"
    robot_ota_port: int = 8080


def sha256_file(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def md5_file(path: pathlib.Path) -> str:
    h = hashlib.md5()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def generate_ed25519_keypair(comment: str) -> tuple[bytes, bytes]:
    priv = ed25519.Ed25519PrivateKey.generate()
    priv_pem = priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.OpenSSH,
        encryption_algorithm=serialization.NoEncryption(),
    )
    pub_openssh = (
        priv.public_key().public_bytes(
            encoding=serialization.Encoding.OpenSSH,
            format=serialization.PublicFormat.OpenSSH,
        )
        + f" {comment}\n".encode("utf-8")
    )
    return priv_pem, pub_openssh


def build_init_script(cfg: CustomizationConfig) -> str:
    allow_pwd = "yes" if cfg.root_password_hash else "no"
    lines = [
        "#!/bin/sh",
        "# open_ineb Phase 1 bootstrap daemon (launched by mdctl.sh as root)",
        "OPEN_INEB_DIR=/md/etc/open_ineb",
        "mkdir -p /run/open_ineb /var/empty",
        "",
        "# 1. Optional custom root password (bind-mount patched /etc/shadow over ro rootfs)",
        'if [ -f "$OPEN_INEB_DIR/root_shadow_hash" ]; then',
        '    NEW_HASH=$(cat "$OPEN_INEB_DIR/root_shadow_hash")',
        '    if [ -n "$NEW_HASH" ] && [ -r /etc/shadow ]; then',
        '        awk -F: -v h="$NEW_HASH" \'BEGIN{OFS=":"} $1=="root"{$2=h} {print}\' /etc/shadow > /run/open_ineb/shadow',
        "        chmod 600 /run/open_ineb/shadow",
        "        mount --bind /run/open_ineb/shadow /etc/shadow 2>/dev/null || true",
        "    fi",
        "fi",
        "",
        "# 2. Optional ADB public key injection into tmpfs /data/misc/adb/adb_keys",
        'if [ -f "$OPEN_INEB_DIR/adb_keys" ]; then',
        "    mkdir -p /data/misc/adb",
        '    cp "$OPEN_INEB_DIR/adb_keys" /data/misc/adb/adb_keys',
        "    chmod 644 /data/misc/adb/adb_keys",
        "fi",
        "",
        "# 3. Optional Beni client SSH keypair exposed under /run/open_ineb/ssh",
        'if [ -f "$OPEN_INEB_DIR/id_ed25519" ]; then',
        "    chmod 600 \"$OPEN_INEB_DIR/id_ed25519\"",
        "fi",
        "",
    ]

    if cfg.ssh_authorized_key or cfg.root_password_hash:
        host_key_arg = (
            '-h "$OPEN_INEB_DIR/ssh_host_ed25519_key"'
            if cfg.generate_beni_host_key
            else "-h /etc/ssh/ssh_host_ed25519_key"
        )
        auth_keys_arg = (
            '-o AuthorizedKeysFile="$OPEN_INEB_DIR/authorized_keys"'
            if cfg.ssh_authorized_key
            else "-o PubkeyAuthentication=no"
        )
        permit_root = "yes" if cfg.root_password_hash else "prohibit-password"
        lines += [
            f"# 4. Run dedicated sshd in foreground on port {cfg.ssh_port} so mdctl tracks it cleanly",
            'if [ -f "$OPEN_INEB_DIR/ssh_host_ed25519_key" ]; then',
            '    chmod 600 "$OPEN_INEB_DIR/ssh_host_ed25519_key"',
            "fi",
            'if [ -f "$OPEN_INEB_DIR/authorized_keys" ]; then',
            '    chmod 600 "$OPEN_INEB_DIR/authorized_keys"',
            "fi",
            f"exec /usr/sbin/sshd -D -p {cfg.ssh_port} \\",
            f"    {host_key_arg} \\",
            f"    {auth_keys_arg} \\",
            f"    -o PasswordAuthentication={allow_pwd} \\",
            f"    -o PermitRootLogin={permit_root} \\",
            "    -o PermitEmptyPasswords=no",
        ]
    else:
        lines += [
            "# No SSH daemon requested; keep process alive for mdctl fanout health check",
            "while true; do sleep 3600; done",
        ]

    return "\n".join(lines) + "\n"


def discover_firmware_zips() -> list[pathlib.Path]:
    candidates: list[pathlib.Path] = []
    for base in [pathlib.Path("../work/firmware"), pathlib.Path("."), pathlib.Path("..")]:
        if base.exists():
            candidates.extend(sorted(base.glob("*.zip")))
    # Deduplicate preserving order
    seen: set[pathlib.Path] = set()
    uniq: list[pathlib.Path] = []
    for p in candidates:
        rp = p.resolve()
        if rp not in seen:
            seen.add(rp)
            uniq.append(p)
    return uniq


def run_tui(args: argparse.Namespace) -> CustomizationConfig:
    console.print(
        Panel.fit(
            "[bold cyan]open_ineb Custom Firmware Builder[/bold cyan]\n"
            "[dim]Brick-resistant mondo.img-only A/B OTA packager for Beni (LZ110 / X5)[/dim]",
            border_style="cyan",
        )
    )

    # 1. Input stock firmware ZIP
    if args.input:
        input_zip = pathlib.Path(args.input).expanduser().resolve()
    else:
        zips = discover_firmware_zips()
        choices = [str(z) for z in zips] + ["Enter custom path..."]
        picked = questionary.select("Select stock Beni firmware ZIP:", choices=choices).ask()
        if not picked:
            raise SystemExit(1)
        if picked == "Enter custom path...":
            picked = questionary.path("Path to stock firmware ZIP:").ask()
        input_zip = pathlib.Path(picked).expanduser().resolve()

    if not input_zip.exists():
        raise FileNotFoundError(f"Firmware ZIP not found: {input_zip}")

    # 2. Root password (optional)
    root_hash = None
    if questionary.confirm("Set a custom root password (applied via bind-mounted /etc/shadow)?", default=False).ask():
        while True:
            p1 = questionary.password("Enter new root password:").ask()
            p2 = questionary.password("Confirm new root password:").ask()
            if p1 and p1 == p2:
                root_hash = sha512_crypt.hash(p1)
                break
            console.print("[red]Passwords did not match or were empty. Try again.[/red]")

    # 3. SSH authorized_keys (optional)
    ssh_auth_key = None
    if questionary.confirm("Install an SSH authorized_keys public key for passwordless root login?", default=True).ask():
        default_pubs = [
            p for p in [
                pathlib.Path.home() / ".ssh/id_ed25519.pub",
                pathlib.Path.home() / ".ssh/id_rsa.pub",
            ]
            if p.exists()
        ]
        key_choices = [str(p) for p in default_pubs] + ["Paste public key manually", "Enter file path..."]
        kp = questionary.select("Select SSH public key:", choices=key_choices).ask()
        if kp == "Paste public key manually":
            ssh_auth_key = (questionary.text("Paste SSH public key line:").ask() or "").strip()
        elif kp == "Enter file path...":
            fp = questionary.path("Path to .pub file:").ask()
            ssh_auth_key = pathlib.Path(fp).expanduser().read_text().strip()
        elif kp:
            ssh_auth_key = pathlib.Path(kp).read_text().strip()

    ssh_port = 2222
    gen_host_key = False
    if ssh_auth_key or root_hash:
        port_str = questionary.text("SSH port for open_ineb sshd:", default="2222").ask()
        ssh_port = int(port_str or "2222")
        gen_host_key = bool(
            questionary.confirm("Generate a persistent Ed25519 SSH host keypair for Beni?", default=True).ask()
        )

    gen_client_key = bool(
        questionary.confirm("Generate an Ed25519 SSH client keypair on Beni itself (~/.ssh/id_ed25519)?", default=False).ask()
    )

    # 4. USB-C ADB (optional)
    enable_adb = bool(
        questionary.confirm("Enable USB-C ADB (removes /md/etc/authorized_debug_profile)?", default=True).ask()
    )
    adb_pubkey = None
    if enable_adb:
        default_adb_pub = pathlib.Path.home() / ".android/adbkey.pub"
        if default_adb_pub.exists() and questionary.confirm(
            f"Inject local ADB public key ({default_adb_pub}) into /data/misc/adb/adb_keys?", default=True
        ).ask():
            adb_pubkey = default_adb_pub.read_text().strip()

    # 5. Output paths
    default_out = pathlib.Path(args.output) if args.output else input_zip.with_name(f"open_ineb_{input_zip.name}")
    out_str = questionary.text("Output custom OTA ZIP path:", default=str(default_out)).ask()
    output_zip = pathlib.Path(out_str).expanduser().resolve()
    keys_out_dir = output_zip.parent / f"{output_zip.stem}_keys"

    # 6. Local OTA upload?
    upload_ota = bool(
        questionary.confirm("Upload custom OTA ZIP to Beni over local Wi-Fi (192.168.7.1:8080) after building?", default=False).ask()
    )
    robot_ip = "192.168.7.1"
    if upload_ota:
        robot_ip = questionary.text("Beni IP address:", default="192.168.7.1").ask() or "192.168.7.1"

    return CustomizationConfig(
        input_zip=input_zip,
        output_zip=output_zip,
        keys_out_dir=keys_out_dir,
        root_password_hash=root_hash,
        ssh_authorized_key=ssh_auth_key,
        generate_beni_host_key=gen_host_key,
        generate_beni_client_key=gen_client_key,
        ssh_port=ssh_port,
        enable_adb=enable_adb,
        adb_pubkey=adb_pubkey,
        upload_ota=upload_ota,
        robot_ip=robot_ip,
    )


def repack_firmware(cfg: CustomizationConfig) -> None:
    table = Table(title="Customization Summary", show_header=True)
    table.add_column("Setting", style="cyan")
    table.add_column("Value", style="green")
    table.add_row("Input Stock ZIP", str(cfg.input_zip))
    table.add_row("Output Custom ZIP", str(cfg.output_zip))
    table.add_row("Partitions Updated", "mondo ONLY (A/B safe; vbmeta/boot/system/uboot untouched)")
    table.add_row("Custom Root Password", "Yes (SHA-512 crypt)" if cfg.root_password_hash else "No")
    table.add_row("SSH Authorized Key", (cfg.ssh_authorized_key[:48] + "...") if cfg.ssh_authorized_key else "No")
    table.add_row("SSH Port", str(cfg.ssh_port) if (cfg.ssh_authorized_key or cfg.root_password_hash) else "Disabled")
    table.add_row("Generate Beni Host Key", "Yes (Ed25519)" if cfg.generate_beni_host_key else "No")
    table.add_row("Generate Beni Client Key", "Yes (Ed25519)" if cfg.generate_beni_client_key else "No")
    table.add_row("Enable USB-C ADB", "Yes (removes authorized_debug_profile)" if cfg.enable_adb else "No")
    table.add_row("Inject ADB Public Key", "Yes" if cfg.adb_pubkey else "No")
    console.print(table)

    with tempfile.TemporaryDirectory(prefix="open_ineb_build_") as tmp:
        tmp_dir = pathlib.Path(tmp)
        stage_dir = tmp_dir / "stage"
        x5_dir = tmp_dir / "x5"
        stage_dir.mkdir()
        x5_dir.mkdir()

        # 1. Locate 0000 manifest and 0300 X5 ZIP inside outer archive
        with zipfile.ZipFile(cfg.input_zip, "r") as ozf:
            names = ozf.namelist()
            cfg_name = next((n for n in names if "_0000_" in n and n.endswith(".cfg.plain.unsign")), None)
            x5_name = next((n for n in names if "_0300_" in n and n.endswith(".fw.plain.unsign")), None)
            if not cfg_name or not x5_name:
                raise RuntimeError(f"Could not locate 0000 manifest and 0300 X5 module in {cfg.input_zip}")
            console.print(f"[blue]Extracting {cfg_name} and {x5_name}...[/blue]")
            manifest_text = ozf.read(cfg_name).decode("utf-8")
            x5_zip_path = tmp_dir / x5_name
            ozf.extract(x5_name, tmp_dir)

        # 2. Extract X5 inner archive (data.json, gpt.conf, ota_process, mondo.img)
        console.print("[blue]Extracting X5 mondo.img and metadata...[/blue]")
        with zipfile.ZipFile(x5_zip_path, "r") as xzf:
            for req_file in ["data.json", "gpt.conf", "ota_process", "mondo.img"]:
                if req_file not in xzf.namelist():
                    raise RuntimeError(f"Missing {req_file} inside {x5_name}")
                xzf.extract(req_file, x5_dir)

        # 3. Prepare staged files to inject into mondo.img
        (stage_dir / "md_open_ineb_init").write_text(build_init_script(cfg), encoding="utf-8")
        autostart_lines = list(STOCK_AUTOSTART_SERVICES) + ["md_open_ineb_init"]
        (stage_dir / "md_app_autostart.conf").write_text("\n".join(autostart_lines) + "\n", encoding="utf-8")

        if cfg.root_password_hash:
            (stage_dir / "root_shadow_hash").write_text(cfg.root_password_hash + "\n", encoding="utf-8")

        if cfg.ssh_authorized_key:
            (stage_dir / "authorized_keys").write_text(cfg.ssh_authorized_key.strip() + "\n", encoding="utf-8")

        if cfg.adb_pubkey:
            (stage_dir / "adb_keys").write_text(cfg.adb_pubkey.strip() + "\n", encoding="utf-8")

        if cfg.generate_beni_host_key or cfg.generate_beni_client_key:
            cfg.keys_out_dir.mkdir(parents=True, exist_ok=True)

        if cfg.generate_beni_host_key:
            h_priv, h_pub = generate_ed25519_keypair("root@beni-open-ineb")
            (stage_dir / "ssh_host_ed25519_key").write_bytes(h_priv)
            (stage_dir / "ssh_host_ed25519_key.pub").write_bytes(h_pub)
            (cfg.keys_out_dir / "ssh_host_ed25519_key.pub").write_bytes(h_pub)
            console.print(f"[green]Generated Beni SSH host pubkey:[/green] {h_pub.decode().strip()}")

        if cfg.generate_beni_client_key:
            c_priv, c_pub = generate_ed25519_keypair("root@beni-client")
            (stage_dir / "id_ed25519").write_bytes(c_priv)
            (stage_dir / "id_ed25519.pub").write_bytes(c_pub)
            (cfg.keys_out_dir / "beni_id_ed25519.pub").write_bytes(c_pub)
            console.print(f"[green]Generated Beni SSH client pubkey:[/green] {c_pub.decode().strip()}")

        # 4. Patch mondo.img using debugfs + e2fsck inside OrbStack Docker
        debugfs_cmds = [
            "mkdir /etc/.config",
            "mkdir /etc/open_ineb",
            "rm /bin/md_open_ineb_init",
            "write /stage/md_open_ineb_init /bin/md_open_ineb_init",
            "set_inode_field /bin/md_open_ineb_init mode 0100755",
            "rm /etc/.config/md_app_autostart.conf",
            "write /stage/md_app_autostart.conf /etc/.config/md_app_autostart.conf",
            "set_inode_field /etc/.config/md_app_autostart.conf mode 0100644",
        ]
        if cfg.enable_adb or cfg.root_password_hash or cfg.ssh_authorized_key:
            debugfs_cmds.append("rm /etc/authorized_debug_profile")

        for fname, mode in [
            ("root_shadow_hash", "0100600"),
            ("authorized_keys", "0100600"),
            ("adb_keys", "0100644"),
            ("ssh_host_ed25519_key", "0100600"),
            ("ssh_host_ed25519_key.pub", "0100644"),
            ("id_ed25519", "0100600"),
            ("id_ed25519.pub", "0100644"),
        ]:
            if (stage_dir / fname).exists():
                debugfs_cmds.extend([
                    f"rm /etc/open_ineb/{fname}",
                    f"write /stage/{fname} /etc/open_ineb/{fname}",
                    f"set_inode_field /etc/open_ineb/{fname} mode {mode}",
                ])

        (stage_dir / "cmds.debugfs").write_text("\n".join(debugfs_cmds) + "\n", encoding="utf-8")

        console.print("[blue]Patching mondo.img ext4 image via Docker debugfs + e2fsck...[/blue]")
        docker_cmd = [
            "docker", "run", "--rm",
            "-v", f"{x5_dir}:/x5",
            "-v", f"{stage_dir}:/stage:ro",
            "alpine", "sh", "-c",
            "apk add -q e2fsprogs e2fsprogs-extra >/dev/null && "
            "debugfs -w -f /stage/cmds.debugfs /x5/mondo.img && "
            "e2fsck -fy /x5/mondo.img || [ $? -le 2 ]",
        ]
        res = subprocess.run(docker_cmd, capture_output=True, text=True)
        if res.returncode != 0:
            raise RuntimeError(f"Docker debugfs/e2fsck failed:\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}")

        # 5. Update X5 data.json to mondo-only with fresh MD5
        mondo_path = x5_dir / "mondo.img"
        new_mondo_md5 = md5_file(mondo_path)
        new_mondo_size = mondo_path.stat().st_size
        data_json = json.loads((x5_dir / "data.json").read_text(encoding="utf-8"))
        data_json["update_partition"] = ["mondo"]
        data_json["partition_info"] = {
            "mondo": {
                "md5sum": {"mondo.img": new_mondo_md5},
                "md5_scope": {"mondo.img": new_mondo_size},
                "medium": "emmc",
                "part_type": "AB",
                "upgrade_method": "image",
                "have_anti_ver": False,
                "imgname": "mondo.img",
            }
        }
        (x5_dir / "data.json").write_text(json.dumps(data_json, indent=4), encoding="utf-8")

        # 6. Repack inner X5 0300 ZIP
        custom_x5_path = tmp_dir / x5_name
        console.print(f"[blue]Repacking {x5_name} (mondo-only)...[/blue]")
        with zipfile.ZipFile(custom_x5_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=1) as nzf:
            for fn in ["gpt.conf", "data.json", "ota_process", "mondo.img"]:
                nzf.write(x5_dir / fn, arcname=fn)

        x5_size = custom_x5_path.stat().st_size
        x5_sha256 = sha256_file(custom_x5_path)

        # 7. Build clean mondo-only outer TOML manifest (preserving package header & strategy)
        header_match = re.search(r"(\[package\].*?)(?=\[\[firmwares\]\])", manifest_text, re.S)
        strategy_match = re.search(r"(\[strategy\].*)", manifest_text, re.S)
        pkg_header = header_match.group(1).strip() if header_match else "[package]\nversion = '26.10.28.04'"
        strategy_block = strategy_match.group(1).strip() if strategy_match else "[strategy]\nmax_concurrent = 1\nglobal_timeout = 3600"

        custom_manifest = (
            f"{pkg_header}\n\n"
            "[[firmwares]]\n"
            "part = '0300'\n"
            "device_type = 'X5'\n"
            f"size = {x5_size}\n"
            f"checksum = 'sha256:{x5_sha256}'\n"
            "priority = 0\n"
            "required = true\n\n"
            f"{strategy_block}\n"
        )

        # 8. Write final outer OTA ZIP
        cfg.output_zip.parent.mkdir(parents=True, exist_ok=True)
        console.print(f"[blue]Writing final custom OTA archive to {cfg.output_zip}...[/blue]")
        with zipfile.ZipFile(cfg.output_zip, "w", compression=zipfile.ZIP_STORED) as fzf:
            fzf.writestr(cfg_name, custom_manifest.encode("utf-8"))
            fzf.write(custom_x5_path, arcname=x5_name)

        outer_sha = sha256_file(cfg.output_zip)
        console.print(
            Panel.fit(
                f"[bold green]Custom OTA archive built & verified![/bold green]\n"
                f"File:   {cfg.output_zip}\n"
                f"Size:   {cfg.output_zip.stat().st_size:,} bytes\n"
                f"SHA256: {outer_sha}\n"
                f"X5 MD5: {new_mondo_md5} (mondo.img only)",
                border_style="green",
            )
        )


def _detect_zip_version(zip_path: pathlib.Path) -> str:
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            for name in zf.namelist():
                if name.endswith(".toml") or name == "config":
                    text = zf.read(name).decode("utf-8", errors="replace")
                    m = re.search(r"version\s*=\s*['\"]([^'\"]+)['\"]", text)
                    if m:
                        return m.group(1)
    except Exception:
        pass
    return "26.10.28.04"


def upload_local_ota(
    zip_path: pathlib.Path,
    robot_ip: str = "192.168.7.1",
    http_port: int = 8080,
    ucp_port: int = 9007,
    upgrade_id: str | None = None,
    grant_token: str | None = None,
    device_id: str = "",
    zenoh_mode: str = "peer",
    trigger_install: bool = True,
    chunk_size: int = 2 * 1024 * 1024,
) -> None:
    tools_dir = pathlib.Path(__file__).resolve().parent
    if str(tools_dir) not in sys.path:
        sys.path.insert(0, str(tools_dir))
    from ota_client import BeniOtaClient, STAGE_NAMES
    from rich.progress import BarColumn, DownloadColumn, Progress, TransferSpeedColumn

    total_size = zip_path.stat().st_size
    version = _detect_zip_version(zip_path)

    if grant_token:
        # Manual token override: skip Zenoh START handshake and upload via TUS 1.0.0 directly
        uid = upgrade_id or str(uuid.uuid4())
        client = BeniOtaClient(robot_ip=robot_ip, ucp_port=ucp_port, http_port=http_port)
        console.print(
            f"[blue]Uploading {zip_path.name} ({total_size:,} bytes) via TUS 1.0.0 "
            f"to http://{robot_ip}:{http_port}/v2/upgrades/{uid}/upload...[/blue]"
        )
        with Progress(
            "[progress.description]{task.description}",
            BarColumn(),
            DownloadColumn(),
            TransferSpeedColumn(),
            console=console,
        ) as progress:
            task_id = progress.add_task("Uploading", total=total_size)
            client.upload_tus(
                zip_path,
                upgrade_id=uid,
                upload_token=grant_token,
                chunk_size=chunk_size,
                on_progress=lambda done, _tot: progress.update(task_id, completed=done),
            )
        console.print(
            Panel.fit(
                f"[bold green]TUS 1.0.0 upload complete (upgrade_id={uid})[/bold green]",
                border_style="green",
            )
        )
        return

    # Full standalone mode: UCP (:9007) -> Zenoh-over-UCP -> START -> TUS 1.0.0 -> INSTALL -> Progress
    console.print(
        f"[blue]Connecting standalone UCP+Zenoh client to {robot_ip}:{ucp_port} "
        f"(mode={zenoh_mode})...[/blue]"
    )
    with BeniOtaClient(
        robot_ip=robot_ip,
        ucp_port=ucp_port,
        http_port=http_port,
        zenoh_mode=zenoh_mode,
    ) as client:
        console.print("[green]UCP + Zenoh session established![/green]")
        console.print(
            f"[blue]Requesting upload grant via app/cmd/ota/upgrade_control_req "
            f"(version={version}, size={total_size:,})...[/blue]"
        )
        prep, task_ctx = client.prepare_upgrade(
            zip_path,
            version=version,
            device_id=device_id,
            upgrade_id=upgrade_id,
        )
        console.print(
            f"[green]Upload grant received:[/green] upgrade_id={prep.upgrade_id}, "
            f"state={prep.state or 'CREATED'}, resume_offset={prep.offset}"
        )

        with Progress(
            "[progress.description]{task.description}",
            BarColumn(),
            DownloadColumn(),
            TransferSpeedColumn(),
            console=console,
        ) as progress:
            task_id = progress.add_task("TUS Upload", total=total_size, completed=prep.offset)
            client.upload_tus(
                zip_path,
                upgrade_id=prep.upgrade_id,
                upload_token=prep.upload_grant_token,
                start_offset=prep.offset,
                chunk_size=chunk_size,
                on_progress=lambda done, _tot: progress.update(task_id, completed=done),
            )

        if not trigger_install:
            console.print(
                Panel.fit(
                    f"[bold green]Upload complete (install skipped via --no-install)[/bold green]\n"
                    f"upgrade_id: {prep.upgrade_id}",
                    border_style="green",
                )
            )
            return

        console.print("[blue]Triggering OTA install (OtaControlCmd.INSTALL)...[/blue]")
        client.trigger_install(
            upgrade_id=prep.upgrade_id,
            upload_token=prep.upload_grant_token,
            task_ctx=task_ctx,
        )

        console.print("[blue]Monitoring device/status/ota/upgrade_progress...[/blue]")
        with Progress(
            "[progress.description]{task.description}",
            BarColumn(),
            "[progress.percentage]{task.percentage:>3.0f}%",
            console=console,
        ) as progress:
            ptask = progress.add_task("Installing", total=100)

            def _on_prog(p: object) -> None:
                data = getattr(p, "data", None)
                if data is not None:
                    stage = STAGE_NAMES.get(data.upgrade_stage, f"STAGE_{data.upgrade_stage}")
                    progress.update(
                        ptask,
                        completed=min(100, data.progress_percent),
                        description=f"OTA {stage}",
                    )

            final = client.monitor_progress(on_update=_on_prog, timeout=180.0)

        if final and final.data and final.data.return_code != 0:
            raise RuntimeError(
                f"Robot reported OTA error: return_code={final.data.return_code}, "
                f"stage={final.data.upgrade_stage}"
            )
        console.print(
            Panel.fit(
                f"[bold green]Standalone OTA upload & install complete![/bold green]\n"
                f"upgrade_id: {prep.upgrade_id}\n"
                "Beni will reboot into the updated mondo A/B slot.",
                border_style="green",
            )
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="open_ineb Custom Firmware TUI Builder & Local OTA Uploader")
    parser.add_argument("-i", "--input", help="Path to stock firmware ZIP")
    parser.add_argument("-o", "--output", help="Path to output custom OTA ZIP")
    parser.add_argument("--non-interactive", action="store_true", help="Build without TUI prompts using CLI flags")
    parser.add_argument("--ssh-pubkey-file", type=pathlib.Path, help="SSH public key file to authorize")
    parser.add_argument("--ssh-port", type=int, default=2222, help="Port for open_ineb sshd (default: 2222)")
    parser.add_argument("--root-password", default=None, help="Optional root password (hashed locally)")
    parser.add_argument("--no-host-key", action="store_true", help="Skip generating Beni Ed25519 host keypair")
    parser.add_argument("--gen-client-key", action="store_true", help="Generate Beni Ed25519 client keypair")
    parser.add_argument("--enable-adb", action="store_true", help="Enable USB-C ADB (remove authorized_debug_profile)")
    parser.add_argument("--adb-pubkey-file", type=pathlib.Path, help="Optional adbkey.pub file")
    parser.add_argument("--upload", action="store_true", help="Upload built OTA ZIP to Beni over local Wi-Fi")
    parser.add_argument("--upload-only", type=pathlib.Path, help="Upload an existing OTA ZIP without rebuilding")
    parser.add_argument("--upgrade-id", default=None, help="Optional Upgrade UUID override")
    parser.add_argument("--grant-token", default=None, help="Optional manual Upload-Token (skips Zenoh START)")
    parser.add_argument("--device-id", default="", help="Optional robot serial/device_id")
    parser.add_argument("--robot-ip", default="192.168.7.1", help="Beni IP address (default: 192.168.7.1)")
    parser.add_argument("--ucp-port", type=int, default=9007, help="Beni UCP UDP port (default: 9007)")
    parser.add_argument("--http-port", type=int, default=8080, help="Beni OTA HTTP port (default: 8080)")
    parser.add_argument("--zenoh-mode", choices=["peer", "client"], default="peer", help="Zenoh session mode (default: peer)")
    parser.add_argument("--no-install", action="store_true", help="Upload ZIP without sending INSTALL command")
    args = parser.parse_args()

    if args.upload_only:
        upload_local_ota(
            args.upload_only.expanduser().resolve(),
            robot_ip=args.robot_ip,
            http_port=args.http_port,
            ucp_port=args.ucp_port,
            upgrade_id=args.upgrade_id,
            grant_token=args.grant_token,
            device_id=args.device_id,
            zenoh_mode=args.zenoh_mode,
            trigger_install=not args.no_install,
        )
        return 0

    if args.non_interactive:
        if not args.input or not args.output:
            parser.error("--non-interactive requires --input and --output")
        out_zip = pathlib.Path(args.output).expanduser().resolve()
        cfg = CustomizationConfig(
            input_zip=pathlib.Path(args.input).expanduser().resolve(),
            output_zip=out_zip,
            keys_out_dir=out_zip.parent / f"{out_zip.stem}_keys",
            root_password_hash=sha512_crypt.hash(args.root_password) if args.root_password else None,
            ssh_authorized_key=args.ssh_pubkey_file.expanduser().read_text().strip() if args.ssh_pubkey_file else None,
            generate_beni_host_key=not args.no_host_key,
            generate_beni_client_key=args.gen_client_key,
            ssh_port=args.ssh_port,
            enable_adb=args.enable_adb,
            adb_pubkey=args.adb_pubkey_file.expanduser().read_text().strip() if args.adb_pubkey_file else None,
            upload_ota=args.upload,
            robot_ip=args.robot_ip,
        )
    else:
        cfg = run_tui(args)

    repack_firmware(cfg)
    if cfg.upload_ota:
        upload_local_ota(
            cfg.output_zip,
            robot_ip=cfg.robot_ip,
            http_port=args.http_port,
            ucp_port=args.ucp_port,
            device_id=args.device_id,
            zenoh_mode=args.zenoh_mode,
            trigger_install=not args.no_install,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
