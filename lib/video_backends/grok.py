"""GrokVideoBackend — xAI Grok 视频生成后端。"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from datetime import timedelta
from pathlib import Path

import httpx

from lib.db.repositories.usage_repo import MAX_BILLED_DURATION_SECONDS
from lib.grok_shared import create_grok_client, grok_should_retry
from lib.logging_utils import format_kwargs_for_log
from lib.providers import PROVIDER_GROK
from lib.retry import with_retry_async
from lib.video_backends.base import (
    IMAGE_MIME_TYPES,
    VideoCapabilities,
    VideoCapability,
    VideoCapabilityError,
    VideoGenerationRequest,
    VideoGenerationResult,
    download_video,
)

logger = logging.getLogger(__name__)


class GrokVideoBackend:
    """xAI Grok 视频生成后端。"""

    DEFAULT_MODEL = "grok-imagine-video"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
    ):
        self._client = create_grok_client(api_key=api_key, base_url=base_url)
        self._model = model or self.DEFAULT_MODEL
        self._capabilities: set[VideoCapability] = {VideoCapability.IMAGE_TO_VIDEO}
        if self._model != "grok-imagine-video-1.5":
            self._capabilities.add(VideoCapability.TEXT_TO_VIDEO)

    @property
    def name(self) -> str:
        return PROVIDER_GROK

    @property
    def model(self) -> str:
        return self._model

    @property
    def capabilities(self) -> set[VideoCapability]:
        return self._capabilities

    @property
    def video_capabilities(self) -> VideoCapabilities:
        # xAI Imagine 的 image-to-video 与 reference-image generation 是互斥输入模式；
        # SDK/API 不接受 image_url 与 reference_image_urls 同时出现。
        return VideoCapabilities(reference_images=True, max_reference_images=7, reference_images_with_start_frame=False)

    async def resume_video(self, job_id: str, request: VideoGenerationRequest) -> VideoGenerationResult:
        # Grok 同步型 API，无 job_id 可接续；orphan handler 据 NotImplementedError 标 [resume_unsupported]
        raise NotImplementedError("GrokVideoBackend 不支持 resume_video（同步型 API）")

    async def generate(self, request: VideoGenerationRequest) -> VideoGenerationResult:
        """生成视频。生成与下载分离重试，避免下载失败导致重新生成浪费额度。"""
        response = await self._create_video(request)

        video_url = response.url
        # SDK 响应字段未类型化，收窄为 int 才能作为实际计费时长落账本的 Integer 列；
        # 先经 float 接受 "15.0" 这类浮点字符串。缺失/不可解析（含 inf/nan）/非正/
        # 超出合理上限的值回落请求时长，保证结果恒为正且可落库。
        raw_duration = getattr(response, "duration", None)
        actual_duration = request.duration_seconds
        try:
            if raw_duration is not None:
                parsed = float(raw_duration)
                # 上下限基于取整前的原始数值判断：86400.9 已超 24h，不得因取整落回上限内被接受
                if 0 < parsed <= MAX_BILLED_DURATION_SECONDS:
                    # half-up 取整与 dashscope extract_billing_duration 同口径，避免截断少计费秒数；
                    # (0, 0.5) 取整到 0 时同样回落，保持结果恒为正
                    rounded = int(parsed + 0.5)
                    if rounded > 0:
                        actual_duration = rounded
        except (TypeError, ValueError, OverflowError):
            # 解析失败属预期内回落（SDK 字段未类型化），保留请求时长即可，无需上抛
            logger.debug("Grok 回报的 duration 无法解析: %r，回落请求时长 %s 秒", raw_duration, actual_duration)

        await download_video(video_url, request.output_path)
        logger.info("Grok 视频下载完成: %s", request.output_path)

        return VideoGenerationResult(
            video_path=request.output_path,
            provider=PROVIDER_GROK,
            model=self._model,
            duration_seconds=actual_duration,
            video_uri=video_url,
            generate_audio=True,
        )

    @with_retry_async(retry_if=grok_should_retry)
    async def _create_video(self, request: VideoGenerationRequest):
        """创建视频生成任务（带独立重试）。"""
        generate_kwargs = {
            "prompt": request.prompt,
            "model": self._model,
            "duration": request.duration_seconds,
            "aspect_ratio": request.aspect_ratio,
            "timeout": timedelta(minutes=15),
            "interval": timedelta(seconds=5),
        }
        if request.resolution is not None:
            generate_kwargs["resolution"] = request.resolution

        def _encode_to_data_uri(path: Path) -> str:
            suffix = path.suffix.lower()
            mime_type = IMAGE_MIME_TYPES.get(suffix, "image/png")
            b64 = base64.b64encode(path.read_bytes()).decode("ascii")
            return f"data:{mime_type};base64,{b64}"

        has_start_image = bool(request.start_image and Path(request.start_image).exists())
        has_reference_images = bool(request.reference_images)
        if has_start_image and has_reference_images:
            raise VideoCapabilityError(
                "video_reference_images_with_frames_unsupported",
                model=self._model,
            )

        if has_start_image:
            image_path = Path(request.start_image)  # type: ignore[arg-type]
            generate_kwargs["image_url"] = await asyncio.to_thread(_encode_to_data_uri, image_path)

        if request.reference_images:
            ref_paths = [Path(p) if not isinstance(p, Path) else p for p in request.reference_images]
            existing_paths = [p for p in ref_paths if p.exists()]
            if existing_paths:
                ref_urls = await asyncio.gather(*[asyncio.to_thread(_encode_to_data_uri, p) for p in existing_paths])
                generate_kwargs["reference_image_urls"] = list(ref_urls)

        logger.info("Grok 视频生成开始: model=%s, duration=%ds", self._model, request.duration_seconds)
        logger.info("调用 %s 视频 SDK kwargs=%s", self.name, format_kwargs_for_log(generate_kwargs))
        return await self._client.video.generate(**generate_kwargs)


class LLM360GrokVideoBackend:
    """Grok Imagine video through an LLM360 OpenAI-compatible node.

    LLM360 exposes xAI's video contract on ``/v1/videos/generations`` and
    ``/v1/videos/retrieve`` while keeping the provider credential inside the
    node.  Studio360 must therefore send the node API key, not an xAI OAuth
    token, and must not instantiate the native xAI SDK for this route.
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
    ):
        if not api_key or not api_key.strip():
            raise ValueError("LLM360GrokVideoBackend requires an LLM360 node API key")
        if not base_url or not base_url.strip():
            raise ValueError("LLM360GrokVideoBackend requires an LLM360 node base_url")
        normalized = base_url.strip().rstrip("/")
        if not normalized.endswith("/v1"):
            normalized = f"{normalized}/v1"
        self._base_url = normalized
        self._api_key = api_key.strip()
        self._model = model or "grok-imagine-video"
        self._capabilities: set[VideoCapability] = {
            VideoCapability.TEXT_TO_VIDEO,
            VideoCapability.IMAGE_TO_VIDEO,
        }

    @property
    def name(self) -> str:
        return "llm360-grok"

    @property
    def model(self) -> str:
        return self._model

    @property
    def capabilities(self) -> set[VideoCapability]:
        return self._capabilities

    @property
    def video_capabilities(self) -> VideoCapabilities:
        return VideoCapabilities(
            reference_images=True,
            max_reference_images=1,
            reference_images_with_start_frame=False,
        )

    async def generate(self, request: VideoGenerationRequest) -> VideoGenerationResult:
        payload: dict[str, object] = {
            "model": self._model,
            "prompt": request.prompt,
            "duration": request.duration_seconds,
            "aspect_ratio": request.aspect_ratio,
        }
        if request.resolution is not None:
            payload["resolution"] = request.resolution
        if request.seed is not None:
            payload["seed"] = request.seed

        if request.start_image and Path(request.start_image).exists():
            image_path = Path(request.start_image)
            mime_type = IMAGE_MIME_TYPES.get(image_path.suffix.lower(), "image/png")
            encoded = await asyncio.to_thread(
                lambda: base64.b64encode(image_path.read_bytes()).decode("ascii")
            )
            payload["image"] = f"data:{mime_type};base64,{encoded}"

        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        timeout = httpx.Timeout(connect=30.0, read=120.0, write=120.0, pool=30.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                f"{self._base_url}/videos/generations",
                headers=headers,
                json=payload,
            )
            response.raise_for_status()
            submitted = response.json()
            request_id = str(submitted.get("request_id") or submitted.get("id") or "").strip()
            if not request_id:
                raise RuntimeError(
                    f"LLM360 video submission returned no request_id: {json.dumps(submitted)[:500]}"
                )

            max_wait = max(600.0, float(request.duration_seconds) * 30.0)
            deadline = asyncio.get_running_loop().time() + max_wait
            final = submitted
            while asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(5.0)
                poll = await client.post(
                    f"{self._base_url}/videos/retrieve",
                    headers=headers,
                    json={"model": self._model, "request_id": request_id},
                )
                poll.raise_for_status()
                final = poll.json()
                status = str(final.get("status") or "").lower()
                if status in {"done", "completed", "succeeded", "success"}:
                    break
                if status in {"failed", "expired", "error", "canceled", "cancelled"}:
                    raise RuntimeError(
                        f"LLM360 video generation failed ({status}): "
                        f"{json.dumps(final)[:700]}"
                    )
            else:
                raise TimeoutError(f"LLM360 video generation timed out: {request_id}")

            video = final.get("video") if isinstance(final, dict) else None
            video_url = video.get("url") if isinstance(video, dict) else None
            if not isinstance(video_url, str) or not video_url.strip():
                raise RuntimeError(
                    f"LLM360 video completed without video.url: {json.dumps(final)[:700]}"
                )

            download = await client.get(video_url, headers={"Accept": "video/mp4"})
            download.raise_for_status()
            request.output_path.parent.mkdir(parents=True, exist_ok=True)
            request.output_path.write_bytes(download.content)

        return VideoGenerationResult(
            video_path=request.output_path,
            provider="llm360-grok",
            model=self._model,
            duration_seconds=request.duration_seconds,
            video_uri=video_url,
            generate_audio=True,
        )
