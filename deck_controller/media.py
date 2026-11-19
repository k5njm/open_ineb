"""
Local HTTP media gallery client for Beni's `md_media_server` (`http://<ip>:8084`).

Endpoints verified from `md_media_server_config.json` & `md_file_server_config.json`:
  - `GET /media/files`                  -> JSON list of indexed photos & videos
  - `GET /media/files/{id}/thumbnail`   -> JPEG thumbnail bytes
  - `GET /media/files/{id}/content`     -> Full photo/video file stream
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import requests

MEDIA_DEFAULT_PORT = 8084


@dataclass
class MediaItem:
    file_id: str
    filename: str
    media_type: str  # "photo" or "video"
    size_bytes: int = 0
    created_at: str = ""
    duration_s: float = 0.0
    storage: str = "internal"


class BeniMediaClient:
    def __init__(self, robot_ip: str = "192.168.7.1", port: int = MEDIA_DEFAULT_PORT) -> None:
        self.base_url = f"http://{robot_ip}:{port}"

    def list_files(self, timeout: float = 5.0) -> list[MediaItem]:
        resp = requests.get(f"{self.base_url}/media/files", timeout=timeout)
        resp.raise_for_status()
        payload = resp.json()
        raw_items = (
            payload
            if isinstance(payload, list)
            else payload.get("files") or payload.get("items") or payload.get("data") or []
        )
        items: list[MediaItem] = []
        for entry in raw_items:
            if not isinstance(entry, dict):
                continue
            fid = str(entry.get("id") or entry.get("file_id") or entry.get("fileId") or "")
            name = str(entry.get("filename") or entry.get("name") or f"beni_{fid}")
            mtype = str(entry.get("media_type") or entry.get("type") or "").lower()
            if not mtype:
                mtype = "video" if name.lower().endswith((".mp4", ".mov", ".mkv")) else "photo"
            items.append(
                MediaItem(
                    file_id=fid,
                    filename=name,
                    media_type=mtype,
                    size_bytes=int(entry.get("size") or entry.get("size_bytes") or 0),
                    created_at=str(entry.get("created_at") or entry.get("createdAt") or ""),
                    duration_s=float(entry.get("duration") or entry.get("duration_s") or 0.0),
                    storage=str(entry.get("storage") or "internal"),
                )
            )
        return items

    def fetch_thumbnail(self, file_id: str, timeout: float = 5.0) -> bytes | None:
        try:
            resp = requests.get(
                f"{self.base_url}/media/files/{file_id}/thumbnail",
                timeout=timeout,
            )
            if resp.status_code == 200 and resp.content:
                return resp.content
        except Exception:
            pass
        return None

    def download_file(
        self,
        item: MediaItem,
        dest_dir: Path,
        *,
        on_progress: Callable[[int, int], None] | None = None,
        timeout: float = 60.0,
    ) -> Path:
        dest_dir.mkdir(parents=True, exist_ok=True)
        out_path = dest_dir / Path(item.filename).name
        with requests.get(
            f"{self.base_url}/media/files/{item.file_id}/content",
            stream=True,
            timeout=timeout,
        ) as resp:
            resp.raise_for_status()
            total = int(resp.headers.get("Content-Length") or item.size_bytes or 0)
            written = 0
            with out_path.open("wb") as f:
                for chunk in resp.iter_content(chunk_size=256 * 1024):
                    if not chunk:
                        continue
                    f.write(chunk)
                    written += len(chunk)
                    if on_progress is not None:
                        on_progress(written, total)
        return out_path
