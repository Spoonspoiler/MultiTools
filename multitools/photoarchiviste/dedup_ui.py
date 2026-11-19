"""Analyse explicite des sources et validation des doublons exacts."""
from pathlib import Path
from queue import Empty, SimpleQueue
from threading import Thread
import tkinter as tk
from tkinter import ttk, messagebox

from ..disk_picker import askopenfilename
from .exact_duplicates import audit, quarantine, restore, QUARANTINE
from .inventory import Gate, TaskStopped


class DedupPane(ttk.Frame):
    def __init__(self, parent, sources, workspace, changed, external_busy=lambda:False, on_state=lambda:None):
        super().__init__(parent, padding=8)
        self.sources, self.workspace, self.changed = sources, workspace, changed
        self.external_busy, self.on_state = external_busy, on_state
        self.gate = None
        self.plan = []
        self.manifests = {}
        self.messages = SimpleQueue()
        self.status = tk.StringVar(self, 'Choisis les sources puis lance l’analyse. Aucun fichier n’est supprimé définitivement.')
        ttk.Label(self, text='Photos et documents : seuls les contenus strictement identiques sont proposés.\n'
                  'Un exemplaire est conservé par contenu ; les fichiers différents restent en place.', wraplength=850).pack(fill='x')
        bar = ttk.Frame(self)
        bar.pack(fill='x', pady=8)
        self.scan_button = ttk.Button(bar, text='Analyser les doublons des sources', command=self.scan)
        self.scan_button.pack(side='left')
        self.select_button = ttk.Button(bar, text='Tout sélectionner', command=self.select_all)
        self.select_button.pack(side='left', padx=6)
        self.apply_button = ttk.Button(bar, text='Mettre la sélection en quarantaine…', command=self.apply)
        self.apply_button.pack(side='left')
        self.restore_button = ttk.Button(bar, text='Restaurer un fichier…', command=self.restore_file)
        self.restore_button.pack(side='left', padx=6)
        ttk.Button(bar, text='Annuler la tâche', command=self.stop).pack(side='left')
        area = ttk.Frame(self)
        area.pack(fill='both', expand=True)
        area.columnconfigure(0, weight=1)
        area.rowconfigure(0, weight=1)
        self.tree = ttk.Treeview(area, columns=('state','keep','extra'), show='headings', selectmode='extended')
        self.tree.grid(row=0,column=0,sticky='nsew')
        for key, title, width in [('state','État',100),('keep','Exemplaire conservé',350),('extra','Doublon à retirer de la source',350)]:
            self.tree.heading(key,text=title)
            self.tree.column(key,width=width)
        vertical = ttk.Scrollbar(area,command=self.tree.yview)
        vertical.grid(row=0,column=1,sticky='ns')
        horizontal = ttk.Scrollbar(area,orient='horizontal',command=self.tree.xview)
        horizontal.grid(row=1,column=0,sticky='ew')
        self.tree.configure(yscrollcommand=vertical.set,xscrollcommand=horizontal.set)
        self.details = tk.StringVar(self)
        self.tree.bind('<<TreeviewSelect>>', self.show_details)
        ttk.Label(self,textvariable=self.details,wraplength=850).pack(fill='x',pady=5)
        ttk.Label(self,textvariable=self.status,wraplength=850).pack(fill='x')
        self.bind('<Destroy>',lambda event:self.stop() if event.widget==self else None)

    def show_details(self, _event=None):
        selected = self.tree.selection()
        if selected:
            index = int(selected[0])
            action = self.plan[index]
            text = f'Conserver : {action.keep.path}\nDoublon : {action.extra.path}\nSHA-256 : {action.extra.sha256}'
            if index in self.manifests:
                text += f'\nJournal de restauration : {self.manifests[index]}'
            self.details.set(text)

    def select_all(self):
        if self.gate is None:
            self.tree.selection_set(*self.tree.get_children())

    def run(self, job, done):
        if self.gate is not None or self.external_busy():
            return
        gate = self.gate = Gate()
        self.on_state()
        for button in (self.scan_button,self.apply_button,self.restore_button,self.select_button):
            button.configure(state='disabled')
        def worker():
            try:
                self.messages.put(('done',job(gate)))
            except TaskStopped:
                self.messages.put(('error','Tâche annulée ; les opérations déjà terminées restent journalisées.'))
            except Exception as error:
                self.messages.put(('error',str(error)))
        Thread(target=worker,daemon=True,name='doublons-exacts').start()
        def poll():
            for _ in range(50):
                try:
                    kind,value = self.messages.get_nowait()
                except Empty:
                    break
                if kind == 'progress':
                    self.status.set(value)
                    assistant=getattr(self,'index_assistant',None)
                    if assistant is not None and assistant.running:
                        assistant.status.set(value)
                    continue
                self.gate = None
                self.on_state()
                for button in (self.scan_button,self.apply_button,self.restore_button,self.select_button):
                    button.configure(state='normal')
                if kind == 'error':
                    self.status.set(value)
                    assistant=getattr(self,'index_assistant',None)
                    if assistant is not None and assistant.running:
                        assistant.running=False
                        assistant.status.set(value)
                else:
                    done(value)
                return
            self.after(80,poll)
        self.after(80,poll)

    def scan(self):
        if self.gate is not None or self.external_busy():
            return
        roots = list(self.sources())
        if not roots:
            self.status.set('Ajoute au moins un dossier avec Sources… dans PhotoArchiviste.')
            return
        workspace = self.workspace()
        self.plan = []
        self.manifests = {}
        self.tree.delete(*self.tree.get_children())
        self.details.set('')
        self.status.set('Inventaire et vérification SHA-256 des sources…')
        def done(plan):
            self.plan = plan
            for index, action in enumerate(plan):
                self.tree.insert('','end',iid=str(index),values=('Proposé',str(action.keep.path),str(action.extra.path)))
            total = sum(action.extra.size for action in plan)
            self.status.set(f'{len(plan)} doublon(s) exact(s), {total / 1024**2:.1f} Mio. Relis puis sélectionne les lignes à retirer. '
                            'La quarantaine ne libère pas d’espace sur le disque.')
        self.run(lambda gate:audit(roots,gate,exclude=[workspace] if workspace else ()),done)

    def apply(self):
        if self.gate is not None or self.external_busy():
            return
        selected = [int(i) for i in self.tree.selection() if int(i) not in self.manifests]
        if not selected:
            self.status.set('Sélectionne les doublons proposés dans la liste.')
            return
        if not messagebox.askyesno('Retirer les doublons des sources',
                f'Déplacer {len(selected)} doublon(s) exact(s) dans une quarantaine sur leur disque ?\n\n'
                'Les exemplaires de la colonne Conserver restent en place.\n'
                'Chaque fichier sera revérifié avant déplacement. Aucune suppression définitive.\n'
                'Ne modifie pas les sources pendant cette opération.',parent=self):
            return
        actions = [(index,self.plan[index]) for index in selected]
        def job(gate):
            outcomes = []
            # Retourner aussi les opérations réussies si l’une des suivantes échoue.
            for index, action in actions:
                try:
                    gate.check()
                    outcomes.append((index,quarantine(action,gate),None))
                except Exception as error:
                    outcomes.append((index,None,str(error) or 'Opération annulée'))
                    break
            return outcomes
        def done(outcomes):
            moved = 0
            error = None
            for index, manifest, failure in outcomes:
                if manifest:
                    self.manifests[index] = manifest
                    self.tree.set(str(index),'state','Quarantaine')
                    moved += 1
                else:
                    self.tree.set(str(index),'state','À vérifier')
                    error = failure
            # Invalider aussi après un échec, qui peut suivre une publication réussie.
            self.changed()
            self.status.set(f'{moved} fichier(s) mis en quarantaine. Réindexe les sources. '
                            + (f'Arrêt : {error}' if error else 'Restauration disponible via le journal.'))
            self.show_details()
        self.run(job,done)

    def restore_file(self):
        if self.gate is not None or self.external_busy():
            return
        selected = self.tree.selection()
        manifest = self.manifests.get(int(selected[0])) if selected else None
        if manifest is None:
            roots = list(self.sources())
            path = askopenfilename(parent=self,title='Choisir le manifest.json de la quarantaine',
                                   initialdir=str(roots[0] / QUARANTINE) if roots else None,
                                   filetypes=[('Journal de quarantaine','manifest.json')])
            if not path:
                return
            manifest = Path(path)
        if not messagebox.askyesno('Restaurer',f'Restaurer le fichier décrit par :\n{manifest}\n\n'
                                  'Un fichier déjà présent ne sera jamais écrasé.',parent=self):
            return
        def done(path):
            self.changed()
            self.status.set(f'Restauré : {path}. Relance l’analyse avant toute nouvelle action.')
            self.plan = []
            self.manifests = {}
            self.tree.delete(*self.tree.get_children())
        self.run(lambda gate:restore(manifest,gate),done)

    def stop(self):
        if self.gate is not None:
            self.gate.stopped.set()
