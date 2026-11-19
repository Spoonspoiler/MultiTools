"""Une source à trier, une destination À jeter et des propositions révisables."""
from collections import defaultdict
import json
from pathlib import Path
from queue import SimpleQueue
import sqlite3
import tkinter as tk
from tkinter import ttk, messagebox

from ..disk_picker import askdirectory, askopenfilename
from ..ollama import Ollama, DEFAULT_MODEL
from .dedup_ui import DedupPane
from .exact_duplicates import Fingerprint
from .path_safety import checked_stat, file_evidence
from .source_sorting import suggest, extract, restore_extraction, save_review, read_review, JOURNAL
from .index_assistant_ui import IndexAssistantPane
from .ai_review import ReviewStore
from .tag_routing import plan_routes, Route
from .review import photo_format
from .inventory import Photo
from .manual_review import manual_targets, with_manual_rejections
from .comparison_group import partition_group
from .ai_review import context_key

LABELS = {'garder':'Garder','a_jeter':'À jeter','a_revoir':'À revoir'}


class SourceSortPane(DedupPane):
    def __init__(self,parent,photos,sources,changed,external_busy,on_state,client=None,workspace=lambda:None,
                 external_gate=lambda:None,related=lambda photo:()):
        ttk.Frame.__init__(self,parent,padding=8)
        self.get_photos,self.sources,self.changed = photos,sources,changed
        self.external_busy,self.on_state = external_busy,on_state
        self.client = client or Ollama()
        self.workspace = workspace
        self.external_gate,self.related=external_gate,related
        self.gate = None
        self.messages = SimpleQueue()
        self.rows = []
        self.offset = 0
        self.destination = None
        self.route_cache={}
        self.format_var=tk.StringVar(self,'Tous les formats')
        self.only_related=tk.BooleanVar(self,False)
        self.visible_indices=[]
        self.status = tk.StringVar(self,'Indexer lance le passage inventaire + assistant. Valider les questions et tags prépare le classement.')
        self.details = tk.StringVar(self)
        self.destination_text = tk.StringVar(self,'Dossier À jeter non choisi')
        self.preferences = tk.StringVar(self,'Conserver les souvenirs et les photos utiles. Proposer À revoir en cas de doute.')
        self.model = tk.StringVar(self,DEFAULT_MODEL)
        bar = ttk.Frame(self)
        self.scan_button = ttk.Button(bar,text='Charger les photos indexées',command=self.load)
        self.scan_button.pack(side='left')
        ttk.Button(bar,text='Propositions IA (1 à 6 photos)',command=self.propose_ai).pack(side='left',padx=6)
        self.select_button = ttk.Button(bar,text='Valider les propositions sélectionnées',command=self.accept)
        self.select_button.pack(side='left')
        ttk.Button(bar,text='Annuler la tâche',command=self.stop).pack(side='left',padx=6)
        options = ttk.Frame(self)
        ttk.Label(options,text='Priorités du tri').pack(side='left')
        ttk.Entry(options,textvariable=self.preferences).pack(side='left',fill='x',expand=True,padx=6)
        ttk.Entry(options,textvariable=self.model,width=25).pack(side='left')
        self.work_tabs=ttk.Notebook(self)
        self.work_tabs.pack(fill='both',expand=True)
        area = ttk.Frame(self.work_tabs)
        self.work_tabs.add(area,text='Liste et plan')
        filters=ttk.Frame(area)
        filters.pack(fill='x')
        self.format_select=ttk.Combobox(filters,textvariable=self.format_var,values=('Tous les formats',),state='readonly',width=18)
        self.format_select.pack(side='left')
        self.format_select.bind('<<ComboboxSelected>>',lambda _:self.filter_changed())
        ttk.Checkbutton(filters,text='Doublons et photos proches uniquement',variable=self.only_related,
                        command=self.filter_changed).pack(side='left',padx=8)
        decisionbar = ttk.Frame(area)
        decisionbar.pack(fill='x',pady=6)
        for code,label in LABELS.items():
            ttk.Button(decisionbar,text=label+' (sélection)',command=lambda code=code:self.decide(code)).pack(side='left',padx=3)
        ttk.Button(decisionbar,text='Page précédente',command=lambda:self.page(-1)).pack(side='right')
        ttk.Button(decisionbar,text='Page suivante',command=lambda:self.page(1)).pack(side='right')
        table=ttk.Frame(area)
        table.pack(fill='both',expand=True)
        self.tree = ttk.Treeview(table,columns=('decision','proposal','target','name'),show='headings',selectmode='extended',height=9)
        for key,title,width in [('decision','Ta décision',90),('proposal','Proposition',100),('target','Dossier prévu / attente',230),('name','Photo — chemin complet',390)]:
            self.tree.heading(key,text=title)
            self.tree.column(key,width=width)
        self.tree.pack(side='left',fill='both',expand=True)
        scrollbar = ttk.Scrollbar(table,command=self.tree.yview)
        scrollbar.pack(side='right',fill='y')
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.bind('<<TreeviewSelect>>',self.show_details)
        self.tree.bind('<Double-1>',self.preview)
        self.index_assistant=IndexAssistantPane(self.work_tabs,self)
        self.work_tabs.add(self.index_assistant,text='Photos')
        self.work_tabs.select(self.index_assistant)
        ttk.Label(area,textvariable=self.details,wraplength=850).pack(fill='x',pady=5)
        destbar = ttk.Frame(self)
        ttk.Button(destbar,text='Choisir le dossier unique À jeter…',command=self.choose_destination).pack(side='left')
        ttk.Label(destbar,textvariable=self.destination_text,wraplength=500).pack(side='left',padx=6)
        actions = ttk.Frame(self)
        actions.pack(fill='x',pady=6)
        self.apply_button = ttk.Button(actions,text='Appliquer le classement…',command=self.apply)
        self.apply_button.pack(side='left')
        self.new_folder_button=ttk.Button(actions,text='Nouveau dossier…',command=self.index_assistant.create_folder)
        self.new_folder_button.pack(side='left',padx=(8,0))
        tools=ttk.Menubutton(actions,text='Options ▾')
        menu=tk.Menu(tools,tearoff=False)
        menu.add_command(label='Dossier À jeter…',command=self.choose_destination)
        menu.add_command(label='Nouveau dossier…',command=self.index_assistant.create_folder)
        menu.add_command(label='Règles tag → dossier…',command=self.index_assistant.set_route)
        menu.add_command(label='Réglages de l’assistant…',command=self.show_settings)
        menu.add_separator()
        menu.add_command(label='Restaurer une extraction…',command=self.restore_file)
        menu.add_command(label='Exporter mes annotations…',command=self.index_assistant.export)
        tools.configure(menu=menu)
        tools.pack(side='left',padx=8)
        self.restore_button = ttk.Button(actions,text='Restaurer une extraction…',command=self.restore_file)
        ttk.Label(self,textvariable=self.status,wraplength=850).pack(fill='x')
        self.bind('<Destroy>',lambda event:self.stop() if event.widget==self else None)

    def show_settings(self):
        if not self.idle():
            self.status.set('Arrêter ou terminer le passage avant de modifier ses réglages.')
            return
        window=tk.Toplevel(self)
        window.title('Réglages de PhotoArchiviste')
        window.geometry('680x360')
        form=ttk.Frame(window,padding=18)
        form.pack(fill='both',expand=True)
        ttk.Label(form,text='Consignes pour Qwen').pack(anchor='w')
        ttk.Entry(form,textvariable=self.preferences).pack(fill='x',pady=(5,16))
        ttk.Label(form,text='Modèle local').pack(anchor='w')
        ttk.Entry(form,textvariable=self.model).pack(fill='x',pady=(5,16))
        ttk.Label(form,text='Dossier À jeter : '+str(self.destination or 'non défini'),wraplength=620).pack(anchor='w',pady=8)
        ttk.Label(form,text='Les décisions sont enregistrées. Les fichiers ne bougent qu’après validation du plan.',wraplength=620).pack(anchor='w',pady=8)
        def close():
            try:
                workspace=self.workspace()
                if workspace:
                    with ReviewStore(workspace/'assistant-tri.sqlite3') as store:
                        store.set_preferences(self.preferences.get())
                self.save()
            except (OSError,ValueError,sqlite3.Error) as error:
                self.status.set(f'Réglages non enregistrés : {error}')
                return
            window.destroy()
        ttk.Button(form,text='Enregistrer',command=close).pack(side='bottom',anchor='e',pady=12)

    def idle(self):
        return self.gate is None and not self.external_busy()

    def save(self):
        workspace=self.workspace()
        if workspace:
            try:
                save_review(workspace/'tri-source.json',self.rows,self.destination)
            except (OSError,ValueError) as error:
                self.status.set(f'Les décisions ne sont pas sauvegardées : {error}')
                return False
        return True

    def load(self):
        if not self.idle():
            return
        photos = list(self.get_photos())
        roots = list(self.sources())
        if not photos:
            self.status.set('Indexer les sources dans PhotoArchiviste avant de charger cette liste.')
            return
        rows = []
        for photo in photos:
            if self.destination and photo.path.is_relative_to(self.destination):
                continue
            matches = [root for root in roots if photo.path.is_relative_to(root)]
            if matches:
                rows.append({'photo':photo,'root':max(matches,key=lambda p:len(p.parts)),
                             'decision':'a_revoir','proposal':'a_revoir','reason':'Pas encore évaluée.','manifest':None})
        groups = defaultdict(list)
        for row in rows:
            groups[(row['photo'].size,row['photo'].sha256)].append(row)
        for group in groups.values():
            if len(group)>1:
                group.sort(key=lambda row:str(row['photo'].path))
                for index,row in enumerate(group):
                    row['proposal']='garder' if index==0 else 'a_jeter'
                    row['reason']='Doublons exacts à l’indexation ; proposition de conserver : '+str(group[0]['photo'].path)
        self.rows=sorted(rows,key=lambda row:(row['photo'].sha256,str(row['photo'].path)))
        self.route_cache={}
        workspace=self.workspace()
        if workspace:
            try:
                saved,destination=read_review(workspace/'tri-source.json')
                for row in self.rows:
                    item=saved.get((str(row['photo'].path),row['photo'].sha256))
                    if item:
                        for field in ('decision','proposal','reason'):
                            row[field]=item[field]
                        row['manifest']=Path(item['manifest']) if item.get('manifest') else None
                if destination:
                    self.destination=Path(destination)
                    self.destination_text.set(destination)
            except (OSError,ValueError,KeyError,TypeError) as error:
                self.status.set(f'Journal non chargé : {error}')
                return
        self.offset=0
        if getattr(self,'format_select',None) is not None:
            formats=('Tous les formats',*sorted({photo_format(row['photo']) for row in self.rows}))
            self.format_select.configure(values=formats)
            if self.format_var.get() not in formats: self.format_var.set('Tous les formats')
        self.render()
        if getattr(self,'index_assistant',None) is not None:
            self.index_assistant.reload()

    def apply_agent_proposals(self,questions):
        known={(str(row['photo'].path),row['photo'].sha256):row for row in self.rows}
        for question in questions:
            photo=question['photo']
            row=known.get((str(photo.path),photo.sha256))
            if row is not None:
                row['proposal']=question['proposal']['decision']
                row['reason']='IA : '+question['proposal']['observation']+' · tags proposés : '+', '.join(question['proposal']['tags'])
                if question['answer'] is not None:
                    row['decision']=question['answer']['decision']
                    target=(str(self.destination) if self.destination else 'Choisir À jeter') if row['decision']=='a_jeter' else (
                        question['destination'] or ('Choisir une destination' if len(question['destinations'])>1 else 'Reste dans la source'))
                    self.route_cache[str(photo.path)]=target
        self.render()
        self.save()

    def apply_agent_answer(self,photo,decision,tags,note):
        for row in self.rows:
            if (row['photo'].path,row['photo'].sha256)==(photo.path,photo.sha256):
                row['decision']=decision
                row['reason']='Réponse validée : '+', '.join(tags)+' · '+note
        self.render()
        self.save()

    def render(self):
        self.tree.delete(*self.tree.get_children())
        self.visible_indices=[index for index,row in enumerate(self.rows)
            if (self.format_var.get()=='Tous les formats' or photo_format(row['photo'])==self.format_var.get())
            and (not self.only_related.get() or len(self.related(row['photo']))>1)]
        for index in self.visible_indices[self.offset:self.offset+200]:
            row = self.rows[index]
            self.tree.insert('','end',iid=str(index),values=(
                'Extrait' if row['manifest'] else LABELS[row['decision']],LABELS[row['proposal']],
                self.route_cache.get(str(row['photo'].path),'Tags à valider'),str(row['photo'].path)))
        counts = {code:sum(row['decision']==code and not row['manifest'] for row in self.rows) for code in LABELS}
        self.status.set(f'{len(self.rows)} photos · garder : {counts["garder"]} · à jeter validés : {counts["a_jeter"]} · '
                        f'à revoir : {counts["a_revoir"]} · page {self.offset//200+1}')

    def page(self,delta):
        if self.idle():
            self.offset = max(0,min(self.offset+200*delta,max(0,((len(self.visible_indices)-1)//200)*200)))
            self.render()

    def filter_changed(self):
        if self.idle():
            self.offset=0
            self.render()

    def selected(self):
        return [int(value) for value in self.tree.selection() if not self.rows[int(value)]['manifest']]

    def show_details(self,_event=None):
        selected = self.tree.selection()
        if selected:
            row = self.rows[int(selected[0])]
            self.details.set(f'{row["photo"].path}\n{row["reason"]}'
                             +(f'\nJournal : {row["manifest"]}' if row['manifest'] else ''))

    def preview(self,_event=None):
        selected = self.tree.selection()
        if selected:
            self.work_tabs.select(self.index_assistant)
            self.index_assistant.focus_photo(self.rows[int(selected[0])]['photo'])

    def decide(self,code):
        if self.idle():
            try:
                for index in self.selected():
                    self.index_assistant.sync_decision(self.rows[index]['photo'],code)
                    self.rows[index]['decision']=code
            except (OSError,ValueError,sqlite3.Error) as error:
                self.render()
                self.save()
                self.status.set(f'Décision non enregistrée : {error}')
                return
            self.render()
            self.save()

    def manual_review(self,photo,decision,include_exact=False):
        if not self.idle(): return False
        try:
            targets=manual_targets(self.rows,photo,include_exact)
        except ValueError as error:
            self.status.set(str(error))
            return False
        if not targets:
            self.status.set('Cette photo n’est plus disponible dans les sources indexées.')
            return False
        if include_exact and decision=='a_jeter':
            paths='\n'.join(str(row['photo'].path) for row in targets[:8])
            if not messagebox.askyesno('Écarter le groupe entier',
                    f'Marquer ces {len(targets)} exemplaire(s) identique(s) À jeter ?\n\n{paths}\n\n'
                    'Tu choisis de ne garder aucun exemplaire de ce groupe dans la source.\n'
                    'Ils seront extraits vers À jeter uniquement lors de l’application du plan. Aucune suppression.',parent=self):
                return False
        try:
            for row in targets:
                self.index_assistant.sync_decision(row['photo'],decision)
                row['decision']=decision
                row['reason']='Décision manuelle'+(' — groupe exact entier' if include_exact else '')
        except (OSError,ValueError,sqlite3.Error) as error:
            self.render()
            self.save()
            self.status.set(f'Décisions partiellement enregistrées, relire la liste : {error}')
            return False
        self.render()
        if not self.save(): return False
        self.status.set(f'{len(targets)} photo(s) marquée(s) {LABELS[decision]}. Aucun fichier déplacé avant application.')
        return True

    def review_group(self,photos,keepers,allow_empty=False):
        if not self.idle(): return False
        try:
            keep,reject=partition_group(self.rows,photos,keepers,allow_empty=allow_empty)
        except ValueError as error:
            self.status.set(str(error))
            return False
        def listing(rows):
            names='\n'.join(str(row['photo'].path) for row in rows[:5]) or 'Aucune'
            return names+('\n…' if len(rows)>5 else '')
        text=(f'À garder : {len(keep)} fichier(s)\n{listing(keep)}\n\n'
              f'À jeter : {len(reject)} fichier(s)\n{listing(reject)}\n\n'
              'Seules les décisions sont enregistrées. Les rejets seront extraits vers À jeter à l’application du plan. Aucune suppression.')
        if not messagebox.askyesno('Confirmer la répartition du groupe',text,parent=self): return False
        try:
            for rows,decision in ((keep,'garder'),(reject,'a_jeter')):
                for row in rows:
                    self.index_assistant.sync_decision(row['photo'],decision)
                    row['decision']=decision
                    row['reason']='Comparaison manuelle du groupe : '+LABELS[decision]
        except (OSError,ValueError,sqlite3.Error) as error:
            self.render(); self.save()
            self.status.set(f'Répartition partiellement enregistrée ; relire la liste : {error}')
            return False
        self.render()
        if not self.save(): return False
        self.status.set(f'Groupe validé : {len(keep)} à garder, {len(reject)} À jeter. Aucun fichier déplacé avant application.')
        return True

    def accept(self):
        if self.idle():
            try:
                for index in self.selected():
                    self.index_assistant.sync_decision(self.rows[index]['photo'],self.rows[index]['proposal'])
                    self.rows[index]['decision']=self.rows[index]['proposal']
            except (OSError,ValueError,sqlite3.Error) as error:
                self.render()
                self.save()
                self.status.set(f'Décision non enregistrée : {error}')
                return
            self.render()
            self.save()

    def propose_ai(self):
        if not self.idle():
            return
        selected=self.selected()
        if not 1<=len(selected)<=6:
            self.status.set('Sélectionne entre une et six photos à comparer ensemble avec l’IA.')
            return
        photos=[self.rows[index]['photo'] for index in selected]
        model,preferences=self.model.get(),self.preferences.get()
        self.status.set('L’IA examine une planche de miniatures ; aucune décision n’est appliquée…')
        def done(results):
            for index,(code,reason) in zip(selected,results):
                self.rows[index]['proposal']=code
                self.rows[index]['reason']='Proposition IA : '+reason
            self.render()
            self.save()
        self.run(lambda gate:suggest(self.client,model,photos,preferences,gate),done)

    def choose_destination(self):
        if not self.idle():
            return
        path=askdirectory(parent=self,title='Choisir ou créer le dossier unique À jeter')
        if path:
            self.destination=Path(path).absolute()
            self.destination_text.set(str(self.destination))
            self.save()

    def apply(self):
        if not self.idle():
            return
        rows=[row for row in self.rows if not row['manifest']]
        if not rows:
            self.status.set('Aucune photo à classer.')
            return
        workspace=self.workspace()
        context=self.index_assistant.context or context_key(self.model.get(),self.preferences.get())
        destination=self.destination
        if workspace is None:
            self.status.set('Choisir le dossier de travail avant le classement.')
            return
        if not self.save(): return
        self.status.set('Préparation du plan à partir des tags validés…')
        def prepare(gate):
            with ReviewStore(workspace/'assistant-tri.sqlite3') as store:
                plan=plan_routes(store,context,[row['photo'] for row in rows],destination,gate)
            plan=with_manual_rejections(plan,rows,destination)
            actions=[]
            for route,row in zip(plan,rows):
                gate.check()
                if route.destination is not None:
                    target=checked_stat(route.destination,directory=True)
                    source=checked_stat(route.photo.path)
                    if target.st_dev!=source.st_dev:
                        raise OSError('Source et dossier cible doivent être sur le même disque : '+str(route.photo.path))
                    actions.append((row,route.destination))
            return plan,actions
        def reviewed(value):
            plan,actions=value
            self.route_cache={str(route.photo.path):str(route.destination) if route.destination else route.status for route in plan}
            self.render()
            if not actions:
                self.status.set('Aucun déplacement : valider les tags, associer leurs dossiers ou résoudre les destinations en attente.')
                return
            preview='\n'.join(str(row['photo'].path)+' → '+str(target) for row,target in actions[:8])
            if len(actions)>8: preview+='\n…'
            if not messagebox.askyesno('Confirmer le classement validé',
                    f'Déplacer {len(actions)} photo(s) selon tes décisions et tags validés ?\n\n{preview}\n\n'
                    'Les autres fichiers restent dans la source. Aucune suppression ni écrasement.',parent=self):
                self.status.set('Plan préparé, aucune modification appliquée.')
                return
            def execute(gate):
                outcomes=[]
                for row,target in actions:
                    try:
                        gate.check()
                        photo=row['photo']
                        item=Fingerprint(photo.path,row['root'],photo.size,photo.sha256,file_evidence(photo.path))
                        outcomes.append((row,extract(item,target,gate),None))
                    except Exception as error:
                        outcomes.append((row,None,str(error)))
                        break
                return outcomes
            def finished(outcomes):
                errors=[]
                for row,manifest,error in outcomes:
                    row['manifest']=manifest
                    if error: errors.append(error)
                    if manifest:
                        try:
                            record=json.loads(manifest.read_text(encoding='utf-8'))
                            current=manifest.parent.parent/record['target']
                            with ReviewStore(workspace/'assistant-tri.sqlite3') as store:
                                store.record_location(row['photo'],current,manifest)
                        except (OSError,ValueError,sqlite3.Error) as error:
                            errors.append(f'Classement effectué, métadonnées à réconcilier depuis {manifest} : {error}')
                self.changed()
                self.render()
                self.save()
                self.status.set(f'{sum(bool(manifest) for _,manifest,_ in outcomes)} photo(s) classée(s). '
                                + ('Arrêt : '+'; '.join(errors) if errors else 'Réindexer pour poursuivre. Restauration disponible.'))
            self.run(execute,finished)
        self.run(prepare,reviewed)

    def restore_file(self):
        if not self.idle():
            return
        selected=self.tree.selection()
        manifest=self.rows[int(selected[0])]['manifest'] if selected else None
        if manifest is None:
            value=askopenfilename(parent=self,title='Choisir un journal d’extraction',
                                  initialdir=str(self.destination/JOURNAL) if self.destination else None,
                                  filetypes=[('Journal JSON','*.json')])
            if not value: return
            manifest=Path(value)
        if not messagebox.askyesno('Restaurer une photo',f'Restaurer sans écrasement depuis le journal :\n{manifest} ?',parent=self):
            return
        def done(path):
            metadata_error=None
            workspace=self.workspace()
            if workspace is not None:
                try:
                    record=json.loads(manifest.read_text(encoding='utf-8'))
                    restored_photo=Photo(path,record['size'],0,record['sha256'])
                    with ReviewStore(workspace/'assistant-tri.sqlite3') as store:
                        store.record_location(restored_photo,path)
                except (OSError,ValueError,sqlite3.Error) as error:
                    metadata_error=str(error)
            for row in self.rows:
                if row['manifest']==manifest:
                    row['manifest']=None
                    row['decision']='a_revoir'
                    row['proposal']='a_revoir'
                    row['reason']='Photo restaurée ; à examiner de nouveau.'
            self.changed()
            self.render()
            self.save()
            self.status.set(f'Restauré : {path}. Réindexe la source avant de poursuivre.'
                            +(f' Métadonnées à vérifier : {metadata_error}' if metadata_error else ''))
        self.run(lambda gate:restore_extraction(manifest,gate),done)
