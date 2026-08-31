"""Credential-safe text provider adapters for review-only regeneration."""
from __future__ import annotations

import json
import base64
import os
import time
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlparse

import httpx


class ProviderUnavailable(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class TextGenerationResult:
    text: str
    model: str
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None


@dataclass(frozen=True)
class ImageGenerationResult:
    image_bytes: bytes
    model: str
    revised_prompt: str | None = None


RequestFn = Callable[[str, str, dict[str, str], dict[str, Any] | None, float], tuple[int, dict[str, Any]]]
OPENAI_CANONICAL_BASE_URL = "https://api.openai.com/v1"

TEXT_COPY_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "headline": {"type": "string"},
        "title": {"type": "string"},
        "description": {"type": "string"},
        "alt_text": {"type": "string"},
    },
    "required": ["headline", "title", "description", "alt_text"],
}

IMAGE_SAFETY_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "background_only": {"type": "boolean"},
        "contains_product": {"type": "boolean"},
        "contains_packaging": {"type": "boolean"},
        "contains_logo": {"type": "boolean"},
        "contains_text": {"type": "boolean"},
        "contains_person": {"type": "boolean"},
    },
    "required": [
        "background_only",
        "contains_product",
        "contains_packaging",
        "contains_logo",
        "contains_text",
        "contains_person",
    ],
}


def normalize_local_base_url(value: str) -> str:
    parsed = urlparse(value.strip())
    if parsed.scheme != "http" or parsed.username or parsed.password:
        raise ValueError("The local provider endpoint must use credential-free HTTP.")
    if parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("The local provider endpoint must use a loopback host.")
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise ValueError("The local provider endpoint must be an origin without a path, query, or fragment.")
    try:
        parsed.port
    except ValueError as exc:
        raise ValueError("The local provider endpoint has an invalid port.") from exc
    return value.strip().rstrip("/")


def normalize_openai_base_url(value: str | None = None) -> str:
    """Only permit the official OpenAI API origin for credential-bearing calls."""
    candidate = (value or OPENAI_CANONICAL_BASE_URL).strip().rstrip("/")
    parsed = urlparse(candidate)
    if (
        candidate != OPENAI_CANONICAL_BASE_URL
        or parsed.scheme != "https"
        or parsed.hostname != "api.openai.com"
        or parsed.username
        or parsed.password
        or parsed.port
        or parsed.query
        or parsed.fragment
        or parsed.path != "/v1"
    ):
        raise ValueError("OpenAI credentials may only be sent to https://api.openai.com/v1.")
    return OPENAI_CANONICAL_BASE_URL


def _http_request(
    method: str,
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any] | None,
    timeout: float,
) -> tuple[int, dict[str, Any]]:
    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.request(method, url, headers=headers, json=payload)
            try:
                body = response.json()
            except ValueError:
                body = {}
            return response.status_code, body if isinstance(body, dict) else {}
    except httpx.TimeoutException as exc:
        raise ProviderUnavailable("timeout", "The selected AI provider timed out.") from exc
    except httpx.RequestError as exc:
        raise ProviderUnavailable("connection_error", "The selected AI provider could not be reached.") from exc


def _tokens(usage: Any) -> tuple[int | None, int | None, int | None]:
    if not isinstance(usage, dict):
        return None, None, None
    prompt = usage.get("input_tokens", usage.get("prompt_tokens", usage.get("prompt_eval_count")))
    completion = usage.get("output_tokens", usage.get("completion_tokens", usage.get("eval_count")))
    total = usage.get("total_tokens")
    if total is None and isinstance(prompt, int) and isinstance(completion, int):
        total = prompt + completion
    return prompt if isinstance(prompt, int) else None, completion if isinstance(completion, int) else None, total if isinstance(total, int) else None


def _response_text(body: dict[str, Any]) -> str | None:
    """Extract only model-authored text from a Responses API payload."""
    output_text = body.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text.strip()
    output = body.get("output")
    if not isinstance(output, list):
        return None
    fragments: list[str] = []
    for item in output:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "refusal":
                return None
            text = part.get("text")
            if part.get("type") == "output_text" and isinstance(text, str):
                fragments.append(text)
    combined = "".join(fragments).strip()
    return combined or None


class OllamaTextProvider:
    name = "ollama"

    def __init__(
        self,
        base_url: str,
        model: str,
        timeout_seconds: int = 30,
        requester: RequestFn | None = None,
    ):
        self.base_url = normalize_local_base_url(base_url)
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.requester = requester or _http_request

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None):
        status, body = self.requester(
            method,
            f"{self.base_url}{path}",
            {"Accept": "application/json", "Content-Type": "application/json"},
            payload,
            float(self.timeout_seconds),
        )
        if status >= 500:
            raise ProviderUnavailable("provider_error", "The local AI provider returned a server error.")
        if status >= 400:
            raise ProviderUnavailable("provider_rejected", "The local AI provider rejected the request.")
        return body

    def health(self) -> dict[str, Any]:
        try:
            body = self._request("GET", "/api/tags")
            models = body.get("models", []) if isinstance(body, dict) else []
            model_names = [item.get("name") for item in models if isinstance(item, dict) and item.get("name")]
            return {
                "provider": self.name,
                "configured": True,
                "reachable": True,
                "model": self.model,
                "model_available": self.model in model_names or not model_names,
                "message": "Ollama-compatible provider is reachable.",
            }
        except ProviderUnavailable as exc:
            return {
                "provider": self.name,
                "configured": True,
                "reachable": False,
                "model": self.model,
                "model_available": False,
                "message": str(exc),
                "failure_code": exc.code,
            }

    def generate(
        self,
        prompt: str,
        *,
        schema: dict[str, Any] | None = None,
        schema_name: str = "social_copy",
    ) -> TextGenerationResult:
        body = self._request("POST", "/api/generate", {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "format": "json",
        })
        text = body.get("response") if isinstance(body, dict) else None
        if not isinstance(text, str) or not text.strip():
            raise ProviderUnavailable("invalid_response", "The local AI provider returned no usable text.")
        prompt_tokens, completion_tokens, total_tokens = _tokens(body)
        if prompt_tokens is None:
            prompt_tokens, completion_tokens, total_tokens = _tokens({
                "prompt_eval_count": body.get("prompt_eval_count"),
                "eval_count": body.get("eval_count"),
            })
        return TextGenerationResult(text=text.strip(), model=self.model, prompt_tokens=prompt_tokens, completion_tokens=completion_tokens, total_tokens=total_tokens)


class OpenAITextProvider:
    name = "openai"

    def __init__(
        self,
        model: str,
        timeout_seconds: int = 30,
        api_key: str | None = None,
        base_url: str | None = None,
        requester: RequestFn | None = None,
    ):
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.api_key = api_key if api_key is not None else os.getenv("OPENAI_API_KEY")
        self.base_url = normalize_openai_base_url(base_url)
        self.requester = requester or _http_request

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None):
        if not self.api_key:
            raise ProviderUnavailable("missing_credentials", "The hosted AI provider is not configured.")
        status, body = self.requester(
            method,
            f"{self.base_url}{path}",
            {"Accept": "application/json", "Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"},
            payload,
            float(self.timeout_seconds),
        )
        if status in (401, 403):
            raise ProviderUnavailable("authentication_error", "The hosted AI provider credentials were rejected.")
        if status >= 500:
            raise ProviderUnavailable("provider_error", "The hosted AI provider returned a server error.")
        if status >= 400:
            raise ProviderUnavailable("provider_rejected", "The hosted AI provider rejected the request.")
        return body

    def health(self) -> dict[str, Any]:
        if not self.configured:
            return {
                "provider": self.name,
                "configured": False,
                "reachable": False,
                "model": self.model,
                "model_available": False,
                "message": "Hosted provider credentials are not configured.",
                "failure_code": "missing_credentials",
            }
        try:
            body = self._request("GET", "/models")
            model_names = [item.get("id") for item in body.get("data", []) if isinstance(item, dict) and item.get("id")]
            return {
                "provider": self.name,
                "configured": True,
                "reachable": True,
                "model": self.model,
                "model_available": self.model in model_names or not model_names,
                "message": "OpenAI provider is reachable.",
            }
        except ProviderUnavailable as exc:
            return {
                "provider": self.name,
                "configured": True,
                "reachable": False,
                "model": self.model,
                "model_available": False,
                "message": str(exc),
                "failure_code": exc.code,
            }

    def generate(
        self,
        prompt: str,
        *,
        schema: dict[str, Any] | None = None,
        schema_name: str = "social_copy",
    ) -> TextGenerationResult:
        body = self._request("POST", "/responses", {
            "model": self.model,
            "max_output_tokens": 600,
            "input": [
                {
                    "role": "system",
                    "content": [
                        {
                            "type": "input_text",
                            "text": "You write concise catalog-grounded social copy. Return only the requested structured object.",
                        }
                    ],
                },
                {"role": "user", "content": [{"type": "input_text", "text": prompt}]},
            ],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": schema_name,
                    "strict": True,
                    "schema": schema or TEXT_COPY_SCHEMA,
                }
            },
        })
        text = _response_text(body) if isinstance(body, dict) else None
        if not isinstance(text, str) or not text.strip():
            raise ProviderUnavailable("invalid_response", "The hosted AI provider returned no usable text.")
        prompt_tokens, completion_tokens, total_tokens = _tokens(body.get("usage"))
        return TextGenerationResult(text=text.strip(), model=self.model, prompt_tokens=prompt_tokens, completion_tokens=completion_tokens, total_tokens=total_tokens)


class OpenAIImageProvider:
    """OpenAI image generation restricted to background-only base64 output."""

    name = "openai"

    def __init__(
        self,
        model: str,
        timeout_seconds: int = 60,
        api_key: str | None = None,
        base_url: str | None = None,
        safety_model: str = "gpt-4o-mini",
        requester: RequestFn | None = None,
    ):
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.api_key = api_key if api_key is not None else os.getenv("OPENAI_API_KEY")
        self.base_url = normalize_openai_base_url(base_url)
        self.safety_model = safety_model
        self.requester = requester or _http_request

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def _request(self, payload: dict[str, Any]):
        return self._request_to("/images/generations", payload)

    def generate_background(self, style_prompt: str) -> ImageGenerationResult:
        body = self._request({
            "model": self.model,
            "prompt": (
                "Create a decorative editorial background only for a product composition. "
                "No product, bottle, packaging, label, logo, brand mark, text, person, or object that could "
                "be mistaken for the product. Leave a clean, uncluttered center foreground for the authentic "
                "Shopify product image to be composited by the application. " + style_prompt
            ),
            "size": "1024x1536",
            "n": 1,
            "response_format": "b64_json",
        })
        items = body.get("data") if isinstance(body, dict) else None
        encoded = items[0].get("b64_json") if items else None
        if not isinstance(encoded, str):
            raise ProviderUnavailable("invalid_response", "The hosted image provider did not return an inline image.")
        try:
            image_bytes = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError) as exc:
            raise ProviderUnavailable("invalid_response", "The hosted image provider returned invalid image data.") from exc
        if not image_bytes or len(image_bytes) > 8 * 1024 * 1024:
            raise ProviderUnavailable("invalid_response", "The hosted image provider returned an unusable image.")
        revised_prompt = items[0].get("revised_prompt") if isinstance(items[0], dict) else None
        return ImageGenerationResult(
            image_bytes=image_bytes,
            model=self.model,
            revised_prompt=revised_prompt if isinstance(revised_prompt, str) else None,
        )

    def validate_background(self, image_bytes: bytes) -> dict[str, Any]:
        encoded = base64.b64encode(image_bytes).decode("ascii")
        body = self._request_to(
            "/responses",
            {
                "model": self.safety_model,
                "max_output_tokens": 120,
                "input": [{
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": (
                                "Classify this candidate decorative background. Return only the structured safety object. "
                                "background_only may be true only if every contains_* value is false. If uncertain, "
                                "set background_only false."
                            ),
                        },
                        {
                            "type": "input_image",
                            "image_url": f"data:image/png;base64,{encoded}",
                            "detail": "low",
                        },
                    ],
                }],
                "text": {
                    "format": {
                        "type": "json_schema",
                        "name": "decorative_background_safety",
                        "strict": True,
                        "schema": IMAGE_SAFETY_SCHEMA,
                    }
                },
            },
        )
        try:
            raw = _response_text(body)
            decision = json.loads(raw)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ProviderUnavailable("invalid_safety_response", "The background safety validator returned an invalid response.") from exc
        keys = ("contains_product", "contains_packaging", "contains_logo", "contains_text", "contains_person")
        if (
            decision.get("background_only") is not True
            or any(decision.get(key) is not False for key in keys)
        ):
            raise ProviderUnavailable("background_safety_rejected", "The generated image was rejected by the background-only safety gate.")
        return {key: decision[key] for key in ("background_only", *keys)}

    def _request_to(self, path: str, payload: dict[str, Any]):
        if not self.api_key:
            raise ProviderUnavailable("missing_credentials", "The hosted AI provider is not configured.")
        status, body = self.requester(
            "POST",
            f"{self.base_url}{path}",
            {"Accept": "application/json", "Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"},
            payload,
            float(self.timeout_seconds),
        )
        if status in (401, 403):
            raise ProviderUnavailable("authentication_error", "The hosted AI provider credentials were rejected.")
        if status >= 500:
            raise ProviderUnavailable("provider_error", "The hosted AI provider returned a server error.")
        if status >= 400:
            raise ProviderUnavailable("provider_rejected", "The hosted AI provider rejected the request.")
        return body


def provider_for_settings(settings: Any) -> OllamaTextProvider | OpenAITextProvider | None:
    effective = settings.provider_mode if settings.enabled else "disabled"
    if effective == "local_free":
        return OllamaTextProvider(
            settings.local_base_url,
            settings.local_model,
            settings.request_timeout_seconds,
        )
    if effective == "hosted_paid":
        return OpenAITextProvider(settings.hosted_model, settings.request_timeout_seconds)
    return None


def image_provider_for_settings(settings: Any) -> OpenAIImageProvider | None:
    effective = settings.provider_mode if settings.enabled else "disabled"
    if effective != "hosted_paid":
        return None
    return OpenAIImageProvider(
        settings.image_model,
        max(60, settings.request_timeout_seconds),
        safety_model=settings.hosted_model,
    )


def video_provider_for_settings(settings: Any) -> OllamaTextProvider | OpenAITextProvider | None:
    effective = settings.provider_mode if settings.enabled else "disabled"
    if effective == "hosted_paid":
        # Hosted video rendering is intentionally not a production capability.
        # Video requests remain reviewable VIDEO_SPEC fallbacks.
        return None
    return provider_for_settings(settings)


def provider_status(settings: Any) -> dict[str, Any]:
    provider = provider_for_settings(settings)
    if provider is None:
        return {
            "provider": "disabled",
            "configured": True,
            "reachable": False,
            "model": None,
            "model_available": False,
            "message": "AI is disabled. Regeneration uses the deterministic fallback.",
        }
    return provider.health()