"""
Single place that constructs the LLM client. Every workflow calls
llm.get_llm() (not `from llm import get_llm`) so tests can monkeypatch this
module's attribute -- see smoke_test.py.
"""

import os
import json
import re
import time
from typing import Any, TypeVar

from langchain_nvidia_ai_endpoints import ChatNVIDIA
from dotenv import load_dotenv
from pydantic import BaseModel

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

MODEL_NAME = os.environ.get("NVIDIA_MODEL", "nvidia/nemotron-3-super-120b-a12b")
API_KEY = os.environ.get("NVIDIA_API_KEY")

ModelT = TypeVar("ModelT", bound=BaseModel)


def get_llm(temperature: float = 0.2, max_tokens: int = 64000) -> Any:
    kwargs: dict[str, Any] = {
        "model": MODEL_NAME,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if API_KEY:
        kwargs["api_key"] = API_KEY
    client = ChatNVIDIA(**kwargs)

    # Wrap client.invoke with rate limit / overload retry
    # Note: ChatNVIDIA is a Pydantic v2 model, use object.__setattr__ to bypass field validation
    original_invoke = client.invoke

    def _wrapped_invoke(prompt, *args, **kwargs):
        return invoke_with_retry(lambda p: original_invoke(p, *args, **kwargs), prompt)

    object.__setattr__(client, "invoke", _wrapped_invoke)
    return client


def _is_rate_limit_or_overload(error_str: str) -> bool:
    """Check if error is transient rate limit, overload, or gateway hiccup."""
    indicators = (
        "429",
        "503",
        "504",
        "rate limit",
        "too many requests",
        "temporarily overloaded",
        "service unavailable",
        "gateway timeout",
        "bad gateway",
        "capacity exceeded",
    )
    lower = error_str.lower()
    return any(ind in lower for ind in indicators) or ("404" in error_str and "nim" in lower)


def invoke_with_retry(
    invoker,
    prompt: Any,
    max_retries: int = 5,
    base_delay: float = 2.0,
    max_delay: float = 30.0,
):
    """Invoke LLM with exponential backoff & jitter on rate limits and overloads."""
    import random

    last_exc = None
    for attempt in range(max_retries):
        try:
            return invoker(prompt)
        except Exception as exc:
            last_exc = exc
            error_str = str(exc)
            if _is_rate_limit_or_overload(error_str):
                if attempt < max_retries - 1:
                    # Exponential backoff with jitter
                    delay = min(max_delay, base_delay * (2 ** attempt)) + random.uniform(0.5, 1.5)
                    print(
                        f"\n[WARN:LLM-RATE-LIMIT] Rate limit or provider overload detected (attempt {attempt + 1}/{max_retries}): "
                        f"{error_str[:120]}... Backing off for {delay:.1f}s before retry."
                    )
                    time.sleep(delay)
                    continue
            raise
    raise last_exc


def _schema_json(schema: type[ModelT]) -> dict[str, Any]:
    if hasattr(schema, "model_json_schema"):
        return schema.model_json_schema()
    return schema.schema()


def _response_content(response: Any) -> Any:
    content = getattr(response, "content", response)
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict) and isinstance(part.get("text"), str):
                parts.append(part["text"])
        return "".join(parts)
    return content


def _json_object(content: Any) -> dict[str, Any]:
    if isinstance(content, dict):
        return content
    if not isinstance(content, str):
        raise ValueError("Structured model response was not a JSON object.")

    text = re.sub(r"<think>.*?</think>", "", content, flags=re.IGNORECASE | re.DOTALL).strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.IGNORECASE | re.DOTALL)
    if fenced:
        text = fenced.group(1)

    decoder = json.JSONDecoder()
    for index, character in enumerate(text):
        if character != "{":
            continue
        try:
            value, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise ValueError("Structured model response did not contain a JSON object.")


def invoke_structured(
    schema: type[ModelT],
    prompt: str,
    *,
    temperature: float = 0.2,
    max_tokens: int = 64000,
) -> ModelT:
    """Request JSON as plain text and validate it locally with Pydantic."""
    debug = os.environ.get("DEBUG", "0").lower() in ("1", "true", "yes")

    structured_prompt = (
        f"{prompt}\n\n"
        "Return only one JSON object matching this schema. Do not use markdown, "
        "comments, or additional keys.\n"
        f"JSON schema:\n{json.dumps(_schema_json(schema), indent=2)}"
    )

    if debug:
        print(f"\n[DEBUG:LLM-REQ] Model={MODEL_NAME} Schema={schema.__name__}")
        print(f"[DEBUG:PROMPT]\n{structured_prompt[:500]}..." if len(structured_prompt) > 500 else f"[DEBUG:PROMPT]\n{structured_prompt}")

    client = get_llm(temperature=temperature, max_tokens=max_tokens)
    try:
        response = invoke_with_retry(client.invoke, structured_prompt)
        content = _response_content(response)
        if debug:
            print(f"[DEBUG:RAW-RESP]\n{content}\n")
    except Exception as exc:
        print(f"\n[ERROR:LLM-INVOKE] Model={MODEL_NAME} failed: {exc}")
        raise

    if isinstance(content, schema):
        return content

    try:
        data = _json_object(content)
        if hasattr(schema, "model_validate"):
            parsed = schema.model_validate(data)
        else:
            parsed = schema.parse_obj(data)
        if debug:
            print(f"[DEBUG:PARSED] {parsed}\n")
        return parsed
    except Exception as exc:
        print(f"\n[ERROR:PARSE-FAIL] Raw content:\n{content}\nParse error: {exc}\n")
        raise
