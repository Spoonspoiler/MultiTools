from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from queue import Empty, SimpleQueue
from threading import Thread
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from ..disk_picker import askdirectory
from tkinter.scrolledtext import ScrolledText

from .inventory import Gate, Photo, TaskStopped
from .journal import Archive, Choice, DEFAULT_CATEGORIES, MOVE_DISABLED, PENDING
from .preview import Preview, SeriesGallery
from .review import Decision, Snapshot, filter_cases, photo_format, propose, propose_selection, snapshot
from .transfers import execute_pending
from .sort_ui import SourceSortPane
from .index_pass import index_with_assistant


STATES = {"planned": "Simulé", "copying": "Copie à reprendre", "ready": "Sortie à publier",
          "verified": "Vérifié · à finaliser", "done": "Copie vérifiée", "cancelled": "Retiré du plan"}
FILTERS = ("Exacts", "Proches", "Toutes", "Reportées")


class PhotoArchiviste(ttk.Frame):
    def __init__(self, parent, *, analyze=None, preflight=None, ollama=None):
        super().__init__(parent, padding=(12, 8))
        self.analyze, self.preflight = analyze, preflight
        self.workspace: Path | None = None
        self.sources: list[Path] = []
        self.data: Snapshot | None = None
        self.gate: Gate | None = None
        self._indexing_pass=False
        self.related_photos={}
        self.messages = SimpleQueue()
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="apercu-photo")
        self.bind("<Destroy>", lambda event: self.executor.shutdown(wait=False, cancel_futures=True)
                  if event.widget == self else None)
        self.shown: tuple[Photo, ...] = ()
        self.visible: tuple[Photo, ...] = ()
        self.case_index = 0
        self.entries = {}
        self.reverse_pending = set()
        self.visited = 0
        self.source_label = tk.StringVar(self, "Aucune source")
        self.workspace_label = tk.StringVar(self, "Aucun dossier de travail")
        self.position = tk.StringVar(self, "")
        self.reason = tk.StringVar(self, "Choisis un dossier de travail et ajoute les sources à comparer.")
        self.activity = tk.StringVar(self, "Prêt")
        self.transfer_detail = tk.StringVar(self, "Sélectionne une action pour voir son détail.")
        self.by_year = tk.BooleanVar(self, False)

        sources = ttk.Frame(self)
        sources.pack(fill="x",pady=(0,6))
        self.workspace_button = ttk.Button(sources,text="Projet…",command=self._select_workspace)
        self.workspace_button.pack(side="left")
        self.source_button = ttk.Button(sources,text="Sources…",command=self._edit_sources)
        self.source_button.pack(side="left",padx=6)
        ttk.Label(sources,textvariable=self.source_label).pack(side="left",padx=6)
        ttk.Button(sources,text="Photos",command=lambda:self.tabs.select(self.dedup)).pack(side="left",padx=6)
        extra=ttk.Menubutton(sources,text="⋯",width=3)
        menu=tk.Menu(extra,tearoff=False)
        menu.add_command(label="Projet, sources et journal…",command=self._show_details)
        menu.add_command(label="Historique des anciennes copies",command=lambda:self.tabs.select(self.plan_tab))
        menu.add_command(label="Anciennes copies À jeter",command=lambda:self.tabs.select(self.quarantine_tab))
        extra.configure(menu=menu)
        extra.pack(side="left")
        self.scan_button = ttk.Button(sources,text="Indexer",command=self._scan)
        self.scan_button.pack(side="right")
        style=ttk.Style(self)
        style.layout('Photo.Workspace.TNotebook.Tab',[])
        self.tabs = ttk.Notebook(self,style="Photo.Workspace.TNotebook")
        self.tabs.pack(fill="both", expand=True)
        self._build_comparison()
        self._build_references()
        self._build_plan()
        self._build_quarantine()
        self._build_activity()
        self.dedup = SourceSortPane(self.tabs, lambda:self.data.photos if self.data else (), lambda:list(self.sources),
                                   self._dedup_changed, external_busy=lambda:self.gate is not None,
                                   on_state=self._refresh_buttons, client=ollama, workspace=lambda:self.workspace,
                                   external_gate=lambda:self.gate, related=lambda photo:self.related_photos.get(photo.path,()))
        self.tabs.add(self.dedup, text="Photos · questions et classement")
        # Les anciens contrôles restent internes pour la compatibilité du journal.
        # Le parcours visible utilise l’assistant et la comparaison réunis.
        self.tabs.hide(0)
        self.tabs.hide(1)
        self.tabs.select(self.dedup)
        self._refresh_buttons()

    def _dedup_changed(self):
        self.data = None
        self._show_case()
        self.reason.set("Sources modifiées par la quarantaine : réindexe avant de reprendre les comparaisons.")

    @property
    def busy(self) -> bool:
        return self.gate is not None or (getattr(self, 'dedup', None) is not None and self.dedup.gate is not None)

    def _build_comparison(self) -> None:
        panel = ttk.Frame(self.tabs, padding=8)
        self.tabs.add(panel, text="Comparer")
        panel.columnconfigure(0, weight=1)
        panel.rowconfigure(3, weight=1)
        navigation = ttk.Frame(panel)
        navigation.grid(row=0, column=0, sticky="ew")
        self.filter = ttk.Combobox(navigation, values=FILTERS, state="readonly", width=12)
        self.filter.set("Proches")
        self.filter.pack(side="left")
        self.filter.bind("<<ComboboxSelected>>", lambda _: self._change_filter())
        self.format_filter = ttk.Combobox(navigation, values=("Tous les formats",), state="readonly", width=18)
        self.format_filter.set("Tous les formats")
        self.format_filter.pack(side="left", padx=6)
        self.format_filter.bind("<<ComboboxSelected>>", lambda _: self._change_filter())
        ttk.Button(navigation, text="Précédent", command=lambda: self._navigate(-1)).pack(side="left", padx=(8, 4))
        ttk.Button(navigation, text="Suivant", command=lambda: self._navigate(1)).pack(side="left")
        ttk.Label(navigation, textvariable=self.position).pack(side="left", padx=8)
        ttk.Label(panel, textvariable=self.reason).grid(row=1, column=0, sticky="ew", pady=(5, 3))
        self.gallery = SeriesGallery(panel, self.executor, self._show_page, self._preview_tile, self._refresh_buttons)
        self.gallery.grid(row=2, column=0, sticky="ew")
        self.gallery.grid_remove()

        panes = ttk.Frame(panel)
        panes.grid(row=3, column=0, sticky="nsew")
        panes.columnconfigure((0, 1), weight=1, uniform="photo")
        panes.rowconfigure(2, weight=1)
        self.variants = []
        self.previews = []
        self.categories = []
        self.analyze_buttons = []
        for column, title in enumerate(("Gauche", "Droite")):
            ttk.Label(panes, text=title, font=("TkDefaultFont", 10, "bold")).grid(row=0, column=column, sticky="w", padx=6)
            variants = ttk.Combobox(panes, width=20, state="disabled")
            variants.grid(row=1, column=column, sticky="ew", padx=6)
            variants.bind("<<ComboboxSelected>>", lambda _, side=column: self._variant_changed(side))
            self.variants.append(variants)
            preview = Preview(panes, self.executor)
            preview.grid(row=2, column=column, sticky="nsew")
            self.previews.append(preview)
            category_row = ttk.Frame(panes, padding=(6, 0))
            category_row.grid(row=3, column=column, sticky="ew")
            ttk.Label(category_row, text="Catégorie").pack(side="left", padx=(0, 6))
            category = ttk.Combobox(category_row, values=DEFAULT_CATEGORIES, width=17)
            category.set(DEFAULT_CATEGORIES[0])
            category.pack(side="left", fill="x", expand=True)
            self.categories.append(category)
            if self.analyze is not None:
                button = ttk.Button(category_row, text="Examiner avec l’IA", command=lambda side=column: self._analyze(side))
                button.pack(side="left", padx=(5, 0))
                self.analyze_buttons.append(button)

        options = ttk.Frame(panel)
        options.grid(row=4, column=0, sticky="ew", pady=(8, 4))
        ttk.Checkbutton(options, text="Sous-dossier par année EXIF", variable=self.by_year).pack(side="left", padx=10)
        self.batch_button = ttk.Button(options, text="Préparer les copies de la vue Toutes", command=self._batch)
        self.batch_button.pack(side="right")
        choices = ttk.Frame(panel)
        choices.grid(row=5, column=0, sticky="ew")
        self.decision_buttons = {}
        for decision in Decision:
            button = ttk.Button(choices, text=decision.value, command=lambda item=decision: self._decision(item))
            button.pack(side="left", padx=(0, 4))
            self.decision_buttons[decision] = button
        self.defer_button = ttk.Button(choices, text="Décider plus tard", command=self._defer)
        self.defer_button.pack(side="left")
        self.selection_row = ttk.Frame(panel)
        self.selection_row.grid(row=6, column=0, sticky="ew", pady=(6, 0))
        self.selection_row.grid_remove()
        self.selection_text = tk.StringVar(self)
        self.keep_selection = ttk.Button(self.selection_row, text="Garder la sélection", style="Convert.TButton",
                                         command=self._keep_selection)
        self.keep_selection.pack(side="right")
        self.reject_selection = ttk.Button(self.selection_row, text="Préparer les copies À jeter", command=self._reject_selection)
        self.reject_selection.pack(side="right", padx=6)
        ttk.Label(self.selection_row, textvariable=self.selection_text).pack(side="left")

    def _build_references(self):
        panel = ttk.Frame(self.tabs, padding=8)
        self.tabs.add(panel, text="Fichiers du groupe")
        ttk.Label(panel, text="Tous les fichiers du groupe affiché dans Comparer, y compris les autres formats.").pack(anchor="w")
        self.reference_tree = self._tree(panel, (("format", "Format", 75), ("resolution", "Résolution", 110),
                                               ("size", "Taille", 105), ("path", "Chemin complet", 550)))
        self.reference_tree.bind("<Double-1>", self._preview_reference)
        ttk.Label(panel, text="Double-clique sur une ligne pour comparer cette photo. Aucun fichier n’est modifié.").pack(anchor="w")

    def _preview_reference(self, _event=None):
        selected = self.reference_tree.selection()
        if not selected or self.busy:
            return
        index = int(selected[0])
        page, slot = divmod(index, self.gallery.PAGE_SIZE)
        self.gallery.change_page(page - self.gallery.page)
        self._preview_tile(slot)
        self.tabs.select(0)

    def _build_plan(self) -> None:
        self.plan_tab = ttk.Frame(self.tabs, padding=8)
        self.tabs.add(self.plan_tab, text="Plan et historique")
        controls = ttk.Frame(self.plan_tab)
        controls.pack(fill="x", pady=(0, 6))
        self.apply_button = ttk.Button(controls, text="Appliquer le plan", style="Convert.TButton", command=self._apply)
        self.apply_button.pack(side="left")
        self.drop_button = ttk.Button(controls, text="Retirer du plan", command=self._drop)
        self.drop_button.pack(side="left", padx=6)
        self.undo_button = ttk.Button(controls, text="Restaurer par copie…", command=lambda: self._restore(self.plan_tree))
        self.undo_button.pack(side="left")
        self.plan_tree = self._tree(self.plan_tab, (("state", "État", 145), ("source", "Source", 315), ("target", "Destination", 315)))
        self.plan_tree.bind("<<TreeviewSelect>>", self._plan_details)
        ttk.Label(self.plan_tab, textvariable=self.transfer_detail, wraplength=800, justify="left").pack(fill="x", pady=6)

    def _build_quarantine(self) -> None:
        panel = ttk.Frame(self.tabs, padding=8)
        self.quarantine_tab=panel
        self.tabs.add(panel, text="À jeter · copies")
        self.restore_button = ttk.Button(panel, text="Restaurer par copie…", command=lambda: self._restore(self.quarantine_tree))
        self.restore_button.pack(anchor="w", pady=(0, 6))
        split = ttk.PanedWindow(panel, orient="horizontal")
        split.pack(fill="both", expand=True)
        listing = ttk.Frame(split)
        self.quarantine_tree = self._tree(listing, (("file", "Fichier", 210), ("decision", "Décision d’origine", 160)))
        self.quarantine_tree.bind("<<TreeviewSelect>>", self._quarantine_details)
        self.quarantine_preview = Preview(split, self.executor)
        split.add(listing, weight=1)
        split.add(self.quarantine_preview, weight=2)

    @staticmethod
    def _tree(parent, headings):
        container = ttk.Frame(parent)
        container.pack(fill="both", expand=True)
        container.columnconfigure(0, weight=1)
        container.rowconfigure(0, weight=1)
        tree = ttk.Treeview(container, columns=[key for key, _, _ in headings], show="headings", height=6)
        tree.grid(row=0, column=0, sticky="nsew")
        for key, name, width in headings:
            tree.heading(key, text=name, anchor="w")
            tree.column(key, width=width, minwidth=70)
        vertical = ttk.Scrollbar(container, command=tree.yview)
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal = ttk.Scrollbar(container, orient="horizontal", command=tree.xview)
        horizontal.grid(row=1, column=0, sticky="ew")
        tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        return tree

    def _build_activity(self) -> None:
        activity = ttk.Frame(self)
        self.activity_frame=activity
        controls = ttk.Frame(activity)
        controls.pack(fill="x", pady=(3, 3))
        self.progress = ttk.Progressbar(controls)
        self.progress.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self.pause_button = ttk.Button(controls, text="Pause", command=self._pause)
        self.stop_button = ttk.Button(controls, text="Annuler la tâche", command=self.stop)
        self.log = ScrolledText(activity, height=3, wrap="word", state="disabled", font="TkDefaultFont")

    def _show_details(self):
        window=tk.Toplevel(self)
        window.title('Projet et journal')
        window.geometry('760x420')
        text=ScrolledText(window,wrap='word',padx=10,pady=10)
        text.pack(fill='both',expand=True)
        content='Projet : '+str(self.workspace or 'non choisi')+'\n\nSources :\n'
        content+='\n'.join(str(path) for path in self.sources)+'\n\n'+self.activity.get()+'\n\n'
        content+=self.log.get('1.0','end')
        text.insert('1.0',content)
        text.configure(state='disabled')
        ttk.Button(window,text='Fermer',command=window.destroy).pack(pady=8)

    def _select_workspace(self) -> None:
        selected = askdirectory(parent=self, title="Dossier du journal et des photos triées", mustexist=True)
        if selected:
            self._launch("Ouverture du journal", lambda *_: "Journal ouvert", Path(selected).absolute())

    def _edit_sources(self) -> None:
        dialog = tk.Toplevel(self)
        dialog.title("Dossiers sources")
        dialog.transient(self.winfo_toplevel())
        dialog.geometry("620x290")
        paths = list(self.sources)
        listing = tk.Listbox(dialog, selectmode="extended")
        listing.pack(fill="both", expand=True, padx=10, pady=10)

        def refresh():
            listing.delete(0, "end")
            if paths:
                listing.insert("end", *(str(path) for path in paths))

        def add():
            selected = askdirectory(parent=dialog, title="Ajouter un dossier source", mustexist=True)
            if selected:
                path = Path(selected).absolute()
                if path not in paths:
                    paths.append(path)
                refresh()

        def remove():
            for index in reversed(listing.curselection()):
                del paths[index]
            refresh()

        def save():
            self.sources = paths
            self._update_sources()
            dialog.destroy()

        buttons = ttk.Frame(dialog)
        buttons.pack(fill="x", padx=10, pady=(0, 10))
        ttk.Button(buttons, text="Ajouter…", command=add).pack(side="left")
        ttk.Button(buttons, text="Retirer", command=remove).pack(side="left", padx=6)
        ttk.Button(buttons, text="Valider", command=save).pack(side="right")
        refresh()
        dialog.grab_set()

    def _update_sources(self) -> None:
        count = len(self.sources)
        self.source_label.set(f"{count} dossier(s) source(s)" if count else "Aucune source")
        if self.data and self.data.scan_incomplete:
            self.scan_button.configure(text="Reprendre l’indexation")
        else:
            self.scan_button.configure(text="Indexer")
        self._refresh_buttons()

    def _scan(self) -> None:
        if self.busy or self.workspace is None or not self.sources:
            return
        roots = list(self.sources)
        model,preferences=self.dedup.model.get(),self.dedup.preferences.get()
        self._indexing_pass=True
        self.dedup.index_assistant.begin_pass()
        self.tabs.select(self.dedup)
        self.dedup.work_tabs.select(self.dedup.index_assistant)
        def work(archive, gate, report):
            if self.preflight is not None:
                for root in roots:
                    gate.check()
                    self.preflight(root, 0)
            result=index_with_assistant(archive,roots,self.dedup.client,model,preferences,gate,report)
            return (f'{result["indexed"]} photo(s) indexée(s), {result["analysed"]} analyse(s) IA, '
                    f'{result["reused"]} réutilisée(s), {result["ai_errors"]} erreur(s) IA')
        self._launch("Indexation + assistant IA", work)

    def _change_filter(self) -> None:
        self.case_index = 0
        self._show_case()

    def _cases(self):
        return filter_cases(self.data.cases[self.filter.get()], self.format_filter.get()) if self.data else []

    def _navigate(self, offset: int) -> None:
        self.case_index += offset
        self._show_case()

    def _show_case(self) -> None:
        cases = self._cases()
        self.case_index = min(max(self.case_index, 0), max(len(cases) - 1, 0))
        case = cases[self.case_index] if cases else None
        self.shown = case.photos if case else ()
        self.reference_tree.delete(*self.reference_tree.get_children())
        for index, photo in enumerate(self.shown):
            self.reference_tree.insert("", "end", iid=str(index), values=(photo_format(photo),
                f"{photo.width} × {photo.height}", f"{photo.size / 1024**2:.2f} Mio", str(photo.path)))
        self.position.set(f"{self.case_index + 1} / {len(cases)}" if cases else "0 / 0")
        if self.data:
            self.reason.set((f"{case.reason} · {len(self.shown)} fichier(s) · groupe complet, tous formats"
                             if case else "Aucune photo dans cette vue."))
        for widget in (self.gallery, self.selection_row):
            if len(self.shown) > 1:
                widget.grid()
            else:
                widget.grid_remove()
        self.gallery.set_series(self.shown)
        self._refresh_buttons()

    def _show_page(self, photos: tuple[Photo, ...], start: int) -> None:
        self.visible = photos
        values = [f"{index + 1} · {photo.path.name} · {photo.path.parent.name}"
                  for index, photo in enumerate(photos, start)]
        for index, box in enumerate(self.variants):
            box.configure(values=values, state="readonly" if len(values) > 1 else "disabled")
            box.set(values[index] if len(values) > index else "")
        self._show_variants()

    def _preview_tile(self, slot: int) -> None:
        self.variants[0].current(slot)
        self._variant_changed(0)

    def _variant_changed(self, side: int) -> None:
        mine, other = self.variants[side], self.variants[1 - side]
        if len(self.visible) > 1 and mine.current() == other.current():
            other.current(0 if mine.current() != 0 else 1)
        self._show_variants()

    def _selected_photos(self) -> tuple[Photo, ...]:
        return tuple(self.visible[index] for box in self.variants if (index := box.current()) >= 0)

    def _show_variants(self) -> None:
        photos = self._selected_photos()
        self.gallery.set_focus(photos[0] if photos else None)
        for index, preview in enumerate(self.previews):
            preview.set_photo(photos[index] if len(photos) > index else None)
        self._refresh_buttons()

    def _decision(self, decision: Decision) -> None:
        photos = self._selected_photos()
        if not photos:
            return
        left, right = photos[0], photos[1] if len(photos) > 1 else None
        lc, rc = (box.get() for box in self.categories)
        year, reason = self.by_year.get(), self.reason.get()
        def work(archive, gate, report):
            entries = propose(archive, decision, left, right, lc, rc, reason, move=False, by_year=year)
            for item in entries:
                report("done", f"Simulé · {item.source.name} → {item.target}")
            return f"{len(entries)} action(s) ajoutée(s) au plan"
        self._launch("Simulation", work)

    def _keep_selection(self) -> None:
        photos = self.gallery.selection()
        if self.busy or not photos:
            return
        category, year, reason = self.categories[0].get(), self.by_year.get(), self.reason.get()

        def work(archive, gate, _report):
            entries = propose_selection(archive, photos, category, reason, move=False, by_year=year, gate=gate)
            return f"{len(entries)} photo(s) ajoutée(s) au plan"

        self._launch("Simulation de la sélection", work)

    def _reject_selection(self):
        photos = self.gallery.selection()
        if self.busy or not photos:
            return
        if not messagebox.askyesno("Préparer À jeter", f"Préparer {len(photos)} copie(s) dans À jeter ?\n"
                                  "Les originaux resteront dans leur dossier. Relis le plan avant application.", parent=self):
            return
        def work(archive, gate, _report):
            entries = archive.plan([Choice(photo, None) for photo in photos], "Sélection À jeter",
                                   "Sélection manuelle ; originaux conservés", move=False, by_year=False, gate=gate)
            return f"{len(entries)} copie(s) À jeter préparée(s), aucun original retiré."
        self._launch("Préparation de la sélection À jeter", work)
        self.tabs.select(self.plan_tab)

    def _defer(self) -> None:
        photos = self._selected_photos()
        undo = self.filter.get() == "Reportées"
        def work(archive, gate, report):
            archive.defer(photos, not undo)
            return "Remis à examiner" if undo else "Décision reportée"
        self._launch("Mise à jour des décisions", work)

    def _analyze(self, side: int) -> None:
        photos = self._selected_photos()
        if side < len(photos):
            photo = photos[side]
            facts = (f"Nom : {photo.path.name}\nDimensions : {photo.width} × {photo.height}\n"
                     f"Date EXIF : {photo.taken or 'absente'}\nComparaison : {self.reason.get()}")
            self.analyze(photo.path, facts)

    def _batch(self) -> None:
        if self.busy or self.filter.get() != "Toutes":
            return
        photos = [case.photos[0] for case in self._cases()]
        category, year = self.categories[0].get(), self.by_year.get()
        if not photos:
            return
        def work(archive, gate, _report):
            choices = [Choice(photo, category) for photo in photos]
            entries = archive.plan(choices, "Classer le lot", "Copies de la vue Toutes", move=False, by_year=year, gate=gate)
            return f"{len(entries)} copie(s) simulée(s). Relis le plan avant application."
        self._launch("Préparation du lot…", work)
        self.tabs.select(self.plan_tab)

    def _apply(self) -> None:
        def work(archive, gate, report):
            done, errors = execute_pending(archive, gate, report, self.preflight)
            return f"{done} transfert(s) vérifié(s) · {errors} erreur(s)"
        self._launch("Exécution du plan", work)

    def _restore(self, tree) -> None:
        selected = [int(iid) for iid in tree.selection()]
        def work(archive, gate, report):
            count = archive.plan_restore(selected, copy=True)
            return f"{count} restauration(s) par copie au plan. Les fichiers classés ou rejetés restent en place."
        self._launch("Préparation de la restauration par copie", work)
        self.tabs.select(self.plan_tab)

    def _drop(self) -> None:
        selected = [int(iid) for iid in self.plan_tree.selection()]
        self._launch("Mise à jour du plan", lambda archive, *_:
                     f"{archive.cancel_plans(selected)} action(s) retirée(s). Les fichiers existants restent en place.")

    def _plan_details(self, _event=None) -> None:
        selected = self.plan_tree.selection()
        item = self.entries.get(int(selected[0])) if selected else None
        if item:
            action = "Déplacer" if item.mode == "move" else "Copier"
            details = f"{item.decision} · {action} · {item.created} UTC\n{item.source} → {item.target}"
            if item.finished:
                details += f"\nTerminé le {item.finished} UTC"
            if item.state in PENDING and item.blocked:
                details += f"\n{item.blocked}"
            elif item.state == "done" and item.blocked:
                details += "\nHistorique antérieur · restauration automatique indisponible sans preuves d’identité."
            for note in (item.detail, item.error):
                if note:
                    details += "\n" + note
            self.transfer_detail.set(details)
        else:
            self.transfer_detail.set("Sélectionne une action pour voir son détail.")
        self._refresh_buttons()

    def _quarantine_details(self, _event=None) -> None:
        selected = self.quarantine_tree.selection()
        item = self.entries.get(int(selected[0])) if selected else None
        photo = Photo(item.target, item.size, 0, item.sha256) if item else None
        self.quarantine_preview.set_photo(photo)
        self._refresh_buttons()

    def _refresh_lists(self) -> None:
        self.entries = {item.id: item for item in self.data.transfers}
        self.reverse_pending = {item.restore_of for item in self.entries.values() if item.restore_of and item.state != "cancelled"}
        restored = {item.restore_of: item.mode for item in self.entries.values()
                    if item.restore_of and item.state == "done"}
        for tree in (self.plan_tree, self.quarantine_tree):
            tree.delete(*tree.get_children())
        for item in self.entries.values():
            state = STATES[item.state]
            if item.state == "done" and item.blocked:
                state = "Terminé · historique"
            if item.id in restored:
                state = "Restauré par copie" if restored[item.id] == "copy" else "Restauré"
            if item.state in PENDING and item.blocked:
                state = "Bloqué · plan ancien"
            elif item.error:
                state = f"Erreur · {state}"
            self.plan_tree.insert("", "end", iid=str(item.id), values=(state, str(item.source), str(item.target)))
            if item.kind == "reject" and item.state == "done" and restored.get(item.id) != "move":
                self.quarantine_tree.insert("", "end", iid=str(item.id), values=(item.target.name, item.decision))
        self._plan_details()
        self._quarantine_details()

    def _refresh_buttons(self) -> None:
        if self.gate is not None:
            self.activity_frame.pack(fill="x",pady=(3,0))
        else:
            self.activity_frame.pack_forget()
        idle = not self.busy
        current = self._selected_photos()
        pair = len(current) == 2
        any_photo = bool(current)
        count = len(self.gallery.checked) or int(self.gallery.focused is not None)
        selection = f"{count} photo(s) cochée(s)" if self.gallery.checked else "Photo agrandie à gauche"
        self.selection_text.set(f"{selection} · Catégorie de gauche")
        for side, button in enumerate(self.analyze_buttons):
            button.configure(state="normal" if idle and side < len(current) else "disabled")
        self.decision_buttons[Decision.KEEP_LEFT].configure(text=Decision.KEEP_LEFT.value if pair else "Classer")
        self.decision_buttons[Decision.REJECT_BOTH].configure(text="Copier les deux dans À jeter" if pair else "Copier dans À jeter")
        self.defer_button.configure(text="Réexaminer" if self.filter.get() == "Reportées" else "Décider plus tard")
        planned = [self.entries[int(iid)] for iid in self.plan_tree.selection() if int(iid) in self.entries]
        rejected = [self.entries[int(iid)] for iid in self.quarantine_tree.selection() if int(iid) in self.entries]
        reverse_pending = self.reverse_pending
        for button, enabled in (
            (self.workspace_button, idle), (self.source_button, idle), (self.scan_button, idle and self.workspace and self.sources),
            (self.decision_buttons[Decision.KEEP_LEFT], idle and any_photo),
            (self.decision_buttons[Decision.REJECT_BOTH], idle and any_photo),
            (self.decision_buttons[Decision.KEEP_RIGHT], idle and pair), (self.decision_buttons[Decision.KEEP_BOTH], idle and pair),
            (self.keep_selection, idle and count > 0 and len(self.shown) > 1),
            (self.reject_selection, idle and count > 0 and len(self.shown) > 1),
            (self.defer_button, idle and any_photo),
            (self.batch_button, idle and self.filter.get() == "Toutes" and bool(self._cases())),
            (self.apply_button, idle and any(item.state in PENDING and not item.blocked for item in self.entries.values())),
            (self.drop_button, idle and any(item.state in PENDING for item in planned)),
            (self.undo_button, idle and any(item.kind != "restore" and item.state == "done" and not item.blocked
                                           and item.id not in reverse_pending for item in planned)),
            (self.restore_button, idle and any(not item.blocked and item.id not in reverse_pending for item in rejected)),
            (self.pause_button, self.gate is not None and not self.gate.stopped.is_set()),
            (self.stop_button, self.gate is not None and not self.gate.stopped.is_set()),
        ):
            button.configure(state="normal" if enabled else "disabled")

    def _launch(self, title, work, workspace=None) -> None:
        workspace = workspace or self.workspace
        if self.busy or workspace is None:
            return
        change_workspace = workspace != self.workspace or self.data is None
        gate = self.gate = Gate()
        self.pause_button.configure(text="Pause")
        self.progress.configure(mode="indeterminate", value=0)
        self.progress.start(50)
        self.activity.set(title)
        self.visited = 0
        self._log(title)
        self._refresh_buttons()

        def emit(kind, value):
            self.messages.put((kind, value))

        def run():
            data = None
            try:
                if self.preflight is not None:
                    self.preflight(workspace, 0)
                with Archive(workspace) as archive:
                    try:
                        result = work(archive, gate, emit)
                    except Exception as error:
                        if not isinstance(error, TaskStopped):
                            emit("error", str(error))
                        result = "Tâche interrompue. Le journal permet de reprendre." if isinstance(error, TaskStopped) else "Tâche en erreur"
                    try:
                        data = snapshot(archive, gate)
                    except TaskStopped:
                        data = snapshot(archive, Gate(), include_visual=False)
                        result = "Tâche interrompue. Le journal permet de reprendre."
            except Exception as error:
                emit("error", str(error))
                result = "Journal indisponible"
            emit("finished", (workspace, change_workspace, data, result))

        Thread(target=run, daemon=True, name="photoarchiviste").start()
        self.after(65, self._poll)

    def _poll(self) -> None:
        for _ in range(160):
            try:
                kind, value = self.messages.get_nowait()
            except Empty:
                break
            if kind == "finished":
                self._finish_task(*value)
                return
            if kind == 'indexed_photo':
                self.dedup.index_assistant.preview.set_photo(value)
                self.dedup.index_assistant.counter.set('Passage en cours : '+value.path.name)
                continue
            if kind == 'assistant_progress':
                self.dedup.index_assistant.status.set(value)
                self.activity.set(value)
                continue
            if kind == "file":
                self.visited += 1
                if self.gate.running.is_set() and not self.gate.stopped.is_set():
                    self.activity.set(f"{self.visited} · {value}")
            elif kind == "progress":
                done, total = value
                if str(self.progress["mode"]) != "determinate":
                    self.progress.stop()
                self.progress.configure(mode="determinate", value=done, maximum=total)
            else:
                self._log(("Erreur · " if kind == "error" else "") + value)
        self.after(65, self._poll)

    def _finish_task(self, workspace: Path, change_workspace: bool, data: Snapshot | None, result: str) -> None:
        self.gate = None
        was_indexing=self._indexing_pass
        self._indexing_pass=False
        self.dedup.index_assistant.running=False
        self.progress.stop()
        self.activity.set(result)
        self._log(result)
        if data:
            self.workspace, self.data = workspace, data
            self.workspace_label.set(str(workspace))
            self.workspace_button.configure(text="Projet : "+workspace.name[:24])
            if change_workspace:
                self.sources = data.roots
                self.case_index = 0
            for box in self.categories:
                box.configure(values=data.categories)
            formats = ("Tous les formats", *sorted({photo_format(photo) for photo in data.photos}))
            self.format_filter.configure(values=formats)
            if self.format_filter.get() not in formats:
                self.format_filter.set("Tous les formats")
            self._refresh_lists()
            self._show_case()
            self.related_photos={photo.path:case.photos for mode in ('Exacts','Proches') for case in data.cases[mode] for photo in case.photos}
            self.dedup.load()
            if was_indexing:
                self.dedup.index_assistant.status.set(result+' · questions et résultats enregistrés')
        self._update_sources()

    def _log(self, message: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", message + "\n")
        if int(self.log.index("end-1c").split(".")[0]) > 300:
            self.log.delete("1.0", "3.0")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _pause(self) -> None:
        if self.gate.running.is_set():
            self.gate.running.clear()
            self.progress.stop()
            self.pause_button.configure(text="Reprendre")
            self.activity.set("Pause demandée · reprise au prochain point sûr")
        else:
            self.gate.running.set()
            if str(self.progress["mode"]) == "indeterminate":
                self.progress.start(50)
            self.pause_button.configure(text="Pause")
            self.activity.set("Reprise en cours…")

    def stop(self) -> None:
        if getattr(self, 'dedup', None) is not None:
            self.dedup.stop()
        if self.gate is not None and not self.gate.stopped.is_set():
            self.gate.stopped.set()
            self.gate.running.set()
            self.activity.set("Annulation en cours…")
            self._refresh_buttons()
