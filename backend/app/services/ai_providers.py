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
    prompt = usage.get("prompt_tokens", usage.get("prompt_eval_count"))
    completion = usage.get("completion_tokens", usage.get("eval_count"))
    total = usage.get("total_tokens")
    if total is None and isinstance(prompt, int) and isinstance(completion, int):
        total = prompt + completion
    return prompt if isinstance(prompt, int) else None, completion if isinstance(completion, int) else None, total if isinstance(total, int) else None


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

    def generate(self, prompt: str) -> TextGenerationResult:
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
        self.base_url = (base_url or os.getenv("OPENAI_BASE_URL") or "https://api.openai.com/v1").rstrip("/")
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

    def generate(self, prompt: str) -> TextGenerationResult:
        body = self._request("POST", "/chat/completions", {
            "model": self.model,
            "temperature": 0.2,
            "max_tokens": 600,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": "You write concise catalog-grounded social copy. Return JSON only."},
                {"role": "user", "content": prompt},
            ],
        })
        choices = body.get("choices") if isinstance(body, dict) else None
        text = choices[0].get("message", {}).get("content") if choices else None
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
        self.base_url = (base_url or os.getenv("OPENAI_BASE_URL") or "https://api.openai.com/v1").rstrip("/")
        self.safety_model = safety_model
        self.requester = requester or _http_request

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def _request(self, payload: dict[str, Any]):
        if not self.api_key:
            raise ProviderUnavailable("missing_credentials", "The hosted AI provider is not configured.")
        status, body = self.requester(
            "POST",
            f"{self.base_url}/images/generations",
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
            "/chat/completions",
            {
                "model": self.safety_model,
                "temperature": 0,
                "max_tokens": 120,
                "response_format": {"type": "json_object"},
                "messages": [{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": (
                            "Classify this candidate decorative background. Return JSON only with boolean keys "
                            "background_only, contains_product, contains_packaging, contains_logo, contains_text, "
                            "contains_person. background_only may be true only if every contains_* value is false. "
                            "If uncertain, set background_only false."
                        )},
                        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{encoded}", "detail": "low"}},
                    ],
                }],
            },
        )
        try:
            raw = body["choices"][0]["message"]["content"]
            decision = json.loads(raw)
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
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
        return OpenAITextProvider(settings.video_model, settings.request_timeout_seconds)
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