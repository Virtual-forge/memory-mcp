"""Small OpenAI-compatible JSON completion adapter."""

import json
from typing import Any


class OpenAIJsonClient:
    """Use an OpenAI-compatible chat endpoint without coupling prompts to a provider."""

    def __init__(
        self,
        model: str,
        api_key: str,
        base_url: str | None = None,
        temperature: float | None = None,
        client: Any | None = None,
    ) -> None:
        if client is None:
            from openai import OpenAI

            kwargs: dict[str, object] = {"api_key": api_key}
            if base_url:
                kwargs["base_url"] = base_url
            client = OpenAI(**kwargs)
        self.client = client
        self.model = model
        self.temperature = temperature

    def complete_json(self, *, system_prompt: str, payload: dict[str, object]) -> object:
        request: dict[str, object] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": json.dumps(payload, ensure_ascii=True, default=str),
                },
            ],
        }
        if self.temperature is not None:
            request["temperature"] = self.temperature
        response = self.client.chat.completions.create(**request)
        content = response.choices[0].message.content
        if not content:
            raise ValueError("LLM returned an empty response")
        return json.loads(strip_json_fence(content))


def strip_json_fence(content: str) -> str:
    """Tolerate a provider adding markdown fences despite the prompt."""
    stripped = content.strip()
    if stripped.startswith("```") and stripped.endswith("```"):
        lines = stripped.splitlines()
        return "\n".join(lines[1:-1]).strip()
    return stripped
