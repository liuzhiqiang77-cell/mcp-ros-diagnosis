"""
LLM 客户端 - 支持本地 Qwen2.5-7B 和远端 OpenAI 兼容 API
"""

import logging
from typing import Optional

import httpx

from ..config import LLMConfig

logger = logging.getLogger(__name__)


class LLMClient:
    def __init__(self, config: LLMConfig):
        self.config = config

    async def chat(self, user_message: str, system_prompt: str = "") -> str:
        """发送对话请求，返回文本回复"""
        if self.config.use_remote:
            return await self._call_api(
                url=self.config.remote_url,
                model=self.config.remote_model,
                api_key=self.config.api_key,
                user_message=user_message,
                system_prompt=system_prompt,
            )
        else:
            return await self._call_api(
                url=self.config.local_url,
                model=self.config.local_model,
                api_key="not-required",
                user_message=user_message,
                system_prompt=system_prompt,
            )

    async def _call_api(
        self,
        url: str,
        model: str,
        api_key: str,
        user_message: str,
        system_prompt: str,
    ) -> str:
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": user_message})

        payload = {
            "model": model,
            "messages": messages,
            "max_tokens": self.config.max_tokens,
            "temperature": self.config.temperature,
        }

        try:
            async with httpx.AsyncClient(timeout=self.config.timeout) as client:
                resp = await client.post(
                    f"{url.rstrip('/')}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "User-Agent": "claude-code/1.0",
                    },
                    json=payload,
                )
                resp.raise_for_status()
                data = resp.json()
                return data["choices"][0]["message"]["content"].strip()

        except httpx.TimeoutException:
            logger.warning(f"LLM 请求超时 ({self.config.timeout}s)")
            raise
        except Exception as e:
            logger.error(f"LLM 请求失败: {e}")
            raise

    def is_available(self) -> bool:
        """检查 LLM 是否已配置"""
        if self.config.use_remote:
            return bool(self.config.api_key)
        return True  # 本地 LLM 假定可用（运行时检测）
