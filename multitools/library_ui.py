import tkinter as tk
from tkinter import filedialog, ttk
from .disk_picker import askdirectory
from pathlib import Path
import webbrowser

from .kiwix import inventory
from .server_tasks import WorkerPane


class LibraryPane(WorkerPane):
    def __init__(self, parent, services):
        super().__init__(parent, services)
        self.archives = []
        self.hits = []
        self.host = tk.StringVar(self, "127.0.0.1")
        self.port = tk.StringVar(self, "8080")
        self.url = tk.StringVar(self)
        self.query = tk.StringVar(self)

        local = ttk.Frame(self)
        local.pack(fill="x", pady=(0, 6))
        for label, command in (("Ajouter un dossier…", self.add_root), ("Ajouter des ZIM…", self.add_files),
                               ("Actualiser", self.refresh), ("Vider la sélection", self.clear)):
            ttk.Button(local, text=label, command=command).pack(side="left", padx=(0, 6))
        self.files = ttk.Treeview(self, columns=("title", "lang", "size", "date", "path"), show="headings", height=7)
        for key, title, width in (("title", "Archive", 175), ("lang", "Langue", 65), ("size", "Taille", 90),
                                  ("date", "Date", 95), ("path", "Chemin", 310)):
            self.files.heading(key, text=title)
            self.files.column(key, width=width)
        self.files.pack(fill="both", expand=True)

        service = ttk.LabelFrame(self, text="Serveur Kiwix", padding=8)
        service.pack(fill="x", pady=8)
        ttk.Label(service, text="Adresse locale ou IP Wi-Fi privée").grid(row=0, column=0, sticky="w")
        ttk.Entry(service, textvariable=self.host, width=16).grid(row=0, column=1, sticky="w", padx=5)
        ttk.Entry(service, textvariable=self.port, width=6).grid(row=0, column=2, sticky="w", padx=5)
        ttk.Button(service, text="Démarrer", command=lambda: self.connect(start=True)).grid(row=0, column=3, padx=4)
        ttk.Button(service, text="Connecter", command=self.connect).grid(row=0, column=4, padx=4)
        ttk.Button(service, text="Arrêter", command=self.stop_server).grid(row=0, column=5, padx=4)
        ttk.Button(service, text="Ouvrir dans le navigateur", command=self.open_library).grid(row=1, column=0, sticky="w", pady=(6, 0))
        ttk.Entry(service, textvariable=self.url, state="readonly").grid(row=1, column=1, columnspan=5, sticky="ew", pady=(6, 0))

        search = ttk.Frame(self)
        search.pack(fill="x", pady=(0, 5))
        self.book = ttk.Combobox(search, state="readonly", width=25)
        self.book.pack(side="left", padx=(0, 6))
        self.book.bind("<<ComboboxSelected>>", lambda _: self.select_book())
        query = ttk.Entry(search, textvariable=self.query)
        query.pack(side="left", fill="x", expand=True)
        query.bind("<Return>", lambda _: self.search())
        ttk.Button(search, text="Rechercher", command=self.search).pack(side="left", padx=6)
        ttk.Button(search, text="Ouvrir la page", command=self.open_hit).pack(side="left")
        self.results = tk.Listbox(self, height=5)
        self.results.pack(fill="both", expand=True)
        self.results.bind("<Double-1>", lambda _: self.open_hit())
        self.results.bind("<<ListboxSelect>>", lambda _: self.excerpt())
        self.summary = tk.StringVar(self)
        ttk.Label(self, textvariable=self.summary, wraplength=790, justify="left").pack(fill="x", pady=4)
        ttk.Button(self, text="Annuler", command=self.stop).pack(side="right")
        ttk.Label(self, textvariable=self.status, wraplength=740).pack(side="left", fill="x", expand=True)

    def _save_paths(self, key, new):
        if self.busy or not new:
            return
        paths = [str(path) for path in self.services.paths(key)]
        try:
            self.services.save(**{key: list(dict.fromkeys(paths + list(new)))})
            self.refresh()
        except OSError as error:
            self.status.set(str(error))

    def add_root(self):
        if not self.busy and (folder := askdirectory(parent=self, mustexist=True)):
            self._save_paths("zim_roots", [folder])

    def add_files(self):
        if not self.busy:
            self._save_paths("zim_files", filedialog.askopenfilenames(parent=self, filetypes=[("Archives ZIM", "*.zim *.zimaa")]))

    def clear(self):
        if self.busy:
            return
        try:
            self.services.save(zim_roots=[], zim_files=[])
            self.show_archives([])
        except OSError as error:
            self.status.set(str(error))

    def refresh(self):
        roots, files = self.services.paths("zim_roots"), self.services.paths("zim_files")
        self.launch("Inventaire des ZIM…", lambda gate, report: inventory(roots, files, gate.stopped, report), self.show_archives)

    def show_archives(self, entries):
        self.archives = entries
        self.files.delete(*self.files.get_children())
        books = {book.name: book for book in self.services.kiwix.books}
        for item in entries:
            book = books.get(item.path.stem)
            self.files.insert("", "end", values=(book.title if book else item.path.name,
                              book.language if book else "", f"{item.size / 1024**3:.2f} Gio",
                              (book.date or item.date) if book else item.date, str(item.path)))
        self.status.set(f"{len(entries)} archive(s) ZIM repérée(s)")

    def connect(self, *, start=False):
        host, port = self.host.get(), self.port.get()
        roots, files = self.services.paths("zim_roots"), self.services.paths("zim_files")
        def work(gate, report):
            if not start:
                return self.services.kiwix.connect(host, port, gate.stopped), self.archives
            entries = inventory(roots, files, gate.stopped, report)
            return self.services.kiwix.start(host, port, entries, gate.stopped), entries
        self.launch("Connexion à Kiwix…", work, self.connected)

    def connected(self, result):
        books, entries = result
        self.show_results([])
        self.book["values"] = [f"{book.title} · {book.language or book.name}" for book in books]
        if books:
            self.book.current(0)
        else:
            self.book.set("")
        self.select_book()
        self.url.set(self.services.kiwix.api.url)
        self.show_archives(entries)
        version = self.services.kiwix.version or "Kiwix"
        self.status.set(f"{version} connecté · {len(books)} titre(s) servable(s)")

    def select_book(self):
        index = self.book.current()
        books = self.services.kiwix.books
        self.services.book = books[index] if 0 <= index < len(books) else None

    def stop_server(self):
        def finished(_):
            self.book.set("")
            self.book["values"] = ()
            self.select_book()
            self.url.set("")
            self.show_results([])
            self.status.set("Kiwix déconnecté")
        self.launch("Arrêt ou déconnexion…", lambda *_: self.services.kiwix.stop(), finished)

    def open_library(self):
        if self.services.kiwix.api:
            webbrowser.open(self.services.kiwix.api.url)
        else:
            self.status.set("Démarre ou connecte Kiwix pour ouvrir la bibliothèque.")

    def search(self):
        api, book, query = self.services.kiwix.api, self.services.book, self.query.get().strip()
        if api is None or book is None or not query:
            self.status.set("Connecte Kiwix et choisis une archive et une recherche.")
            return
        self.launch("Recherche Kiwix…", lambda gate, _: api.search(query, book, cancel=gate.stopped), self.show_results)

    def show_results(self, hits):
        self.hits = hits
        self.results.delete(0, "end")
        if hits:
            self.results.insert("end", *(hit.title for hit in hits))
        self.summary.set("")
        self.status.set(f"{len(hits)} résultat(s)")

    def excerpt(self):
        if selection := self.results.curselection():
            self.summary.set(self.hits[selection[0]].excerpt)

    def open_hit(self):
        if selection := self.results.curselection():
            webbrowser.open(self.hits[selection[0]].url)
