"""
AlterChat - ai_engine.py
========================

OpenAI-compatible cloud API engine for AlterChat.
Communicates with https://bandelbanget.xyz/v1 using the `openai` SDK.
"""

from __future__ import annotations

import logging
import os
import threading
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional, Union

from dotenv import load_dotenv
from openai import OpenAI

# Automatically load environment variables from .env
load_dotenv()

logger = logging.getLogger("alterchat.ai")

Message = dict[str, str]
MessagesSource = Union[list[Message], Callable[[], list[Message]]]

TokenCallback = Callable[[str], None]
DoneCallback = Callable[[str, bool], None]      # (full_text, was_cancelled)
ErrorCallback = Callable[[str], None]
LoadCallback = Callable[[bool, str], None]      # (success, message)
ResetCallback = Callable[[], None]              # kept for GUI compatibility

DEFAULT_BASE_URL = "https://bandelbanget.xyz/v1"
DEFAULT_MODEL = "auto"
DEFAULT_TEMPERATURE = 0.7
DEFAULT_MAX_TOKENS = 250


class EngineState(str, Enum):
    """Lifecycle of the API engine."""

    NOT_LOADED = "not_loaded"
    LOADING = "loading"
    READY = "ready"
    ERROR = "error"


def build_system_instruction(contact_name: str = "") -> str:
    """Return the strong Tsundere Indonesian slang system instruction."""
    name = contact_name.strip() if contact_name else "Teman"
    return (
        f"Kamu adalah {name}. Sifatmu tsundere, ketus, gengsi, "
        f"tapi diam-diam peduli. Balas dengan bahasa Indonesia gaul, 1-2 kalimat pendek saja."
    )


def build_guarded_messages(
    messages: list[Message],
    persona_name: str = "",
    **kwargs: Any,
) -> list[Message]:
    """Construct prompt messages for OpenAI chat completion.

    Injects the strong Tsundere persona instruction and integrates the full
    context (including raw WhatsApp history / memories) safely.
    """
    contact_name = persona_name.strip() if persona_name else "Teman"
    system_instruction = build_system_instruction(contact_name)

    raw_system_parts: list[str] = []
    dialogue_turns: list[Message] = []

    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content", "")
        if not content:
            continue
        if role == "system":
            raw_system_parts.append(content.strip())
        else:
            dialogue_turns.append({"role": role, "content": content})

    # Combine the strong instruction with any imported context / memories
    full_system = system_instruction
    if raw_system_parts:
        extra_context = "\n\n".join(raw_system_parts)
        if system_instruction not in extra_context:
            full_system = (
                f"{system_instruction}\n\n"
                f"Gunakan konteks, gaya bicara, dan ingatan percakapan lampau berikut sebagai referensi:\n"
                f"{extra_context}"
            )
        else:
            full_system = extra_context

    result: list[Message] = [{"role": "system", "content": full_system}]
    result.extend(dialogue_turns)
    return result


class AIEngine:
    """Thread-safe manager for OpenAI-compatible API inference."""

    def __init__(self) -> None:
        self._client: Optional[OpenAI] = None
        self._state = EngineState.NOT_LOADED
        self._error = ""
        self._base_url = DEFAULT_BASE_URL
        self._model = DEFAULT_MODEL
        self._state_lock = threading.Lock()
        self._infer_lock = threading.Lock()
        self._cancel_event = threading.Event()

    # ------------------------------------------------------------ properties
    @property
    def state(self) -> EngineState:
        return self._state

    @property
    def error(self) -> str:
        return self._error

    @property
    def model_path(self) -> Optional[Path]:
        return None

    @property
    def is_ready(self) -> bool:
        return self._state == EngineState.READY and self._client is not None

    @property
    def is_busy(self) -> bool:
        return self._infer_lock.locked()

    # --------------------------------------------------------------- loading
    def load_model_async(
        self,
        model_path: Any = None,
        *,
        on_done: Optional[LoadCallback] = None,
        **kwargs: Any,
    ) -> bool:
        """Initialize or reload the OpenAI client from .env on a background thread."""
        with self._state_lock:
            if self._state == EngineState.LOADING:
                logger.info("load_model_async ignored: already loading")
                return False
            self._state = EngineState.LOADING
            self._error = ""

        thread = threading.Thread(
            target=self._load_worker,
            args=(on_done,),
            name="alterchat-api-loader",
            daemon=True,
        )
        thread.start()
        return True

    def _load_worker(self, on_done: Optional[LoadCallback]) -> None:
        """Background thread body: load .env and instantiate OpenAI client."""
        try:
            load_dotenv(override=True)
            api_key = os.getenv("CUSTOM_API_KEY", "").strip()
            base_url = os.getenv("CUSTOM_BASE_URL", DEFAULT_BASE_URL).strip()
            model = os.getenv("CUSTOM_MODEL_NAME", DEFAULT_MODEL).strip()

            if not api_key:
                raise ValueError(
                    "CUSTOM_API_KEY belum diisi. Buat file .env lalu isi CUSTOM_API_KEY=your_key_here"
                )

            client = OpenAI(
                api_key=api_key,
                base_url=base_url,
            )

            with self._state_lock:
                self._client = client
                self._base_url = base_url
                self._model = model
                self._state = EngineState.READY
                self._error = ""

            msg = f"Connected to custom API ({base_url}) [model: {model}]"
            logger.info(msg)
            self._safe_call(on_done, True, msg)
        except Exception as exc:  # noqa: BLE001
            err_msg = str(exc)
            logger.exception("Failed to initialize API client: %s", err_msg)
            with self._state_lock:
                self._client = None
                self._state = EngineState.ERROR
                self._error = err_msg
            self._safe_call(on_done, False, err_msg)

    # ------------------------------------------------------------- inference
    def generate_async(
        self,
        messages: MessagesSource,
        *,
        persona_name: str = "",
        on_token: Optional[TokenCallback] = None,
        on_done: Optional[DoneCallback] = None,
        on_error: Optional[ErrorCallback] = None,
        on_reset: Optional[ResetCallback] = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        temperature: float = DEFAULT_TEMPERATURE,
        **kwargs: Any,
    ) -> bool:
        """Start streaming chat completion on a background thread."""
        if not self.is_ready:
            self._safe_call(on_error, self._error or "AI Engine belum siap atau CUSTOM_API_KEY belum diatur.")
            return False
        if not self._infer_lock.acquire(blocking=False):
            self._safe_call(on_error, "AI Engine sedang memproses pesan lain.")
            return False

        self._cancel_event.clear()
        thread = threading.Thread(
            target=self._generate_worker,
            args=(messages, persona_name, max_tokens, temperature, on_token, on_done, on_error),
            name="alterchat-api-inference",
            daemon=True,
        )
        try:
            thread.start()
        except Exception as exc:
            self._infer_lock.release()
            self._safe_call(on_error, f"Gagal memulai thread inferensi: {exc}")
            return False
        return True

    def _generate_worker(
        self,
        messages: MessagesSource,
        persona_name: str,
        max_tokens: int,
        temperature: float,
        on_token: Optional[TokenCallback],
        on_done: Optional[DoneCallback],
        on_error: Optional[ErrorCallback],
    ) -> None:
        """Background thread body: stream chat completion from OpenAI API."""
        final_text = ""
        cancelled = False
        error: Optional[str] = None
        try:
            raw = messages() if callable(messages) else list(messages)
            raw = [dict(m) for m in raw if m.get("content") is not None]

            prompt = build_guarded_messages(raw, persona_name=persona_name)
            final_text, cancelled = self._stream_completion(prompt, max_tokens, temperature, on_token)
        except Exception as exc:  # noqa: BLE001
            logger.exception("API completion failed")
            error = f"{type(exc).__name__}: {exc}"
        finally:
            self._infer_lock.release()

        if error is not None:
            self._safe_call(on_error, error)
        else:
            self._safe_call(on_done, final_text, cancelled)

    def _stream_completion(
        self,
        prompt: list[Message],
        max_tokens: int,
        temperature: float,
        on_token: Optional[TokenCallback],
    ) -> tuple[str, bool]:
        """Stream chunks from the OpenAI API endpoint."""
        if self._client is None:
            raise RuntimeError("API client is not initialized")

        chunks: list[str] = []
        stream = self._client.chat.completions.create(
            model=self._model,
            messages=prompt,  # type: ignore[arg-type]
            stream=True,
            temperature=float(temperature),
            max_tokens=int(max_tokens),
        )
        try:
            for chunk in stream:
                if self._cancel_event.is_set():
                    return "".join(chunks), True
                choices = chunk.choices or []
                if not choices:
                    continue
                piece = choices[0].delta.content or ""
                if piece:
                    chunks.append(piece)
                    self._safe_call(on_token, piece)
        finally:
            close = getattr(stream, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass
        return "".join(chunks), False

    def cancel(self) -> None:
        """Signal current generation to cancel immediately."""
        self._cancel_event.set()

    # -------------------------------------------------------------- shutdown
    def shutdown(self, timeout: float = 3.0) -> None:
        """Release client state and cancel ongoing generation."""
        self.cancel()
        acquired = self._infer_lock.acquire(timeout=timeout)
        try:
            with self._state_lock:
                self._client = None
                self._state = EngineState.NOT_LOADED
        finally:
            if acquired:
                self._infer_lock.release()

    # ----------------------------------------------------------------- utils
    @staticmethod
    def _safe_call(callback: Optional[Callable[..., Any]], *args: Any) -> None:
        """Safely invoke callback without crashing the worker thread."""
        if callback is None:
            return
        try:
            callback(*args)
        except Exception:
            logger.exception("Engine callback raised an exception")


# ---------------------------------------------------------------------------
# Global singleton
# ---------------------------------------------------------------------------

_engine: Optional[AIEngine] = None
_engine_guard = threading.Lock()


def get_engine() -> AIEngine:
    """Return the process-wide AIEngine singleton."""
    global _engine
    with _engine_guard:
        if _engine is None:
            _engine = AIEngine()
        return _engine
