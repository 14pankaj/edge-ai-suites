# Copyright (C) 2025 Intel Corporation
# SPDX-License-Identifier: Apache-2.0

"""
VLMClient — async OpenAI-compatible wrapper for OVMS VLM inference.

Improvements over the original:
- system_prompt sent as a proper ``{"role": "system", ...}`` message.
- Configurable image resolution and JPEG quality (from settings).
- Retry with exponential backoff for transient network/server errors.
- Inference latency tracking for metrics.
"""

import asyncio
import base64
import logging
import time
from typing import List, Optional, Tuple

import cv2
from openai import AsyncOpenAI, APIError, APITimeoutError, APIConnectionError

from src.config import settings

logger = logging.getLogger(__name__)


class VLMClient:
    """Async client for a single OVMS VLM endpoint."""

    def __init__(self, base_url: str, model_name: str):
        self.model_name = model_name
        self._client = AsyncOpenAI(
            base_url=base_url,
            api_key="no-key",  # OVMS does not validate the key
            timeout=settings.VLM_TIMEOUT,
            max_retries=0,   # we implement our own retry below
        )
        self.last_inference_ms: Optional[float] = None
        logger.info(f"VLMClient initialised — model={model_name} url={base_url}")

    async def analyze_stream_segment(
        self,
        frames: List,
        system_prompt: str,
        user_prompt: str,
    ) -> Optional[str]:
        """Send frames to the VLM and return the raw text response, or None on failure."""
        if not frames:
            return None

        # Build the user content block (images + text)
        user_content = []
        for frame in frames:
            encoded = self._encode_image(frame)
            if encoded:
                user_content.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{encoded}"},
                })

        if not user_content:
            logger.warning("No frames could be encoded — skipping VLM call")
            return None

        user_content.append({"type": "text", "text": user_prompt})
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ]

        logger.info(
            f"VLM call → model={self.model_name!r} "
            f"url={self._client.base_url} frames={len(frames)}"
        )
        return await self._call_with_retry(messages)

    def _encode_image(self, frame) -> Optional[str]:
        """Resize, JPEG-compress, and base64-encode a single frame."""
        try:
            h, w = frame.shape[:2]
            max_dim = settings.VLM_IMAGE_MAX_DIM
            if max(h, w) > max_dim:
                scale = max_dim / max(h, w)
                frame = cv2.resize(
                    frame,
                    (int(w * scale), int(h * scale)),
                    interpolation=cv2.INTER_AREA,
                )
            _, buf = cv2.imencode(
                ".jpg", frame,
                [int(cv2.IMWRITE_JPEG_QUALITY), settings.VLM_JPEG_QUALITY],
            )
            return base64.b64encode(buf).decode("utf-8")
        except Exception as exc:
            logger.error(f"Frame encoding failed: {exc}")
            return None

    async def _call_with_retry(self, messages: list) -> Optional[str]:
        """POST to the VLM with up to ``VLM_MAX_RETRIES`` retries."""
        retries = settings.VLM_MAX_RETRIES
        delay = 1.0

        for attempt in range(retries + 1):
            try:
                t0 = time.monotonic()
                response = await self._client.chat.completions.create(
                    model=self.model_name,
                    messages=messages,
                    max_tokens=settings.VLM_MAX_TOKENS,
                    temperature=0.0,
                )
                self.last_inference_ms = (time.monotonic() - t0) * 1000
                logger.info(f"VLM inference complete — {self.last_inference_ms:.0f} ms (attempt {attempt + 1})")
                content = response.choices[0].message.content
                logger.debug(f"VLM raw response (truncated): {(content or '')[:300]!r}")
                return content

            except (APITimeoutError, APIConnectionError) as exc:
                if attempt < retries:
                    logger.warning(f"VLM transient error (attempt {attempt + 1}): {exc} — retrying in {delay:.1f}s")
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 10.0)
                else:
                    logger.error(f"VLM call failed after {retries + 1} attempts: {exc}")
                    return None

            except APIError as exc:
                # Non-retryable API error (4xx etc.)
                logger.error(f"VLM API error: {exc}")
                return None

            except Exception as exc:
                logger.error(f"Unexpected VLM error: {exc}")
                return None

        return None
