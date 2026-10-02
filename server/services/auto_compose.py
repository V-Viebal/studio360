"""Durable background composition and completion-hook orchestration.

Generation tasks deliberately finish as soon as their own media is persisted.
Composing a whole episode must not consume the agent's ReAct turn or make a
worker task wait for a potentially long ffmpeg operation.  This module is the
small, process-local dispatcher for that follow-up work.  The output receipt
and fingerprint make the operation idempotent across duplicate completion
events; a later worker restart can safely compose again when the inputs changed.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from lib.storyboard_sequence import get_storyboard_items

logger = logging.getLogger(__name__)

_locks: dict[tuple[str, str], asyncio.Lock] = {}
_scheduled: set[tuple[str, str]] = set()
_scheduled_guard = asyncio.Lock()


def auto_compose_enabled() -> bool:
    """Return the runtime switch; enabled by default for the accepted proposal."""
    value = os.getenv("STUDIO360_AUTO_COMPOSE_ENABLED", "true").strip().lower()
    return value not in {"0", "false", "no", "off"}


def _safe_output_name(script_file: str) -> str:
    stem = Path(script_file).stem
    stem = re.sub(r"[^a-zA-Z0-9_-]+", "_", stem).strip("_") or "episode_1"
    return f"auto_{stem}_final.mp4"


def _script_media_fingerprint(project_path: Path, script_file: str, script: dict[str, Any]) -> str:
    """Hash the ordered clip paths and mtimes, not media bytes, for cheap idempotency."""
    items, id_field, *_ = get_storyboard_items(script)
    rows: list[str] = [script_file, str(script.get("updated_at") or "")]
    for item in items:
        resource_id = str(item.get(id_field) or item.get("scene_id") or item.get("segment_id") or "")
        asset = item.get("generated_assets") or {}
        rel = asset.get("video_clip") if isinstance(asset, dict) else None
        path = (project_path / rel).resolve() if isinstance(rel, str) else None
        mtime = path.stat().st_mtime_ns if path and path.is_file() else 0
        rows.append(f"{resource_id}|{rel or ''}|{mtime}")
    return hashlib.sha256("\n".join(rows).encode("utf-8")).hexdigest()


def _ready_video_inputs(project_path: Path, script: dict[str, Any]) -> bool:
    """All storyboard items must have an in-project, regular video file."""
    items, id_field, *_ = get_storyboard_items(script)
    if not items:
        return False
    for item in items:
        asset = item.get("generated_assets") or {}
        rel = asset.get("video_clip") if isinstance(asset, dict) else None
        if not isinstance(rel, str) or not rel.strip():
            return False
        candidate = (project_path / rel).resolve()
        if not candidate.is_relative_to(project_path.resolve()) or not candidate.is_file():
            return False
    return True


def _public_url(project_name: str, output_name: str) -> str:
    base = os.getenv("STUDIO360_PUBLIC_BASE_URL", "https://studio360.hmz.one").rstrip("/")
    return f"{base}/api/v1/files/{quote(project_name, safe='')}/output/{quote(output_name, safe='')}"


async def _post_completion_hook(payload: dict[str, Any]) -> dict[str, Any]:
    """Send one optional, secret-safe channel webhook notification.

    The hook is intentionally generic so Telegram, Mons, Slack, or an internal
    channel adapter can consume the same contract without coupling Studio360
    to a channel credential.  Missing hook configuration is a valid no-op.
    """
    url = os.getenv("STUDIO360_COMPLETION_WEBHOOK_URL", "").strip()
    if not url:
        return {"configured": False, "delivered": False}
    headers = {"Content-Type": "application/json"}
    token = os.getenv("STUDIO360_COMPLETION_WEBHOOK_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    timeout = httpx.Timeout(connect=10.0, read=30.0, write=30.0, pool=10.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(url, headers=headers, json=payload)
        response.raise_for_status()
    return {"configured": True, "delivered": True, "status_code": response.status_code}


async def _compose(project_name: str, script_file: str, task_id: str | None) -> bool:
    from server.services.generation_tasks import get_project_manager

    pm = get_project_manager()
    project_path = await asyncio.to_thread(pm.get_project_path, project_name)
    script = await asyncio.to_thread(pm.load_script, project_name, script_file)
    if not _ready_video_inputs(project_path, script):
        logger.info("auto-compose deferred: video inputs incomplete project=%s script=%s", project_name, script_file)
        return False

    fingerprint = _script_media_fingerprint(project_path, script_file, script)
    output_name = _safe_output_name(script_file)
    output_dir = project_path / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    receipt_path = output_dir / ".auto-compose.json"
    if receipt_path.is_file():
        try:
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            receipt = {}
        if receipt.get("status") == "succeeded" and receipt.get("fingerprint") == fingerprint:
            logger.info("auto-compose idempotent skip project=%s script=%s", project_name, script_file)
            return True

    composer = project_path / ".claude/skills/compose-video/scripts/compose_video.py"
    if not composer.is_file():
        # Source checkouts use the profile path; production projects normally
        # have the materialized .claude skill.
        composer = Path(__file__).resolve().parents[2] / "agent_runtime_profile/.claude/skills/compose-video/scripts/compose_video.py"
    if not composer.is_file():
        raise FileNotFoundError("compose-video skill is not installed")

    cmd = [sys.executable, str(composer), script_file, "--output", output_name]
    try:
        process = await asyncio.to_thread(
            subprocess.run,
            cmd,
            cwd=project_path,
            capture_output=True,
            text=True,
            timeout=1800,
            check=False,
        )
        if process.returncode != 0:
            raise RuntimeError(process.stderr[-2000:] or "composer failed")
        output_path = output_dir / output_name
        if not output_path.is_file() or output_path.stat().st_size <= 0:
            raise RuntimeError("composer returned success without a verifiable output file")

        probe = await asyncio.to_thread(
            subprocess.run,
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration,size",
                "-of",
                "json",
                str(output_path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        media = json.loads(probe.stdout).get("format", {})
        duration = float(media.get("duration") or 0)
        size = int(media.get("size") or 0)
        if duration <= 0 or size <= 0:
            raise RuntimeError("ffprobe did not return a positive duration and size")

        receipt = {
            "status": "succeeded",
            "fingerprint": fingerprint,
            "project_name": project_name,
            "script_file": script_file,
            "task_id": task_id,
            "output_file": f"output/{output_name}",
            "public_url": _public_url(project_name, output_name),
            "duration_seconds": duration,
            "size_bytes": size,
        }
        receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            receipt["hook"] = await _post_completion_hook(
                {"event": "studio360.video.completed", "kind": "auto_compose", **receipt}
            )
        except Exception:
            # The media result is still valid; keep delivery failure explicit.
            logger.exception("auto-compose completion hook failed project=%s", project_name)
            receipt["hook"] = {"configured": True, "delivered": False}
        receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info("auto-compose completed project=%s script=%s url=%s", project_name, script_file, receipt["public_url"])
        return True
    except Exception:
        receipt_path.write_text(
            json.dumps(
                {
                    "status": "failed",
                    "fingerprint": fingerprint,
                    "project_name": project_name,
                    "script_file": script_file,
                    "task_id": task_id,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        raise


async def schedule_auto_compose(
    project_name: str,
    script_file: str | None,
    *,
    task_id: str | None = None,
) -> bool:
    """Schedule one non-blocking compose attempt for a completed video task."""
    if not auto_compose_enabled() or not script_file:
        return False
    key = (project_name, script_file)
    async with _scheduled_guard:
        if key in _scheduled:
            return False
        _scheduled.add(key)

    async def runner() -> None:
        lock = _locks.setdefault(key, asyncio.Lock())
        try:
            async with lock:
                # A segment can finish while its siblings are still running.
                # Keep this work in the background and retry until the whole
                # script is ready; the agent turn never waits for this loop.
                for _attempt in range(720):  # up to one hour at 5s intervals
                    if await _compose(project_name, script_file, task_id):
                        break
                    await asyncio.sleep(5.0)
        except Exception:
            logger.exception("background auto-compose failed project=%s script=%s", project_name, script_file)
        finally:
            async with _scheduled_guard:
                _scheduled.discard(key)

    asyncio.create_task(runner(), name=f"auto-compose-{project_name}-{Path(script_file).stem}")
    return True


__all__ = [
    "auto_compose_enabled",
    "schedule_auto_compose",
    "_ready_video_inputs",
    "_safe_output_name",
    "_script_media_fingerprint",
]
