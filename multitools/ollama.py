"""Client Ollama partagé et file de génération bornée."""

from base64 import b64encode
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from io import BytesIO
import json
from pathlib import Path
import shutil
from threading import BoundedSemaphore, Event, Lock
from time import monotonic

from PIL import Image, ImageOps

from .local_http import HttpStatusError, LocalHTTP, LocalServiceError, check_cancel


DEFAULT_MODEL = "qwen3.5:2b-q4_K_M"


def _local_model(name: str, metadata: dict) -> bool:
    return bool(name) and not ("cloud" in name.casefold().split(":")[-1]
                               or metadata.get("remote_host") or metadata.get("remote_model"))


@dataclass(frozen=True)
class ChatResult:
    text: str
    seconds: float
    model_bytes: int | None
    tokens: int | None


def _image(path: Path) -> str:
    with Image.open(path) as original:
        original.draft("RGB", (640, 640))
        original.thumbnail((640, 640), Image.Resampling.LANCZOS)
        image = ImageOps.exif_transpose(original).convert('RGBA')
        background = Image.new('RGBA', image.size, 'white')
        background.alpha_composite(image)
        image = background.convert('RGB')
        with BytesIO() as buffer:
            image.convert("RGB").save(buffer, "JPEG", quality=80)
            return b64encode(buffer.getvalue()).decode("ascii")


class Ollama:
    def __init__(self, host="127.0.0.1", port=11434):
        self.http = LocalHTTP(host, port)
        self.last_model = None
        self.generation = Lock()

    @staticmethod
    def installed() -> bool:
        return shutil.which("ollama") is not None

    def models(self, cancel=None) -> list[str]:
        data = self.http.json("/api/tags", cancel=cancel)
        return sorted(model["name"] for model in data.get("models", []) if _local_model(model.get("name", ""), model))

    def details(self, name: str, cancel=None) -> dict:
        if name not in self.models(cancel):
            raise LocalServiceError("Choisis un modèle local déjà présent dans Ollama.")
        data = self.http.json("/api/show", {"model": name}, cancel=cancel)
        if not _local_model(name, data):
            raise LocalServiceError("Ce modèle est distant.")
        return data

    def unload(self, model: str, cancel=None) -> None:
        try:
            self.http.json("/api/generate", {"model": model, "keep_alive": 0, "stream": False}, cancel=cancel)
        except HttpStatusError as error:
            if error.status != 404:
                raise

    def chat(self, model: str, messages: list[dict], on_text, cancel: Event, *, context=2048,
             image: Path | None = None, keep_alive="2m", schema=None) -> ChatResult:
        if not 2048 <= context <= 4096:
            raise ValueError("Choisis un contexte entre 2048 et 4096 tokens.")
        while not self.generation.acquire(timeout=0.12):
            check_cancel(cancel)
        try:
            return self._chat(model, messages, on_text, cancel, context, image, keep_alive, schema)
        finally:
            self.generation.release()

    def _chat(self, model, messages, on_text, cancel, context, image, keep_alive, schema):
        check_cancel(cancel)
        details = self.details(model, cancel)
        capabilities = details.get("capabilities", [])
        messages = [{"role": item["role"], "content": item["content"]} for item in messages]
        if image is not None:
            if "vision" not in capabilities:
                raise LocalServiceError("Le modèle sélectionné ne déclare pas de capacité visuelle.")
            messages[-1]["images"] = [_image(image)]
        if self.last_model and self.last_model != model:
            self.unload(self.last_model, cancel)
        self.last_model = model
        body = {"model": model, "messages": messages, "stream": True, "keep_alive": keep_alive,
                "options": {"num_ctx": context, "num_predict": 512}}
        if "thinking" in capabilities:
            body["think"] = False
        if schema is not None:
            body["format"] = schema
        started = monotonic()
        parts = []
        final = None
        with self.http.open("/api/chat", body, cancel=cancel, timeout=180) as response:
            while line := response.readline(256 * 1024 + 1):
                check_cancel(cancel)
                if len(line) > 256 * 1024:
                    raise LocalServiceError("Un fragment Ollama est trop volumineux.")
                try:
                    event = json.loads(line)
                except ValueError as error:
                    raise LocalServiceError("Flux Ollama invalide.") from error
                if event.get("error"):
                    raise LocalServiceError(event["error"])
                token = event.get("message", {}).get("content", "")
                if token:
                    parts.append(token)
                    on_text(token)
                if event.get("done"):
                    final = event
                    break
        check_cancel(cancel)
        if final is None:
            raise LocalServiceError("Le flux Ollama s’est interrompu avant la fin.")
        size = None
        try:
            runners = self.http.json("/api/ps", cancel=cancel, timeout=3).get("models", [])
            size = next((r.get("size") for r in runners if r.get("name", r.get("model")) == model), None)
        except LocalServiceError:
            pass
        return ChatResult("".join(parts), monotonic() - started, size, final.get("eval_count"))


class AssistantQueue:
    def __init__(self):
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="multitools-ia")
        self.slots = BoundedSemaphore(4)

    def submit(self, job):
        if not self.slots.acquire(blocking=False):
            raise LocalServiceError("La file IA est pleine.")
        try:
            future = self.executor.submit(job)
        except RuntimeError:
            self.slots.release()
            raise
        future.add_done_callback(lambda _: self.slots.release())
        return future

    def close(self):
        self.executor.shutdown(wait=False, cancel_futures=True)
