"""Credential-safe text provider adapters for review-only regeneration."""
from __future__ import annotations

import json
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