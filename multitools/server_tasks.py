"""Configuration portable et liaison entre workers et interface Tkinter."""

import json
from datetime import datetime, timezone
from pathlib import Path
from queue import Empty, SimpleQueue
from tempfile import NamedTemporaryFile
from threading import Thread
import tkinter as tk
from tkinter import ttk

from .documents import Documents
from .kiwix import KiwixServer
from .local_http import Cancelled
from .ollama import AssistantQueue, Ollama
from .photoarchiviste.inventory import Gate, TaskStopped


class Services:
    def __init__(self, directory: Path):
        self.directory = directory
        self.settings = directory / "settings.json"
        self.warning = ""
        self.config = {}
        try:
            config = json.loads(self.settings.read_text(encoding="utf-8"))
            if isinstance(config, dict):
                self.config = config
        except FileNotFoundError:
            pass
        except (ValueError, OSError) as error:
            self.warning = f"Configuration non chargée · {error}"
        self.ollama = Ollama()
        self.queue = AssistantQueue()
        self.kiwix = KiwixServer()
        self.documents = Documents(directory / "documents.sqlite3")
        self.book = None

    def paths(self, key):
        return [Path(value) for value in self.config.get(key, []) if isinstance(value, str)]

    def save(self, **values):
        updated = {**self.config, **values}
        self.directory.mkdir(exist_ok=True, parents=True)
        pending = None
        try:
            with NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.directory, delete=False) as file:
                pending = Path(file.name)
                json.dump(updated, file, ensure_ascii=False, indent=2)
            pending.replace(self.settings)
            self.config = updated
        finally:
            if pending and pending.exists():
                pending.unlink()

    def record_chat(self, model, context, result):
        self.directory.mkdir(exist_ok=True, parents=True)
        log = self.directory / "ollama.jsonl"
        if log.is_file() and log.stat().st_size >= 64 * 1024:
            log.replace(self.directory / "ollama.previous.jsonl")
        record = {"time": datetime.now(timezone.utc).isoformat(), "model": model, "context": context,
                  "seconds": round(result.seconds, 2), "model_bytes": result.model_bytes, "tokens": result.tokens}
        with log.open("a", encoding="utf-8") as file:
            file.write(json.dumps(record, ensure_ascii=False) + "\n")


class WorkerPane(ttk.Frame):
    def __init__(self, parent, services: Services):
        super().__init__(parent, padding=12)
        self.services = services
        self.status = tk.StringVar(self, services.warning or "Prêt")
        self.gate = None
        self.messages = SimpleQueue()

    @property
    def busy(self):
        return self.gate is not None

    def stop(self):
        if self.gate is not None and not self.gate.stopped.is_set():
            self.gate.stopped.set()
            self.gate.running.set()
            self.status.set("Annulation en cours…")

    def pause(self):
        if self.gate is None or self.gate.stopped.is_set():
            return
        if self.gate.running.is_set():
            self.gate.running.clear()
            self.status.set("Pause demandée")
        else:
            self.gate.running.set()
            self.status.set("Reprise…")

    def launch(self, title, work, on_result, on_update=None, *, queued=False):
        if self.busy:
            return False
        gate = self.gate = Gate()
        self.status.set(title)

        def emit(value):
            self.messages.put(("update", value))

        def run():
            try:
                gate.check()
                result = work(gate, emit)
            except (Cancelled, TaskStopped):
                self.messages.put(("cancelled", "Tâche annulée."))
            except Exception as error:
                self.messages.put(("error", str(error) or type(error).__name__))
            else:
                self.messages.put(("done", result))

        try:
            if queued:
                self.services.queue.submit(run)
            else:
                Thread(target=run, daemon=True, name="multitools-service").start()
        except Exception as error:
            self.status.set(str(error))
            self.gate = None
            return False

        def poll():
            for _ in range(100):
                try:
                    kind, value = self.messages.get_nowait()
                except Empty:
                    break
                if kind == "update":
                    if on_update:
                        on_update(value)
                    elif self.gate.running.is_set() and not self.gate.stopped.is_set():
                        self.status.set(value)
                    continue
                self.gate = None
                if kind == "done":
                    self.status.set("Terminé")
                    on_result(value)
                else:
                    self.status.set(value)
                return
            self.after(70, poll)

        self.after(70, poll)
        return True
