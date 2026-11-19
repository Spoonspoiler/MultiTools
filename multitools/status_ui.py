from pathlib import Path
import subprocess
import shutil
import sys
import tkinter as tk
from tkinter import ttk

from .kiwix import KiwixServer
from .local_http import LocalServiceError
from .server_tasks import WorkerPane
from .storage import ROLES, backup_independence, mounts, verify_mount


class StoragePane(WorkerPane):
    def __init__(self, parent, services):
        super().__init__(parent, services)
        self.inventory = []
        self.roles = {}
        self.messages_by_role = {}
        ttk.Button(self, text="Détecter les montages", command=self.refresh).pack(anchor="w")
        self.mounts = ttk.Treeview(self, columns=("mount", "disks", "total", "used", "free"), show="headings", height=7)
        for column, label, width in (("mount", "Montage", 200), ("disks", "Disque physique", 145),
                                     ("total", "Capacité", 85), ("used", "Occupé", 85), ("free", "Disponible", 105)):
            self.mounts.heading(column, text=label)
            self.mounts.column(column, width=width)
        self.mounts.pack(fill="both", expand=True, pady=9)
        ttk.Button(self, text="Ouvrir le disque sélectionné", command=self.open_mount).pack(anchor="w")
        self.mounts.bind("<Double-1>", lambda _event: self.open_mount())
        form = ttk.Frame(self)
        form.pack(fill="x")
        form.columnconfigure(2, weight=1)
        roles = services.config.get("storage_roles", {})
        for row, role in enumerate(ROLES):
            ttk.Label(form, text=role, width=12).grid(row=row, column=0, sticky="w", pady=3)
            select = ttk.Combobox(form, state="readonly", width=23)
            select.set(roles.get(role, {}).get("path", ""))
            select.grid(row=row, column=1, sticky="w", padx=6)
            self.roles[role] = select
            label = tk.StringVar(self, "À vérifier" if select.get() else "À définir")
            self.messages_by_role[role] = label
            ttk.Label(form, textvariable=label).grid(row=row, column=2, sticky="w")
        ttk.Button(form, text="Enregistrer les rôles", command=self.save).grid(row=3, column=1, sticky="w", pady=8)
        ttk.Label(self, textvariable=self.status, wraplength=800).pack(fill="x")
        self.after_idle(self.refresh)

    def open_mount(self):
        selected = self.mounts.selection()
        if not selected:
            self.status.set("Sélectionne un disque dans la liste.")
            return
        path = Path(self.mounts.item(selected[0], "values")[0])
        expected = next((mount for mount in self.inventory if mount.path == path), None)
        if expected is None:
            self.status.set("Actualise la liste des disques.")
            return

        def work(gate, _):
            gate.check()
            verify_mount({"path": str(path), "uuid": expected.uuid}, mounts())
            if not path.is_dir():
                raise OSError(f"Dossier indisponible · {path}")
            if sys.platform.startswith("linux"):
                subprocess.run(["xdg-open", str(path)], check=True, timeout=15,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            elif sys.platform == "win32":
                import os
                os.startfile(path)
            else:
                subprocess.run(["open", str(path)], check=True, timeout=15)
        self.launch("Ouverture du disque…", work,
                    lambda _: self.status.set(f"Disque ouvert · {path}"))

    def refresh(self):
        def work(gate, _):
            result = []
            for mount in mounts():
                gate.check()
                if sys.platform.startswith("linux") and not mount.uuid:
                    continue
                try:
                    usage = shutil.disk_usage(mount.path)
                except OSError:
                    usage = None
                result.append((mount, usage))
            return result
        self.launch("Lecture des points de montage…", work, self.show)

    def show(self, rows):
        self.inventory = [mount for mount, _ in rows]
        self.mounts.delete(*self.mounts.get_children())
        for mount, usage in rows:
            sizes = tuple(f"{value / 1024**3:.1f} Gio" for value in usage) if usage else ("", "", "")
            self.mounts.insert("", "end", values=(str(mount.path), ", ".join(sorted(mount.disks)), *sizes))
        options = ["", *(str(mount.path) for mount in self.inventory)]
        for field in self.roles.values():
            field["values"] = options
        self.report_roles()
        self.status.set(f"{len(rows)} point(s) de montage détecté(s)")

    def report_roles(self):
        connected = {}
        for role, label in self.messages_by_role.items():
            spec = self.services.config.get("storage_roles", {}).get(role)
            if not spec:
                label.set("À définir")
                continue
            try:
                connected[role] = verify_mount(spec, self.inventory)
                label.set("Disponible")
            except OSError as error:
                label.set(str(error))
        if "DATA" in connected and "BACKUP" in connected:
            self.messages_by_role["BACKUP"].set(backup_independence(connected["DATA"], connected["BACKUP"]))

    def save(self):
        if self.busy:
            return
        current = self.services.config.get("storage_roles", {})
        known = {str(mount.path): mount for mount in self.inventory}
        roles = {}
        for role, entry in self.roles.items():
            path = entry.get()
            if not path:
                continue
            if current.get(role, {}).get("path") == path:
                roles[role] = current[role]
            elif path in known:
                roles[role] = {"path": path, "uuid": known[path].uuid}
            else:
                self.status.set("Détecte les montages avant d’attribuer un nouveau rôle.")
                return
        try:
            self.services.save(storage_roles=roles)
            self.report_roles()
            self.status.set("Rôles enregistrés.")
        except OSError as error:
            self.status.set(str(error))


class StatusPane(WorkerPane):
    def __init__(self, parent, services):
        super().__init__(parent, services)
        ttk.Button(self, text="Vérifier les services locaux", command=self.refresh).pack(anchor="w")
        self.details = tk.StringVar(self, f"Configuration · {services.directory}")
        ttk.Label(self, textvariable=self.details, justify="left", wraplength=860).pack(fill="both", pady=12)
        ttk.Label(self, textvariable=self.status, wraplength=860).pack(fill="x")

    def refresh(self):
        def work(gate, _):
            ollama = self.services.ollama
            output = [f"Ollama dans le PATH · {'oui' if ollama.installed() else 'non'}"]
            try:
                version = ollama.http.json("/api/version", cancel=gate.stopped).get("version", "inconnue")
                models = ollama.models(gate.stopped)
                output.append(f"Ollama {version} répond · {len(models)} modèle(s) local(aux)")
            except LocalServiceError as error:
                output.append(str(error))
            output.append(f"kiwix-serve · {KiwixServer.executable() or 'absent du PATH'}")
            if (kiwix := self.services.kiwix.api) is not None:
                try:
                    kiwix.http.read("/catalog/v2/entries?count=1", cancel=gate.stopped, timeout=4)
                    output.append(f"Kiwix répond · {kiwix.url}")
                except LocalServiceError as error:
                    output.append(str(error))
            if sys.platform.startswith("linux"):
                try:
                    mem = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())
                    size = int(mem["MemAvailable"].split()[0]) / 1024**2
                    output.append(f"Mémoire disponible · {size:.1f} Gio")
                except (OSError, ValueError, KeyError):
                    pass
            return "\n".join(output)
        self.launch("Vérification des services…", work, self.details.set)
