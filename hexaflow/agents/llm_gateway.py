from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from openai import AsyncOpenAI

from hexaflow.tools.helpers import image_to_data_url


@dataclass
class LLMConfig:
    api_key: str
    base_url: str
    model_name: str
    provider: str = "openai_compatible"


class LLMGateway:
    """
    Unified LLM caller for OpenAI-compatible endpoints.
    Extend this class to support provider-specific quirks in one place.
    """

    def __init__(self, config: LLMConfig):
        self.config = config
        self.client = AsyncOpenAI(api_key=config.api_key, base_url=config.base_url)

    @property
    def model_name(self) -> str:
        return self.config.model_name

    @staticmethod
    def _build_user_content(
        user_prompt: str,
        screenshot_path: str = "",
        screenshot_paths: Optional[list[str]] = None,
    ) -> list[dict]:
        content = [{"type": "text", "text": user_prompt}]

        paths = list(screenshot_paths or [])
        if screenshot_path and screenshot_path not in paths:
            paths.insert(0, screenshot_path)

        for path in paths:
            data_url = image_to_data_url(path)
            if data_url:
                content.append({"type": "image_url", "image_url": {"url": data_url}})
        return content

    async def complete(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.1,
        screenshot_path: str = "",
        screenshot_paths: Optional[list[str]] = None,
    ) -> str:
        content = self._build_user_content(
            user_prompt=user_prompt,
            screenshot_path=screenshot_path,
            screenshot_paths=screenshot_paths,
        )
        resp = await self.client.chat.completions.create(
            model=self.config.model_name,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": content},
            ],
            temperature=temperature,
        )
        return (resp.choices[0].message.content or "").strip()

