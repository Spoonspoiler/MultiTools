"""Agent local dans le tri : analyse reprenable, questions fermées et annotations."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
import sqlite3
import tkinter as tk
from tkinter import ttk, simpledialog, filedialog

from ..disk_picker import askdirectory, ask_new_directory
from .ai_review import ReviewStore, analyse_index, context_key, SCENES
from .preview import Preview
from .tag_editor import TagEditor
from .adaptive_panel import AdaptivePanel
from .comparison_group import comparison_group
from .group_comparison_ui import GroupComparison


class DescriptionDialog(simpledialog.Dialog):
    """Éditeur multiligne redimensionnable, sans enregistrement à l'annulation."""
    def __init__(self, parent, value):
        self.initial_value = value
        super().__init__(parent, title='Modifier la description')

    def body(self, master):
        from tkinter.scrolledtext import ScrolledText
        self.resizable(True, True)
        self.minsize(420, 300)
        ttk.Label(master, text='Corrige la description de Qwen, puis enregistre les modifications.').pack(
            anchor='w', pady=(0, 8))
        self.editor = ScrolledText(master, wrap='word', width=85, height=24,
                                   undo=True, padx=10, pady=10)
        self.editor.pack(fill='both', expand=True)
        self.editor.insert('1.0', self.initial_value)
        self.editor.edit_reset()
        self.editor.mark_set('insert', '1.0')
        master.pack_configure(fill='both', expand=True)
        return self.editor

    def buttonbox(self):
        buttons = ttk.Frame(self)
        buttons.pack(fill='x', padx=10, pady=(0, 10))
        ttk.Button(buttons, text='Annuler', command=self.cancel).pack(side='right')
        ttk.Button(buttons, text='Enregistrer', command=self.ok).pack(side='right', padx=8)
        self.bind('<Escape>', self.cancel)
        self.bind('<Control-Return>', self.ok)

    def apply(self):
        self.result = self.editor.get('1.0', 'end-1c')


SCENE_LABELS={'famille':'Famille / souvenir','portrait':'Portrait','paysage':'Paysage',
              'document':'Document','capture_ecran':'Capture d’écran','objet':'Objet',
              'autre':'Autre','incertain':'À préciser'}


class IndexAssistantPane(ttk.Frame):
    def __init__(self,parent,owner):
        super().__init__(parent,padding=8)
        self.owner=owner
        self.context=None
        self.questions=[]
        self.position=0
        self.running=False
        self.last_workspace=None
        self.status=owner.status
        self.observation=tk.StringVar(self)
        self.summary=tk.StringVar(self)
        self.observation.trace_add('write',lambda *_:self.summary.set(
            self.observation.get()[:140]+('…' if len(self.observation.get())>140 else '')))
        self.counter=tk.StringVar(self,'Choisir une source puis lancer Indexer')
        self.decision=tk.StringVar(self,'a_revoir')
        self.scene=tk.StringVar(self,SCENE_LABELS['incertain'])
        self.tags=tk.StringVar(self)
        self.note=tk.StringVar(self)
        self.pending_only=tk.BooleanVar(self,True)
        self.route=tk.StringVar(self)
        self.comparison_enabled=tk.BooleanVar(self,False)
        style=ttk.Style(self)
        style.configure('Photo.Side.TFrame',background='#f5f7fa')
        style.configure('Photo.Side.TLabel',background='#f5f7fa',foreground='#243444')
        style.configure('Photo.Title.TLabel',background='#f5f7fa',foreground='#152a36',font=('TkDefaultFont',12,'bold'))
        style.configure('Photo.Primary.TButton',padding=(12,10),font=('TkDefaultFont',10,'bold'))

        bar=ttk.Frame(self)
        bar.pack(fill='x',pady=(0,6))
        bar.columnconfigure(2,weight=1)
        ttk.Button(bar,text='←',width=3,command=lambda:self.navigate(-1)).grid(row=0,column=0)
        ttk.Button(bar,text='→',width=3,command=lambda:self.navigate(1)).grid(row=0,column=1,padx=(4,8))
        ttk.Label(bar,textvariable=self.counter,width=1,anchor='w').grid(row=0,column=2,sticky='ew',padx=4)
        self.pause_button=ttk.Button(bar,text='Pause',command=self.pause)
        self.pause_button.grid(row=0,column=3,padx=4)
        ttk.Button(bar,text='Arrêter',command=self.cancel).grid(row=0,column=4)
        tools=ttk.Menubutton(bar,text='Outils ▾')
        menu=tk.Menu(tools,tearoff=False)
        menu.add_command(label='Reprendre les analyses manquantes',command=self.start)
        menu.add_command(label='Actualiser les questions',command=self.reload)
        menu.add_checkbutton(label='Questions sans réponse uniquement',variable=self.pending_only,
                             command=lambda:self.refresh(self.context))
        menu.add_separator()
        menu.add_command(label='Nouveau dossier…',command=self.create_folder)
        menu.add_command(label='Exporter mes annotations…',command=self.export)
        tools.configure(menu=menu)
        tools.grid(row=0,column=5,padx=(6,0))

        body=ttk.Frame(self)
        body.pack(fill='both',expand=True)
        body.columnconfigure(0,weight=1,minsize=300)
        body.columnconfigure(1,weight=0,minsize=310)
        body.rowconfigure(0,weight=1)
        self.executor=ThreadPoolExecutor(max_workers=1,thread_name_prefix='question-photo')
        self.images=ttk.Frame(body)
        self.images.grid(row=0,column=0,sticky='nsew',padx=(0,14))
        self.images.columnconfigure((0,1),weight=1)
        self.images.rowconfigure(2,weight=1)
        image_tools=ttk.Frame(self.images)
        self.image_tools=image_tools
        image_tools.grid(row=0,column=0,columnspan=2,sticky='ew',pady=(0,5))
        image_tools.columnconfigure(3,weight=1)
        self.compare_button=ttk.Button(image_tools,text='Comparer',command=self.toggle_comparison,state='disabled')
        fit=ttk.Button(image_tools,text='Ajuster',command=lambda:self.preview._zoom(1))
        zoom=ttk.Button(image_tools,text='×2',width=3,command=lambda:self.preview._zoom(2))
        full=ttk.Button(image_tools,text='Plein écran',command=lambda:self.preview.open_fullscreen())
        info=ttk.Button(image_tools,text='Infos',command=self.show_photo_info)
        self.image_buttons=(self.compare_button,fit,zoom,info,full)
        self.image_layout=None
        image_tools.bind('<Configure>',self._layout_image_tools)
        self._layout_image_tools()
        self.related_select=ttk.Combobox(self.images,state='disabled')
        self.related_select.grid(row=1,column=0,columnspan=2,sticky='ew',pady=(0,5))
        self.related_select.bind('<<ComboboxSelected>>',self.show_related)
        self.preview=Preview(self.images,self.executor,compact=True)
        self.preview.canvas.configure(width=450,height=380)
        self.preview.grid(row=2,column=0,columnspan=2,sticky='nsew')
        self.other_preview=Preview(self.images,self.executor,compact=True)
        self.other_preview.canvas.configure(width=300,height=380)
        self.other_preview.grid(row=2,column=1,sticky='nsew')
        self.other_preview.grid_remove()
        self.related_select.grid_remove()
        self.related_photos=()
        self.group_board=GroupComparison(self.images,self.executor,self.focus_photo,self.inspect_detail,
                                        self.apply_group_selection,self.owner.idle)
        self.group_board.grid(row=2,column=0,columnspan=2,sticky='nsew')
        self.group_board.grid_remove()
        observation_panel=ttk.Frame(self.images)
        observation_panel.grid(row=3,column=0,columnspan=2,sticky='ew',pady=(8,0))
        observation_header=ttk.Frame(observation_panel)
        observation_header.pack(fill='x')
        ttk.Label(observation_header,text='Observation de Qwen',font=('TkDefaultFont',10,'bold')).pack(side='left')
        ttk.Button(observation_header,text='Corriger…',command=self.edit_note).pack(side='right')
        ttk.Button(observation_header,text='Lier à la source…',command=self.link_source).pack(side='right',padx=4)
        self.observation_label=ttk.Label(observation_panel,textvariable=self.observation,
                                        wraplength=450,width=1,justify='left')
        self.observation_label.pack(fill='x',pady=(4,0))
        observation_panel.bind('<Configure>',lambda event:self.observation_label.configure(wraplength=max(180,event.width-8)))

        form=ttk.Frame(body,padding=10,width=310,style='Photo.Side.TFrame')
        form.grid(row=0,column=1,sticky='nsew')
        form.grid_propagate(False)
        form.columnconfigure(0,weight=1)
        form.rowconfigure(2,weight=1)
        ttk.Label(form,text='Votre décision',style='Photo.Title.TLabel').grid(row=0,column=0,sticky='w')
        choices=ttk.Frame(form,style='Photo.Side.TFrame')
        choices.grid(row=1,column=0,sticky='ew',pady=(6,8))
        for code,label in [('garder','Garder'),('a_revoir','À revoir'),('a_jeter','À jeter')]:
            ttk.Radiobutton(choices,text=label,value=code,variable=self.decision).pack(side='left',padx=(0,8))
        self.details_panel=AdaptivePanel(form)
        self.details_panel.grid(row=2,column=0,sticky='nsew')
        details=self.details_panel.content
        self.tag_editor=TagEditor(details,self.tags,on_change=self.update_route_options,
                                  on_error=self.status.set,can_edit=self.owner.idle)
        self.tag_editor.pack(fill='x',pady=(3,8))
        type_row=ttk.Frame(details,style='Photo.Side.TFrame')
        type_row.pack(fill='x',pady=(3,7))
        ttk.Label(type_row,text='Catégorie',width=9,style='Photo.Side.TLabel').pack(side='left')
        ttk.Combobox(type_row,textvariable=self.scene,values=list(SCENE_LABELS.values()),state='readonly',width=14).pack(side='left',fill='x',expand=True)
        route_row=ttk.Frame(details,style='Photo.Side.TFrame')
        route_row.pack(fill='x',pady=(0,7))
        ttk.Label(route_row,text='Dossier',width=9,style='Photo.Side.TLabel').pack(side='left')
        self.route_select=ttk.Combobox(route_row,textvariable=self.route,state='readonly',width=14)
        self.route_select.pack(side='left',fill='x',expand=True)
        folders=ttk.Frame(details,style='Photo.Side.TFrame')
        folders.pack(fill='x')
        ttk.Button(folders,text='Associer tag…',command=self.set_route).pack(side='left',fill='x',expand=True)
        ttk.Button(folders,text='Nouveau dossier…',command=self.create_folder).pack(side='left',padx=(4,0))
        actions=ttk.Frame(form,style='Photo.Side.TFrame')
        actions.grid(row=3,column=0,sticky='ew',pady=(6,0))
        self.validate_button=ttk.Button(actions,text='Valider et suivante →',command=self.answer,style='Photo.Primary.TButton')
        self.validate_button.pack(fill='x',pady=(0,4))
        self.reject_group_button=ttk.Button(actions,text='Photo et doublons → À jeter…',
                   command=lambda:self.mark_manual('a_jeter',include_exact=True))
        self.reject_group_button.pack(fill='x',pady=4)
        self.reject_other=ttk.Button(actions,text='Écarter la variante affichée',
                   command=lambda:self.mark_manual('a_jeter',other=True))
        self.bind('<Destroy>',lambda event:self.executor.shutdown(wait=False,cancel_futures=True) if event.widget==self else None)

    def _layout_image_tools(self,event=None):
        width=event.width if event is not None else self.image_tools.winfo_width()
        mode='wide' if width>=520 else 'compact'
        if self.image_layout==mode: return
        self.image_layout=mode
        positions=((0,0),(0,1),(0,2),(0,4),(0,5)) if mode=='wide' else ((0,0),(0,1),(0,2),(1,0),(1,1))
        for button,(row,column) in zip(self.image_buttons,positions):
            button.grid(row=row,column=column,sticky='w',padx=(0,4),pady=2)

    def create_folder(self):
        if not self.owner.idle():
            self.status.set('Arrêter le passage avant de choisir un nouveau dossier.')
            return
        photo=self.preview.photo
        initial=photo.path.parent if photo else self.owner.workspace()
        folder=ask_new_directory(parent=self,initialdir=str(initial) if initial else None)
        if folder:
            self.status.set('Dossier créé : '+folder+' · Associer tag permet de l’utiliser pour le classement.')

    def toggle_comparison(self):
        if not self.owner.idle(): return
        self.comparison_enabled.set(not self.comparison_enabled.get())
        self._update_comparison()

    def _update_comparison(self):
        current=self.preview.photo
        try:
            photos=comparison_group(self.owner.rows,current,self.related_photos) if current else ()
        except ValueError as error:
            self.status.set(str(error))
            photos=()
        available=len(photos)>1
        self.compare_button.configure(state='normal' if available else 'disabled')
        if not available: self.comparison_enabled.set(False)
        visible=available and self.comparison_enabled.get()
        self.compare_button.configure(text='Retour à la photo' if visible else f'Comparer ({len(photos)})' if available else 'Comparer')
        self.other_preview.grid_remove()
        self.related_select.grid_remove()
        self.reject_other.pack_forget()
        if visible:
            self.preview.grid_remove()
            states={row['photo'].path:row['decision'] for row in self.owner.rows}
            self.group_board.set_group(photos,states,current.path)
            self.group_board.grid()
        else:
            self.group_board.grid_remove()
            self.group_board.cancel_loading()
            self.preview.grid(columnspan=2)

    def inspect_detail(self,photo):
        if not self.owner.idle(): return
        self.focus_photo(photo)
        self.preview.open_fullscreen(native=True)

    def apply_group_selection(self,photos,keepers,allow_empty=False):
        if not self.owner.review_group(photos,keepers,allow_empty): return False
        current=self.preview.photo
        by_path={row['photo'].path:row for row in self.owner.rows}
        if current and current.path in by_path: self.decision.set(by_path[current.path]['decision'])
        try:
            with ReviewStore(self.store_path()) as store:
                affected={photo.path for photo in photos}
                for index,question in enumerate(self.questions):
                    if question['photo'].path in affected:
                        updated=store.question(self.context,question['photo'])
                        if updated is not None: self.questions[index]=updated
        except (OSError,ValueError,sqlite3.Error) as error:
            self.status.set(f'Choix enregistrés ; questions à recharger : {error}')
        self._update_comparison()
        return True

    def link_source(self):
        if not self.owner.idle() or self.preview.photo is None: return
        from ..provenance import link
        photo = self.preview.photo
        selected = filedialog.askopenfilename(parent=self, title='Image source de cette version traitée')
        if not selected: return
        self.status.set('Enregistrement du lien avec la source…')
        future = self.executor.submit(link, Path(selected), photo.path,
                                      'Traitement déclaré par l’utilisateur', manual=True)
        def poll():
            if not future.done(): self.after(100, poll); return
            try:
                future.result()
                self.status.set('Source liée. Relancer l’analyse IA pour prendre en compte la provenance.')
            except (OSError, ValueError) as error:
                self.status.set(f'Lien non enregistré : {error}')
        self.after(100, poll)

    def edit_note(self):
        if not self.owner.idle() or not self.questions: return
        question = self.questions[self.position]
        dialog = DescriptionDialog(self, question['proposal']['observation'])
        value = dialog.result
        if value is None: return
        try:
            with ReviewStore(self.store_path()) as store:
                store.edit_observation(self.context, question['photo'], value)
                updated = store.question(self.context, question['photo'])
            if updated is not None:
                self.questions[self.position] = updated
                self.show()
            self.status.set('Description corrigée enregistrée.')
        except (OSError, ValueError, sqlite3.Error) as error:
            self.status.set(f'Impossible d’enregistrer la description : {error}')

    def show_photo_info(self):
        photo=self.preview.photo
        if photo is None: return
        window=tk.Toplevel(self)
        window.title('Détails de la photo')
        window.geometry('680x300')
        from tkinter.scrolledtext import ScrolledText
        text=ScrolledText(window,wrap='word',padx=8,pady=8)
        text.pack(fill='both',expand=True)
        text.insert('1.0',self.preview.info.get('1.0','end').strip()+'\n\n'+self.observation.get())
        text.configure(state='disabled')
        ttk.Button(window,text='Corriger les observations…',command=self.edit_note).pack(side='left',padx=8,pady=6)
        ttk.Button(window,text='Fermer',command=window.destroy).pack(pady=6)

    def store_path(self):
        workspace=self.owner.workspace()
        if workspace is None:
            raise ValueError('Choisir le dossier de travail de PhotoArchiviste.')
        return workspace/'assistant-tri.sqlite3'

    def photos(self):
        return [row['photo'] for row in self.owner.rows if not row['manifest']]

    def begin_pass(self):
        self.running=True
        self.status.set('Indexation et assistant en cours. Les résultats terminés sont enregistrés.')
        self.questions=[]
        self.position=0
        self.pause_button.configure(text='Pause après la photo')
        self.counter.set('Passage inventaire + assistant en cours')
        self.observation.set('Les analyses terminées seront disponibles comme questions à valider.')
        self.tags.set('')
        self.tag_editor.set_tags([],[])
        self.details_panel.canvas.yview_moveto(0)
        self.note.set('')
        self.route.set('')
        self.preview.set_photo(None)
        self.other_preview.set_photo(None)
        self.related_photos=()
        self.related_select.configure(values=(),state='disabled')
        self._update_comparison()

    def start(self):
        if not self.owner.idle(): return
        if not self.owner.rows:
            self.owner.load()
        if not self.owner.rows:
            self.status.set('Indexer les sources avant de lancer l’agent IA.')
            return
        photos=self.photos()
        if not photos:
            self.status.set('Charger les photos indexées dans la liste du tri avant de lancer l’agent.')
            return
        try: path=self.store_path()
        except (OSError,ValueError,sqlite3.Error) as error:
            self.status.set(str(error)); return
        model,preferences=self.owner.model.get(),self.owner.preferences.get()
        self.context=context_key(model,preferences)
        self.running=True
        self.pause_button.configure(text='Pause après la photo')
        self.status.set(f'Analyse de {len(photos)} référence(s), une inférence à la fois. Tu peux revenir plus tard.')
        def progress(message): self.owner.messages.put(('progress',message))
        def done(result):
            self.running=False
            self.refresh(result['context'])
            self.status.set(f'{result["processed"]} nouvelle(s) analyse(s), {result["cached"]} réutilisée(s), '
                            f'{len(result["errors"])} erreur(s). '
                            + ('Analyse arrêtée ; reprise possible. ' if result['cancelled'] or result.get('stopped_on_errors') else 'Questions prêtes. ')
                            + (result['errors'][-1] if result['errors'] else ''))
        self.owner.run(lambda gate:analyse_index(photos,path,self.owner.client,model,preferences,gate,progress),done)

    def pause(self):
        gate=self.owner.gate or self.owner.external_gate()
        if gate is None: return
        if gate.running.is_set():
            gate.running.clear()
            self.pause_button.configure(text='Reprendre l’analyse')
            self.status.set('Pause demandée ; la requête en cours peut terminer avant l’attente.')
        else:
            gate.running.set()
            self.pause_button.configure(text='Pause après la photo')
            self.status.set('Analyse reprise.')

    def cancel(self):
        if self.owner.gate is not None or self.owner.external_gate() is not None:
            gate=self.owner.gate or self.owner.external_gate()
            if gate is not None:
                gate.stopped.set()
                gate.running.set()

    def reload(self):
        if self.owner.idle():
            self.refresh()

    def refresh(self,context=None):
        if not self.owner.idle(): return
        try:
            path=self.store_path()
            with ReviewStore(path) as store:
                if self.last_workspace != path:
                    saved=store.preferences()
                    if saved is not None: self.owner.preferences.set(saved)
                    self.last_workspace=path
                self.context=context or context_key(self.owner.model.get(),self.owner.preferences.get())
                questions=[store.question(self.context,photo) for photo in self.photos()]
            self.questions=[question for question in questions if question is not None
                            and (not self.pending_only.get() or question['answer'] is None or
                                 (question['answer']['decision']=='garder' and len(question['destinations'])>1 and not question['destination']))]
            self.owner.apply_agent_proposals([question for question in questions if question is not None])
            self.position=min(self.position,max(0,len(self.questions)-1))
            self.show()
        except (OSError,ValueError,KeyError,sqlite3.Error) as error:
            self.status.set(f'Questions indisponibles : {error}')

    def show(self):
        if not self.questions:
            self.preview.set_photo(None)
            self.other_preview.set_photo(None)
            self.related_select.configure(values=(),state='disabled')
            self.counter.set('Aucune question en attente pour cet index et ces consignes.')
            self.observation.set('')
            self.tag_editor.set_tags([],[])
            self.related_photos=()
            self._update_comparison()
            return
        question=self.questions[self.position]
        proposal,answer=question['proposal'],question['answer']
        self.preview.set_photo(question['photo'])
        self.related_photos=tuple(photo for photo in self.owner.related(question['photo']) if photo.path!=question['photo'].path)
        self.related_select.configure(values=[f'{photo.format} · {photo.path.name} · {photo.path.parent}' for photo in self.related_photos],
                                      state='readonly' if self.related_photos else 'disabled')
        if self.related_photos:
            self.related_select.current(0)
            self.show_related()
        else:
            self.related_select.set('Aucun doublon ou proche dans l’index')
            self.other_preview.set_photo(None)
        self.counter.set(f'Question {self.position+1}/{len(self.questions)}')
        self.observation.set(proposal['observation']+'\n'
            + f'Proposition : {dict(garder="Garder",a_jeter="À jeter",a_revoir="À revoir")[proposal["decision"]]} · confiance déclarée : {proposal["confiance"]}')
        self.decision.set(answer['decision'] if answer else 'a_revoir')
        self.scene.set(SCENE_LABELS[answer['scene'] if answer else proposal['scene']])
        self.tag_editor.set_tags(proposal['tags'],answer['tags'] if answer else proposal['tags'])
        self.details_panel.canvas.yview_moveto(0)
        self.note.set(answer['note'] if answer else '')
        self.route_select.configure(values=('',*question['destinations']))
        self.route.set(question['destination'])
        self.details_panel.schedule_fit()
        self._update_comparison()

    def show_related(self,_event=None):
        self.other_preview.set_photo(None)
        self._update_comparison()

    def mark_manual(self,decision,include_exact=False,other=False):
        if not self.owner.idle(): return
        photo=self.other_preview.photo if other else self.preview.photo
        if photo is None:
            self.status.set('Choisis une photo dans la liste ou une variante à comparer.')
            return
        if not self.owner.manual_review(photo,decision,include_exact): return
        self.refresh(self.context)
        if not self.questions:
            remaining=[row for row in self.owner.rows if not row['manifest']
                       and row['decision']=='a_revoir' and row['photo'].path!=photo.path]
            if remaining:
                self.focus_photo(remaining[0]['photo'])
            else:
                self.counter.set('Choix manuels enregistrés — le plan peut être préparé.')
                self.status.set('Aucun fichier déplacé. Appliquer le classement validé pour extraire les rejets vers À jeter.')

    def focus_photo(self,photo):
        if not self.owner.idle(): return
        self.pending_only.set(False)
        self.refresh(self.context)
        for index,question in enumerate(self.questions):
            if question['photo'].path==photo.path:
                self.position=index
                self.show()
                return
        self.questions=[]
        self.observation.set('Cette photo n’a pas encore de question IA enregistrée.')
        self.tags.set('')
        self.tag_editor.set_tags([],[])
        self.note.set('')
        self.route.set('')
        self.decision.set('a_revoir')
        self.preview.set_photo(photo)
        self.related_photos=tuple(other for other in self.owner.related(photo) if other.path!=photo.path)
        self.related_select.configure(values=[str(other.path) for other in self.related_photos],
                                      state='readonly' if self.related_photos else 'disabled')
        if self.related_photos:
            self.related_select.current(0)
            self.show_related()
        else:
            self.other_preview.set_photo(None)
        self._update_comparison()
        self.counter.set('Tri manuel · '+photo.path.name)
        self.status.set('Relancer Indexer ou reprendre les analyses manquantes. Cette photo peut être comparée sans IA.')

    def update_route_options(self):
        if not self.owner.idle(): return
        try:
            with ReviewStore(self.store_path()) as store:
                choices=store.destinations(self.tag_editor.model.values())
            selected=self.route.get()
            self.route_select.configure(values=('',*choices))
            self.route.set(selected if selected in choices else (choices[0] if len(choices)==1 else ''))
            self.details_panel.schedule_fit()
        except (OSError,ValueError,sqlite3.Error) as error:
            self.status.set(f'Destinations indisponibles : {error}')

    def set_route(self):
        if not self.owner.idle(): return
        tag=simpledialog.askstring('Tag → dossier','Quel tag doit classer vers ce dossier ?',parent=self,
                                  initialvalue=next(iter(self.tag_editor.model.values()),''))
        if not tag: return
        destination=askdirectory(parent=self,title=f'Dossier de destination pour le tag « {tag} »')
        if not destination: return
        try:
            with ReviewStore(self.store_path()) as store: store.set_route(tag,destination)
            self.update_route_options()
            self.status.set(f'Règle enregistrée : {tag} → {destination}. Les tags proposés doivent encore être validés.')
        except (OSError,ValueError,sqlite3.Error) as error:
            self.status.set(f'Règle non enregistrée : {error}')

    def navigate(self,offset):
        if not self.questions and self.owner.idle():
            rows=[row for row in self.owner.rows if not row['manifest']]
            if rows:
                current=self.preview.photo
                index=next((i for i,row in enumerate(rows) if current and row['photo'].path==current.path),0)
                self.focus_photo(rows[max(0,min(index+offset,len(rows)-1))]['photo'])
            return
        if self.questions and self.owner.idle():
            self.position=max(0,min(self.position+offset,len(self.questions)-1))
            self.show()

    def answer(self):
        if not self.owner.idle(): return
        if not self.tag_editor.commit_pending(): return
        photo=self.questions[self.position]['photo'] if self.questions else self.preview.photo
        if photo is None:
            self.status.set('Choisis une photo avant de valider ses tags.')
            return
        scene=next(code for code,label in SCENE_LABELS.items() if label==self.scene.get())
        tags=self.tag_editor.model.values()
        try:
            with ReviewStore(self.store_path()) as store:
                choices=store.destinations(tags)
                destination=self.route.get()
                if destination not in choices: destination=choices[0] if len(choices)==1 else ''
                if self.questions:
                    store.answer(self.context,photo,self.decision.get(),scene,tags,self.note.get(),destination)
                else:
                    store.answer_manual(photo,self.decision.get(),scene,tags,self.note.get(),destination)
            self.owner.apply_agent_answer(photo,self.decision.get(),tags,self.note.get())
            self.refresh(self.context)
            if not self.questions:
                remaining=[row for row in self.owner.rows if not row['manifest']
                           and row['decision']=='a_revoir' and row['photo'].path!=photo.path]
                if remaining: self.focus_photo(remaining[0]['photo'])
        except (OSError,ValueError,sqlite3.Error) as error:
            self.status.set(f'Réponse non enregistrée : {error}')

    def sync_decision(self,photo,decision):
        """Une correction dans la liste reste prioritaire sur une ancienne réponse."""
        if self.context is None: return
        with ReviewStore(self.store_path()) as store:
            question=store.question(self.context,photo)
            if question is None: return
            answer=question['answer']
            store.answer(self.context,photo,decision,answer['scene'] if answer else 'incertain',
                         answer['tags'] if answer else [],answer['note'] if answer else 'Décision dans la liste ; tags non validés.',
                         question['destination'] if answer else '')

    def export(self):
        if not self.owner.idle(): return
        folder=askdirectory(parent=self,title='Exporter les annotations humaines pour un futur entraînement')
        if not folder: return
        target=Path(folder)/('annotations-tri-'+datetime.now().strftime('%Y%m%d-%H%M%S-%f')+'.jsonl')
        try:
            with ReviewStore(self.store_path()) as store: count=store.export(target)
            self.status.set(f'{count} annotation(s) exportée(s) : {target}. Aucun modèle réentraîné.')
        except (OSError,ValueError,sqlite3.Error) as error:
            self.status.set(f'Export impossible : {error}')
