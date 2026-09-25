"""Small synchronous OpenAI-compatible gateway; no inference retries or secret logs.

Pass ``ModelGateway(SecretStore(path).get)`` to look up keys per connection.
On Windows keys are encrypted for the current OS user with DPAPI. Elsewhere
SecretStore is memory-only; OPENROUTER_API_KEY can be supplied by the environment.
"""

from __future__ import annotations

import base64
import ctypes
import ipaddress
import json
import math
import os
from pathlib import Path
import threading
import time
from collections.abc import Callable, Mapping
from urllib.parse import urlsplit, urlunsplit

import httpx

from .files import open_new


OPENROUTER_URL = "https://openrouter.ai/api/v1"


class ProviderError(Exception):
    """Safe to show to a user: never includes upstream response bodies or keys.

    ``code`` is a stable machine-readable class (unreachable, timeout, credential-missing,
    credential-rejected, model-missing, ...) so callers never parse the Spanish message.
    """

    def __init__(self, message: str, code: str = "provider-error") -> None:
        super().__init__(message)
        self.code = code


def validate_connection(connection: dict) -> dict:
    if not isinstance(connection, dict):
        raise ProviderError("La conexión debe ser un objeto.")
    provider = connection.get("provider", "local")
    if provider not in ("local", "openrouter"):
        raise ProviderError("Proveedor no compatible; elige Local u OpenRouter.")
    fields = {}
    for field in ("id", "name", "model"):
        value = connection.get(field, "")
        if not isinstance(value, str) or len(value) > 300 or any(ord(c) < 32 for c in value):
            raise ProviderError("Identificador, nombre o modelo inválido.")
        fields[field] = value.strip()
    if not fields["model"]:
        raise ProviderError("Indica el identificador del modelo.")
    fields["name"] = fields["name"] or fields["model"]
    url = connection.get("url") or (OPENROUTER_URL if provider == "openrouter" else "")
    if not isinstance(url, str) or any(ord(c) <= 32 for c in url) or "\\" in url:
        raise ProviderError("URL de conexión inválida.")
    try:
        parts = urlsplit(url)
        port = parts.port
        if parts.username is not None or parts.password is not None or parts.query or parts.fragment:
            raise ValueError()
        if parts.scheme not in ("http", "https") or not parts.hostname or not parts.path.rstrip("/").endswith("/v1"):
            raise ValueError()
        if provider == "openrouter":
            if url.rstrip("/") != OPENROUTER_URL:
                raise ValueError()
            url = OPENROUTER_URL
        else:
            host = parts.hostname.lower()
            if host != "localhost":
                address = ipaddress.ip_address(host)
                allowed = address.is_loopback or any(address in ipaddress.ip_network(net) for net in (
                    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "fc00::/7"
                ))
                if not allowed:
                    raise ValueError()
            if port == 0 or "%" in parts.netloc or "%" in parts.path or ".." in parts.path.split("/"):
                raise ValueError()
            url = urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))
    except (ValueError, TypeError):
        raise ProviderError("Usa OpenRouter oficial o una URL local/LAN/Tailscale con IP explícita terminada en /v1, sin credenciales ni parámetros.") from None
    return {**fields, "provider": provider, "url": url}


class ModelGateway:
    def __init__(self, api_keys: Mapping[str, str] | Callable[[str], str | None] | None = None, *, client: httpx.Client | None = None):
        self.api_keys = api_keys if api_keys is not None else {}
        self.client = client

    def _headers(self, connection: dict, *, require_key: bool) -> dict:
        try:
            key = self.api_keys(connection["id"]) if callable(self.api_keys) else self.api_keys.get(connection["id"])
        except Exception:
            raise ProviderError("No se pudo recuperar la credencial de esta conexión.") from None
        if not key and connection["provider"] == "openrouter":
            key = os.environ.get("OPENROUTER_API_KEY")
        if require_key and connection["provider"] == "openrouter" and not key:
            raise ProviderError("Falta la API key de OpenRouter para esta conexión.", code="credential-missing")
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if key:
            if not isinstance(key, str) or any(ord(c) < 33 or ord(c) > 126 for c in key):
                raise ProviderError("La credencial no tiene un formato válido.")
            headers["Authorization"] = "Bearer " + key
        return headers

    def _request(self, connection: dict, method: str, route: str, timeout: float, payload: dict | None = None) -> dict:
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not math.isfinite(timeout) or not 0 < timeout <= 600:
            raise ProviderError("El timeout debe estar entre 0 y 600 segundos.")
        headers = self._headers(connection, require_key=method == "POST")
        client = self.client or httpx.Client(trust_env=False)
        try:
            response = client.request(method, connection["url"] + route, headers=headers, json=payload, timeout=timeout, follow_redirects=False)
            if not 200 <= response.status_code < 300:
                reason = {401: "Credencial rechazada", 403: "Acceso denegado", 402: "Saldo insuficiente", 404: "Ruta o modelo no disponible", 429: "Límite del proveedor alcanzado"}.get(response.status_code, "El proveedor rechazó la solicitud")
                code = {401: "credential-rejected", 403: "credential-rejected", 402: "billing", 404: "not-found", 429: "rate-limit"}.get(response.status_code, "http-error")
                raise ProviderError(f"{reason} (HTTP {response.status_code}).", code=code)
            try:
                data = response.json()
            except (ValueError, UnicodeError):
                raise ProviderError("El proveedor devolvió una respuesta que no es JSON válido.", code="invalid-response") from None
            if not isinstance(data, dict) or data.get("error"):
                raise ProviderError("El proveedor devolvió una respuesta de error o inesperada.", code="invalid-response")
            return data
        except httpx.TimeoutException:
            raise ProviderError("El proveedor excedió el tiempo de espera; la solicitud no se reintentó.", code="timeout") from None
        except httpx.HTTPError:
            raise ProviderError("No se pudo conectar con el proveedor; revisa la URL y el servidor.", code="unreachable") from None
        finally:
            if self.client is None:
                client.close()

    def complete(self, connection: dict, messages: list[dict], max_tokens: int = 1024, timeout: float = 60) -> dict:
        connection = validate_connection(connection)
        if not isinstance(max_tokens, int) or isinstance(max_tokens, bool) or not 1 <= max_tokens <= 32768:
            raise ProviderError("max_tokens debe estar entre 1 y 32768.")
        if not isinstance(messages, list) or not messages or len(messages) > 200:
            raise ProviderError("Se requieren entre 1 y 200 mensajes.")
        cleaned = []
        for message in messages:
            if not isinstance(message, dict) or message.get("role") not in ("system", "user", "assistant") or not isinstance(message.get("content"), str):
                raise ProviderError("Los mensajes deben tener rol system, user o assistant y contenido de texto.")
            cleaned.append({"role": message["role"], "content": message["content"]})
        if sum(len(item["content"]) for item in cleaned) > 2_000_000:
            raise ProviderError("El contenido de los mensajes excede el límite permitido.")
        started = time.monotonic()
        data = self._request(connection, "POST", "/chat/completions", timeout, {"model": connection["model"], "messages": cleaned, "max_tokens": max_tokens, "stream": False})
        latency = round((time.monotonic() - started) * 1000, 2)
        try:
            choice = data["choices"][0]
            content = choice["message"]["content"]
            if not isinstance(content, str) or not content.strip():
                raise ValueError()
            if choice.get("finish_reason") == "length":
                raise ProviderError("La respuesta del modelo quedó incompleta: alcanzó el límite de tokens de salida.", code="output-truncated")
            if choice.get("finish_reason") in ("content_filter", "error"):
                raise ProviderError("La respuesta del modelo quedó incompleta o fue filtrada; revisa el límite de salida.")
        except (KeyError, IndexError, TypeError, ValueError):
            raise ProviderError("El modelo no devolvió una respuesta de texto utilizable.") from None
        raw_usage = data.get("usage") or {}
        if not isinstance(raw_usage, dict):
            raw_usage = {}
        usage = {key: value if isinstance(value := raw_usage.get(key), int) and not isinstance(value, bool) and value >= 0 else None for key in ("prompt_tokens", "completion_tokens", "total_tokens")}
        cost = raw_usage.get("cost")
        cost = float(cost) if isinstance(cost, (int, float)) and not isinstance(cost, bool) and math.isfinite(cost) and cost >= 0 else None
        return {"text": content, "latencyMs": latency, "usage": usage, "costUsd": cost}

    def models(self, connection: dict, timeout: float = 10) -> list[dict]:
        # Catalog discovery happens before the user can select a model.
        if not isinstance(connection, dict):
            raise ProviderError("La conexión debe ser un objeto.")
        connection = validate_connection({**connection, "model": connection.get("model") or "catalog-discovery"})
        data = self._request(connection, "GET", "/models", timeout)
        if not isinstance(data.get("data"), list):
            raise ProviderError("El proveedor no devolvió un catálogo de modelos válido.", code="invalid-response")
        result = []
        for item in data["data"]:
            if isinstance(item, dict) and isinstance(item.get("id"), str) and item["id"]:
                result.append({"id": item["id"], "name": item.get("name") if isinstance(item.get("name"), str) else item["id"]})
        return result

    def check(self, connection: dict) -> dict:
        connection = validate_connection(connection)
        models = self.models(connection)
        if connection["model"] not in {model["id"] for model in models}:
            raise ProviderError("El modelo elegido no aparece en el catálogo de esta conexión.", code="model-missing")
        return {"ok": True, "models": models, "model": connection["model"], "inferenceTested": False, "authenticationTested": False}


def _dpapi(data: bytes, *, decrypt: bool = False) -> bytes:
    """DPAPI user-bound encryption, with UI explicitly forbidden."""
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]

    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    output = Blob()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    method = crypt32.CryptUnprotectData if decrypt else crypt32.CryptProtectData
    method.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    method.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    if not method(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(output)):
        raise ProviderError("No se pudo proteger o recuperar la credencial con la cuenta de Windows actual.")
    try:
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        kernel32.LocalFree(output.pbData)


class SecretStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.persistent = os.name == "nt"
        self._lock = threading.RLock()
        self._entries: dict[str, str] = {}
        if self.persistent and self.path.exists():
            try:
                document = json.loads(self.path.read_text(encoding="utf-8"))
                entries = document["keys"]
                if document.get("format") != "dpapi-user-v1" or not isinstance(entries, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in entries.items()):
                    raise ValueError()
                self._entries = entries
            except (OSError, ValueError, KeyError, TypeError):
                raise ProviderError("No se pudo leer el almacén de credenciales cifradas.") from None

    def _save(self, entries: dict[str, str]) -> None:
        if not self.persistent:
            return
        temporary = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle, temporary = open_new(self.path.parent, ".credentials-", ".tmp")
            with handle:
                json.dump({"format": "dpapi-user-v1", "keys": entries}, handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        except OSError:
            raise ProviderError("No se pudo guardar la credencial cifrada.") from None
        finally:
            if temporary and temporary.exists():
                temporary.unlink(missing_ok=True)

    def set(self, connection_id: str, key: str) -> None:
        if not isinstance(connection_id, str) or not connection_id or not isinstance(key, str) or not key or len(key) > 8192 or any(ord(c) < 33 or ord(c) > 126 for c in key):
            raise ProviderError("La conexión o credencial no tiene un formato válido.")
        with self._lock:
            value = base64.b64encode(_dpapi(key.encode())).decode("ascii") if self.persistent else key
            entries = {**self._entries, connection_id: value}
            self._save(entries)
            self._entries = entries

    def get(self, connection_id: str) -> str | None:
        with self._lock:
            value = self._entries.get(connection_id)
            if value is None or not self.persistent:
                return value
            try:
                return _dpapi(base64.b64decode(value, validate=True), decrypt=True).decode("utf-8")
            except (ValueError, UnicodeError):
                raise ProviderError("No se pudo recuperar la credencial cifrada.") from None

    def has(self, connection_id: str) -> bool:
        with self._lock:
            return connection_id in self._entries

    def delete(self, connection_id: str) -> None:
        with self._lock:
            entries = dict(self._entries)
            entries.pop(connection_id, None)
            self._save(entries)
            self._entries = entries
