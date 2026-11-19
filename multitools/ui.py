from __future__ import annotations

from pathlib import Path
from queue import Empty, SimpleQueue
from threading import Event, Thread
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from .disk_picker import askdirectory

from .operations import IMAGES, MEDIA, ConversionCancelled, Operation, convert_file
from .photoarchiviste.ui import PhotoArchiviste
from .assistant_ui import AssistantPane
from .library_ui import LibraryPane
from .server_tasks import Services
from .status_ui import StatusPane, StoragePane
from .storage import guard_target
from .documentary.ui import DocumentaryPane


IMAGE_TYPES = [("Images", "*.png *.jpg *.jpeg *.jpe *.jfif *.webp *.avif *.bmp *.tif *.tiff *.gif *.ico"),
               ("Tous les fichiers", "*.*")]
MEDIA_TYPES = [("Vidéos et audio", "*.mp4 *.mov *.mkv *.webm *.avi *.m4v *.mts *.mxf *.mp3 *.wav *.m4a *.flac *.ogg *.opus"),
               ("Tous les fichiers", "*.*")]


class ConversionPanel(ttk.Frame):
    def __init__(self, parent, operations: tuple[Operation, ...], filetypes):
        super().__init__(parent, padding=20)
        self.operations = operations
        self.filetypes = filetypes
        self.files: dict[str, Path] = {}
        self.details: dict[str, str] = {}
        self.messages = SimpleQueue()
        self.cancelled: Event | None = None
        self.destination = tk.StringVar(self)
        self.status = tk.StringVar(self, value="Ajoute un ou plusieurs fichiers.")

        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        toolbar = ttk.Frame(self)
        toolbar.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        self.add_button = ttk.Button(toolbar, text="Ajouter des fichiers…", command=self._add_files)
        self.add_button.pack(side="left")
        self.remove_button = ttk.Button(toolbar, text="Retirer la sélection", command=self._remove_files)
        self.remove_button.pack(side="left", padx=8)

        filelist = ttk.Frame(self)
        filelist.grid(row=1, column=0, sticky="nsew")
        self.tree = ttk.Treeview(filelist, columns=("result",), height=7)
        self.tree.heading("#0", text="Fichier source", anchor="w")
        self.tree.heading("result", text="Résultat", anchor="w")
        self.tree.column("#0", width=320, minwidth=140)
        self.tree.column("result", width=300, minwidth=140)
        self.tree.pack(side="left", fill="both", expand=True)
        scrollbar = ttk.Scrollbar(filelist, command=self.tree.yview)
        scrollbar.pack(side="right", fill="y")
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.bind("<<TreeviewSelect>>", lambda _: self._update_buttons())
        self.tree.bind("<Double-1>", lambda event: self._show_details(self.tree.identify_row(event.y)))
        self.tree.bind("<Return>", lambda _: self._show_details(self.tree.focus()))
        self.tree.tag_configure("error", foreground="#a22b2b")
        self.tree.tag_configure("done", foreground="#176056")

        settings = ttk.Frame(self)
        settings.grid(row=2, column=0, sticky="ew", pady=(18, 14))
        settings.columnconfigure(1, weight=1)
        ttk.Label(settings, text="Format de sortie").grid(row=0, column=0, sticky="w", padx=(0, 12))
        self.format_box = ttk.Combobox(settings, state="readonly", values=[item.label for item in operations])
        self.format_box.grid(row=0, column=1, columnspan=2, sticky="ew")
        self.format_box.current(0)
        self.format_box.bind("<<ComboboxSelected>>", self._update_description)
        self.description = ttk.Label(settings, foreground="#485466")
        self.description.grid(row=1, column=1, columnspan=2, sticky="ew", pady=(4, 10))
        self._update_description()
        ttk.Label(settings, text="Dossier de sortie").grid(row=2, column=0, sticky="w", padx=(0, 12))
        self.destination_entry = ttk.Entry(settings, textvariable=self.destination)
        self.destination_entry.grid(row=2, column=1, sticky="ew")
        self.browse_button = ttk.Button(settings, text="Parcourir…", command=self._choose_destination)
        self.browse_button.grid(row=2, column=2, padx=(8, 0))

        actions = ttk.Frame(self)
        actions.grid(row=3, column=0, sticky="ew")
        self.progress = ttk.Progressbar(actions)
        self.progress.pack(side="left", fill="x", expand=True, padx=(0, 16))
        self.cancel_button = ttk.Button(actions, text="Annuler", command=self.request_cancel)
        self.cancel_button.pack(side="right", padx=(8, 0))
        self.run_button = ttk.Button(actions, text="Convertir", style="Convert.TButton", command=self._start)
        self.run_button.pack(side="right")
        ttk.Label(self, textvariable=self.status).grid(row=4, column=0, sticky="ew", pady=(10, 0))
        self._update_buttons()

    @property
    def busy(self) -> bool:
        return self.cancelled is not None

    def _update_description(self, _event=None) -> None:
        self.description.configure(text=self.operations[self.format_box.current()].description)

    def _update_buttons(self) -> None:
        idle = not self.busy
        for button, enabled in (
            (self.add_button, idle),
            (self.remove_button, idle and bool(self.tree.selection())),
            (self.browse_button, idle),
            (self.run_button, idle and bool(self.files)),
            (self.cancel_button, self.busy and not self.cancelled.is_set()),
        ):
            button.configure(state="normal" if enabled else "disabled")
        self.format_box.configure(state="readonly" if idle else "disabled")
        self.destination_entry.configure(state="normal" if idle else "disabled")

    def _add_files(self) -> None:
        selected = filedialog.askopenfilenames(parent=self, title="Choisir des fichiers", filetypes=self.filetypes)
        known = set(self.files.values())
        for filename in selected:
            path = Path(filename).resolve()
            if path not in known:
                iid = self.tree.insert("", "end", text=path.name)
                self.files[iid] = path
                known.add(path)
                if not self.destination.get():
                    self.destination.set(str(path.parent))
        if selected:
            self._update_file_count()

    def _remove_files(self) -> None:
        for iid in self.tree.selection():
            self.tree.delete(iid)
            self.files.pop(iid)
            self.details.pop(iid, None)
        self._update_file_count()

    def _update_file_count(self) -> None:
        count = len(self.files)
        self.status.set(f"Fichiers à convertir : {count}" if count else "Ajoute un ou plusieurs fichiers.")
        self._update_buttons()

    def _choose_destination(self) -> None:
        directory = askdirectory(parent=self, initialdir=self.destination.get(), title="Choisir le dossier de sortie")
        if directory:
            self.destination.set(directory)

    def _show_details(self, iid: str) -> None:
        if iid in self.files:
            messagebox.showinfo(self.files[iid].name, self.details.get(iid, str(self.files[iid])), parent=self)

    def _start(self) -> None:
        if self.busy or not self.files:
            return
        destination = self.destination.get().strip()
        directory = Path(destination).expanduser()
        if not destination or not directory.is_dir():
            messagebox.showerror("Dossier de sortie", "Choisis un dossier de sortie existant.", parent=self)
            return

        for iid in self.files:
            self.tree.item(iid, values=("En attente",), tags=())
        self.details.clear()
        self.cancelled = Event()
        self.progress.configure(maximum=len(self.files), value=0)
        self._update_buttons()
        operation = self.operations[self.format_box.current()]
        Thread(target=self._convert_batch, args=(list(self.files.items()), directory.resolve(), operation, self.cancelled),
               daemon=True).start()
        self.status.set("Conversion en cours…")
        self.after(75, self._poll)

    def _convert_batch(self, files, directory: Path, operation: Operation, cancelled: Event) -> None:
        for iid, path in files:
            self.messages.put(("working", iid, path.name))
            try:
                output = convert_file(path, directory, operation, cancelled)
            except ConversionCancelled:
                break
            except Exception as error:
                self.messages.put(("error", iid, str(error) or type(error).__name__))
            else:
                self.messages.put(("done", iid, output))
        self.messages.put(("finished", "", cancelled.is_set()))

    def _poll(self) -> None:
        while True:
            try:
                kind, iid, detail = self.messages.get_nowait()
            except Empty:
                self.after(75, self._poll)
                return
            if kind == "finished":
                self._finish(detail)
                return
            if kind == "working":
                self.tree.item(iid, values=("Conversion en cours…",))
                if not self.cancelled.is_set():
                    self.status.set(f"Conversion de {detail}…")
                continue
            self.details[iid] = str(detail)
            self.tree.item(iid, values=(detail.name if kind == "done" else detail.splitlines()[0],), tags=(kind,))
            self.progress.configure(value=self.progress["value"] + 1)

    def _finish(self, cancelled: bool) -> None:
        successes = failures = 0
        for iid in self.files:
            tags = self.tree.item(iid, "tags")
            if "done" in tags:
                successes += 1
            elif "error" in tags:
                failures += 1
            else:
                self.tree.item(iid, values=("Annulé",))
        self.cancelled = None
        self.progress.configure(value=successes + failures)
        label = "Lot interrompu" if cancelled else "Lot terminé"
        self.status.set(f"{label} · {successes} fichier(s) créé(s) · {failures} erreur(s)")
        self._update_buttons()

    def request_cancel(self) -> None:
        if self.cancelled is not None:
            self.cancelled.set()
            self.status.set("Annulation en cours…")
            self._update_buttons()


def run() -> None:
    root = tk.Tk()
    root.title("Multitools")
    root.geometry(f"{min(1180,root.winfo_screenwidth()-80)}x{min(900,root.winfo_screenheight()-80)}")
    root.minsize(800, 690)

    style = ttk.Style(root)
    style.theme_use("clam")
    style.configure("TNotebook.Tab", padding=(10, 8))
    style.configure("Treeview", rowheight=24, background="#ffffff", fieldbackground="#ffffff")
    style.configure("Convert.TButton", background="#19665a", foreground="white", padding=(14, 5))
    style.map("Convert.TButton", background=[("disabled", "#d9dfdd"), ("active", "#24786b")],
              foreground=[("disabled", "#495a57")])

    header = tk.Frame(root, bg="#162a35", padx=18, pady=8)
    header.pack(fill="x")
    tk.Label(header, text="Multitools", font=("Segoe UI", 16, "bold"), bg="#162a35", fg="white").pack(anchor="w")
    tk.Label(header, text="Atelier de fichiers hors ligne", font=("Segoe UI", 10),
             bg="#162a35", fg="#c3d8df").pack(anchor="w")
    tabs = ttk.Notebook(root)
    tabs.pack(fill="both", expand=True, padx=12, pady=12)
    services = Services(Path(__file__).resolve().parent.parent / "server_state")
    assistant = AssistantPane(tabs, services)
    library = LibraryPane(tabs, services)
    storage = StoragePane(tabs, services)
    state = StatusPane(tabs, services)

    def analyze_photo(path, facts):
        assistant.attach(path, facts)
        tabs.select(assistant)

    def check_target(path, size):
        guard_target(path, size, services.config.get("storage_roles", {}))

    photos = PhotoArchiviste(tabs, analyze=analyze_photo, preflight=check_target, ollama=services.ollama)
    images = ConversionPanel(tabs, IMAGES, IMAGE_TYPES)
    media = ConversionPanel(tabs, MEDIA, MEDIA_TYPES)
    documentary = DocumentaryPane(tabs, services)
    tabs.add(photos, text="PhotoArchiviste")
    tabs.add(documentary, text="Analyse documentaire")
    tabs.add(images, text="Images")
    tabs.add(media, text="Vidéo et audio")
    tabs.add(library, text="Bibliothèque")
    tabs.add(assistant, text="Assistant IA")
    tabs.add(storage, text="Stockage")
    tabs.add(state, text="État du serveur")

    closing_library = False
    def close() -> None:
        nonlocal closing_library
        panes = (photos, assistant, library, storage, state, documentary)
        if any(pane.busy for pane in panes) or images.busy or media.busy:
            for pane in panes:
                if pane is not library or not closing_library:
                    pane.stop()
            images.request_cancel()
            media.request_cancel()
            root.after(125, close)
        elif services.kiwix.process is not None and not closing_library:
            closing_library = True
            library.stop_server()
            root.after(125, close)
        else:
            documentary.save()
            documentary.pool.shutdown(wait=False, cancel_futures=True)
            services.queue.close()
            root.destroy()

    root.protocol("WM_DELETE_WINDOW", close)
    root.mainloop()
