"""Onglet documentaire : modèle partagé, coordonnées natives, travail IA asynchrone."""
from copy import deepcopy
import json
import logging
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from threading import Event
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog
from PIL import ImageTk, ImageDraw
import tempfile
from .model import FIELDS, LAYERS, SKILL, Store, Editing, document, geometry, validate_analysis
from .imaging import preview, operation, overlay, TOOLS
from ..ollama import DEFAULT_MODEL


class DocumentaryPane(ttk.Frame):
    def __init__(self, parent, services):
        super().__init__(parent)
        self.services = services
        self.store = Store(services.documents.database)
        self.edit = None
        self.image = self.original = None
        self.overlay_image = None
        self.show_original = False
        self.scale, self.offset = 1., [0., 0.]
        self.selected = None
        self.points = []
        self.drag = None
        self.pool = ThreadPoolExecutor(max_workers=1)
        self.pending = None
        self.cancel = Event()
        self.mode = tk.StringVar(self, 'selection')
        self.category = tk.StringVar(self, 'cadastre')
        self.status = tk.StringVar(self, 'Ouvrir une image ou reprendre un document enregistré.')
        bar = ttk.Frame(self); bar.pack(fill='x')
        for label, action in [('Ouvrir', self.open), ('Reprendre', self.resume), ('Enregistrer', self.save),
                              ('Annuler', self.undo), ('Rétablir', self.redo), ('Terminer tracé', self.finish),
                              ('Supprimer', self.delete)]:
            ttk.Button(bar, text=label, command=action).pack(side='left')
        controls = ttk.Frame(self); controls.pack(fill='x')
        ttk.Combobox(controls, textvariable=self.mode, state='readonly', width=12,
                     values=('selection', 'deplacement', 'line', 'polyline', 'polygon')).pack(side='left')
        ttk.Combobox(controls, textvariable=self.category, state='readonly', width=12,
                     values=tuple(LAYERS)).pack(side='left')
        self.tool = ttk.Combobox(controls, values=tuple(x for x in TOOLS if x != 'rotation'), state='readonly', width=14)
        self.tool.set('contraste'); self.tool.pack(side='left')
        self.factor = ttk.Entry(controls, width=5); self.factor.insert(0, '1.5'); self.factor.pack(side='left')
        ttk.Button(controls, text='Appliquer', command=self.transform).pack(side='left')
        ttk.Button(controls, text='Original / résultat', command=self.compare_original).pack(side='left')
        panes = ttk.Panedwindow(self, orient='horizontal'); panes.pack(fill='both', expand=True)
        self.canvas = tk.Canvas(panes, background='#17252d', highlightthickness=0)
        panes.add(self.canvas, weight=3)
        side = ttk.Notebook(panes); panes.add(side, weight=1)
        fields = ttk.Frame(side); side.add(fields, text='Fiche')
        scroll = tk.Canvas(fields, width=320); scroll.pack(side='left', fill='both', expand=True)
        sb = ttk.Scrollbar(fields, command=scroll.yview); sb.pack(side='right', fill='y'); scroll.configure(yscrollcommand=sb.set)
        body = ttk.Frame(scroll); scroll.create_window(0, 0, anchor='nw', window=body)
        body.bind('<Configure>', lambda e: scroll.configure(scrollregion=scroll.bbox('all')))
        self.fields = {}
        for name in FIELDS:
            ttk.Label(body, text=name).pack(anchor='w')
            entry = ttk.Entry(body, width=39); entry.pack(fill='x')
            entry.bind('<FocusOut>', lambda e, n=name: self.field_change(n))
            self.fields[name] = entry
        self.field_name = ttk.Combobox(body, values=FIELDS, state='readonly', width=35)
        self.field_name.set(FIELDS[0]); self.field_name.pack(fill='x')
        ttk.Button(body, text='Justification du champ', command=self.justification).pack(fill='x')
        ttk.Button(body, text='Valider ce champ', command=self.validate_field).pack(fill='x')
        ttk.Button(body, text='Lier le champ à l’objet sélectionné', command=self.link_region).pack(fill='x')
        ttk.Button(body, text='Afficher la région du champ', command=self.show_region).pack(fill='x')
        self.objects = ttk.Frame(side); side.add(self.objects, text='Objets')
        self.list = tk.Listbox(self.objects, height=9, exportselection=False); self.list.pack(fill='x')
        self.list.bind('<<ListboxSelect>>', self.list_select)
        self.properties = {}
        for name in ('number', 'section', 'locality', 'comment', 'transcription', 'width'):
            ttk.Label(self.objects, text={'number':'Numéro', 'section':'Section', 'locality':'Localité',
                'comment':'Description / commentaire', 'transcription':'Inscription liée', 'width':'Épaisseur'}[name]).pack(anchor='w')
            entry = ttk.Entry(self.objects); entry.pack(fill='x'); self.properties[name] = entry
        ttk.Button(self.objects, text='Appliquer la correction', command=self.object_change).pack(fill='x')
        ttk.Button(self.objects, text='Valider comme exemple', command=lambda: self.object_change(True)).pack(fill='x')
        ttk.Button(self.objects, text='Ajouter un sommet au centre', command=self.add_vertex).pack(fill='x')
        ttk.Button(self.objects, text='Retirer le dernier sommet', command=self.remove_vertex).pack(fill='x')
        self.visible = {}
        for name in LAYERS:
            var = tk.BooleanVar(self, True); self.visible[name] = var
            ttk.Checkbutton(self.objects, text=name, variable=var, command=self.visibility).pack(anchor='w')
        comparison = ttk.Frame(side); side.add(comparison, text='Comparer')
        ttk.Button(comparison, text='Ouvrir une seconde carte', command=self.open_overlay).pack(fill='x')
        self.alignment = {}
        for name, value in [('opacity', .5), ('x', 0), ('y', 0), ('scale', 1), ('angle', 0)]:
            ttk.Label(comparison, text=name).pack(anchor='w')
            entry=ttk.Entry(comparison); entry.insert(0,str(value)); entry.pack(fill='x')
            self.alignment[name]=entry
        ttk.Button(comparison, text='Appliquer / enregistrer', command=self.apply_overlay).pack(fill='x')
        ttk.Button(comparison, text='Masquer la seconde carte', command=self.hide_overlay).pack(fill='x')
        ai = ttk.Frame(self); ai.pack(fill='x')
        self.model = ttk.Entry(ai, width=25); self.model.insert(0, DEFAULT_MODEL); self.model.pack(side='left')
        ttk.Button(ai, text='Identifier avec Qwen', command=self.analyse).pack(side='left')
        ttk.Button(ai, text='Arrêter', command=self.stop).pack(side='left')
        ttk.Label(self, textvariable=self.status, wraplength=1000).pack(fill='x')
        self.canvas.bind('<Button-1>', self.press)
        self.canvas.bind('<B1-Motion>', self.motion)
        self.canvas.bind('<ButtonRelease-1>', self.release)
        self.canvas.bind('<MouseWheel>', self.zoom)
        self.canvas.bind('<Button-4>', lambda e: self.zoom(e, 1.15))
        self.canvas.bind('<Button-5>', lambda e: self.zoom(e, 1/1.15))
        self.canvas.bind('<Configure>', lambda e: self.draw())

    @property
    def busy(self): return self.pending is not None

    def stop(self): self.cancel.set()

    def task(self, function, success):
        if self.busy: return
        self.cancel = Event()
        self.pending = self.pool.submit(function)
        def poll():
            if not self.pending.done(): self.after(80, poll); return
            future, self.pending = self.pending, None
            try: success(future.result())
            except Exception as error:
                logging.exception('Analyse documentaire')
                self.status.set(str(error))
        self.after(80, poll)

    def open(self):
        if self.busy: return
        path = filedialog.askopenfilename(parent=self, title='Image documentaire')
        if path:
            self.save()
            self.status.set('Chargement…')
            self.task(lambda: (document(path), preview(path)), self.loaded)

    def loaded(self, result):
        data, (image, size) = result
        self.edit = Editing(data); self.original = image; self.native_size = size
        self.image = image
        self.overlay_image = None
        self.show_original = False
        for command in data['transforms']: self.image = operation(self.image, command)
        self.scale = min(1, 700/size[0], 600/size[1]); self.offset = [0., 0.]
        self.selected = None; self.points = []
        for name,entry in self.alignment.items():
            if name in data['comparison']:
                entry.delete(0,'end'); entry.insert(0,str(data['comparison'][name]))
        self.refresh(); self.save(); self.status.set('Document prêt. Tracés en coordonnées de l’original.')

    def resume(self):
        if self.busy: return
        listing = self.store.listing()
        if not listing: return
        value = simpledialog.askinteger('Reprendre', '\n'.join(f'{i+1}. {Path(p).name}' for i, (_,p) in enumerate(listing)),
                                        parent=self, minvalue=1, maxvalue=len(listing))
        if value:
            self.save(); data = self.store.load(listing[value-1][0])
            def load():
                if document(data['path'])['sha256'] != data['sha256']:
                    raise ValueError('L’image originale a changé ; reprise refusée pour préserver les coordonnées.')
                return data, preview(data['path'])
            self.task(load, self.loaded)

    def save(self):
        if self.edit: self.store.save(self.edit.data)

    def refresh(self):
        if not self.edit: return
        for name, entry in self.fields.items():
            entry.delete(0, 'end'); entry.insert(0, self.edit.data['metadata'][name]['value'])
        self.list.delete(0, 'end')
        for obj in self.edit.data['objects']:
            self.list.insert('end', f"{obj['number'] or obj['id'][:6]} · {obj['category']} · {obj['status']}")
        for name, var in self.visible.items(): var.set(name not in self.edit.data['hidden'])
        self.draw()

    def field_change(self, name):
        if not self.edit: return
        field = self.edit.data['metadata'][name]; value = self.fields[name].get()
        if value == field['value']: return
        before = deepcopy(field)
        self.edit.change('Correction champ', lambda d: d['metadata'][name].update(value=value, status='corrige'))
        self.edit.correct(name, before, self.edit.data['metadata'][name]); self.save()

    def xy(self, x, y): return [(x-self.offset[0])/self.scale, (y-self.offset[1])/self.scale]
    def screen(self, p): return [p[0]*self.scale+self.offset[0], p[1]*self.scale+self.offset[1]]
    def object(self):
        return next((o for o in self.edit.data['objects'] if o['id'] == self.selected), None) if self.edit else None

    def draw(self):
        self.canvas.delete('all')
        if self.image is None: return
        w, h = self.native_size; sw, sh = max(1, int(w*self.scale)), max(1, int(h*self.scale))
        # Recadrage de la prévisualisation à la fenêtre : pas de bitmap géant au zoom.
        left = max(0, -self.offset[0]); top = max(0, -self.offset[1])
        right = min(sw, self.canvas.winfo_width()-self.offset[0]); bottom = min(sh, self.canvas.winfo_height()-self.offset[1])
        if right > left and bottom > top:
            pw, ph = self.image.size
            crop = self.image.crop((int(left/sw*pw), int(top/sh*ph), max(1,int(right/sw*pw)), max(1,int(bottom/sh*ph))))
            self.picture = ImageTk.PhotoImage(crop.resize((max(1,int(right-left)), max(1,int(bottom-top)))))
            self.canvas.create_image(self.offset[0]+left, self.offset[1]+top, image=self.picture, anchor='nw')
        for obj in self.edit.data['objects']:
            if obj['category'] in self.edit.data['hidden']: continue
            points = [v for p in obj['points'] for v in self.screen(p)]
            color = self.edit.data['layers'].get(obj['category'], 'white')
            if obj['kind'] == 'polygon':
                self.canvas.create_polygon(*points, fill='', outline=color, width=obj['width'], tags=(obj['id'],))
            else: self.canvas.create_line(*points, fill=color, width=obj['width'], tags=(obj['id'],))
            self.canvas.create_text(*points[:2], text=obj['number'], fill=color, anchor='sw', tags=(obj['id'],))
            if obj['id'] == self.selected:
                for i, p in enumerate(obj['points']):
                    x,y = self.screen(p)
                    self.canvas.create_rectangle(x-4,y-4,x+4,y+4, fill='white', tags=(obj['id'],f'vertex:{i}'))
        if len(self.points) > 1:
            self.canvas.create_line(*[v for p in self.points for v in self.screen(p)], fill='white', dash=(4,4))

    def press(self, e):
        if not self.edit: return
        mode = self.mode.get()
        if mode in ('line','polyline','polygon'):
            p = self.xy(e.x,e.y)
            if 0 <= p[0] <= self.native_size[0] and 0 <= p[1] <= self.native_size[1]: self.points.append(p)
            if mode == 'line' and len(self.points)==2: self.finish()
            self.draw(); return
        if mode == 'deplacement': self.drag = ('pan', e.x, e.y); return
        hits = self.canvas.find_overlapping(e.x-5,e.y-5,e.x+5,e.y+5)
        tags = self.canvas.gettags(hits[-1]) if hits else ()
        self.selected = tags[0] if tags else None
        obj = self.object()
        if obj:
            vertex = int(tags[1].split(':')[1]) if len(tags)>1 and tags[1].startswith('vertex:') else None
            self.drag = ('object', self.xy(e.x,e.y), deepcopy(obj), vertex)
            for name, entry in self.properties.items(): entry.delete(0,'end'); entry.insert(0,str(obj[name]))
            index = self.edit.data['objects'].index(obj); self.list.selection_clear(0,'end'); self.list.selection_set(index)
        self.draw()

    def motion(self, e):
        if not self.drag: return
        if self.drag[0] == 'pan':
            _,x,y = self.drag; self.offset[0] += e.x-x; self.offset[1] += e.y-y; self.drag=('pan',e.x,e.y)
        else:
            _, start, before, vertex = self.drag; p = self.xy(e.x,e.y); obj = self.object()
            obj['points'] = deepcopy(before['points'])
            if vertex is not None: obj['points'][vertex] = p
            else: obj['points'] = [[x+p[0]-start[0],y+p[1]-start[1]] for x,y in before['points']]
        self.draw()

    def release(self, e):
        if self.drag and self.drag[0] == 'object':
            before = self.drag[2]; after = deepcopy(self.object())
            if after != before:
                self.object().update(before)
                self.edit.change('Modifier géométrie', lambda d: self.object().update(after))
                self.edit.correct(after['id'], before, after, after['category']); self.save()
        self.drag = None

    def finish(self):
        if not self.edit: return
        try: obj = geometry(self.mode.get(), self.points, self.category.get())
        except ValueError as error: self.status.set(str(error)); return
        self.edit.change('Créer objet', lambda d: d['objects'].append(obj)); self.points=[]
        self.selected=obj['id']; self.save(); self.refresh()

    def list_select(self, e):
        if self.list.curselection():
            obj=self.edit.data['objects'][self.list.curselection()[0]]; self.selected=obj['id']
            for name,entry in self.properties.items(): entry.delete(0,'end'); entry.insert(0,str(obj[name]))
            self.draw()

    def object_change(self, validated=False):
        obj=self.object()
        if not obj: return
        values={name:entry.get() for name,entry in self.properties.items()}
        try:
            values['width']=float(values['width'])
            if not 0.5 <= values['width'] <= 20: raise ValueError()
        except ValueError: self.status.set('Épaisseur attendue entre 0,5 et 20.'); return
        before=deepcopy(obj); values['status']='valide' if validated else 'corrige'
        self.edit.change('Correction objet', lambda d: self.object().update(values))
        self.edit.correct(obj['id'],before,self.object(),obj['category'],validated); self.save(); self.refresh()

    def add_vertex(self):
        obj=self.object()
        if obj and obj['kind'] != 'line':
            a,b=obj['points'][-2:]; p=[(a[0]+b[0])/2,(a[1]+b[1])/2]
            self.edit.change('Ajouter sommet', lambda d: self.object()['points'].insert(-1,p)); self.save(); self.draw()

    def remove_vertex(self):
        obj=self.object()
        if obj and len(obj['points']) > (3 if obj['kind']=='polygon' else 2):
            self.edit.change('Retirer sommet', lambda d: self.object()['points'].pop()); self.save(); self.draw()

    def delete(self):
        if self.object():
            self.edit.change('Supprimer objet', lambda d: d['objects'].remove(self.object()))
            self.selected=None; self.save(); self.refresh()

    def visibility(self):
        if self.edit:
            self.edit.data['hidden']=[n for n,v in self.visible.items() if not v.get()]; self.save(); self.draw()

    def zoom(self,e,factor=None):
        if not self.edit: return
        p=self.xy(e.x,e.y); factor=factor or (1.15 if e.delta>0 else 1/1.15)
        self.scale=max(.005,min(8,self.scale*factor))
        self.offset=[e.x-p[0]*self.scale,e.y-p[1]*self.scale]; self.draw()

    def rebuild(self):
        if self.original is None: return
        self.image=self.original
        for command in self.edit.data['transforms']: self.image=operation(self.image,command)
        self.refresh(); self.save()
    def undo(self):
        if self.edit: self.edit.undo(); self.rebuild()
    def redo(self):
        if self.edit: self.edit.redo(); self.rebuild()
    def transform(self):
        if not self.edit: return
        try:
            command={'tool':self.tool.get(),'value':float(self.factor.get())}
            image=operation(self.original if self.show_original else self.image,command)
            self.show_original=False
            self.edit.change('Transformation',lambda d:d['transforms'].append(command)); self.image=image
            self.save(); self.draw()
        except ValueError as error: self.status.set(str(error))
    def compare_original(self):
        if self.edit:
            self.show_original = not self.show_original
            if self.show_original: self.image=self.original; self.draw()
            else: self.rebuild()

    def justification(self):
        if not self.edit: return
        name=self.field_name.get(); field=self.edit.data['metadata'][name]
        value=simpledialog.askstring(name, 'Provenance : '+field['status']+' — justification :',
                                     parent=self,initialvalue=field.get('justification',''))
        if value is not None:
            self.edit.change('Justification',lambda d:d['metadata'][name].update(justification=value))
            self.save()

    def validate_field(self):
        if not self.edit: return
        name=self.field_name.get(); self.field_change(name)
        before=deepcopy(self.edit.data['metadata'][name])
        self.edit.change('Validation champ',lambda d:d['metadata'][name].update(status='valide'))
        self.edit.correct(name,before,self.edit.data['metadata'][name], 'metadata', True); self.save()

    def link_region(self):
        if self.object():
            name=self.field_name.get(); points=deepcopy(self.object()['points'])
            self.edit.change('Région du champ',lambda d:d['metadata'][name].update(region=points)); self.save()

    def show_region(self):
        if not self.edit: return
        region=self.edit.data['metadata'][self.field_name.get()].get('region')
        if region:
            self.offset=[self.canvas.winfo_width()/2-sum(p[0] for p in region)/len(region)*self.scale,
                         self.canvas.winfo_height()/2-sum(p[1] for p in region)/len(region)*self.scale]
            self.draw()
            self.canvas.create_polygon(*[v for p in region for v in self.screen(p)],fill='',outline='white',width=3)

    def open_overlay(self):
        if not self.edit or self.busy: return
        path=filedialog.askopenfilename(parent=self,title='Seconde carte')
        if path:
            def done(result):
                self.overlay_image=result[0]
                self.edit.data['comparison']['path']=str(Path(path).resolve())
                self.apply_overlay()
            self.task(lambda:preview(path),done)

    def apply_overlay(self):
        if not self.edit: return
        config=self.edit.data['comparison']
        if self.overlay_image is None and config.get('path'):
            try: self.overlay_image=preview(config['path'])[0]
            except OSError as error: self.status.set(str(error)); return
        if self.overlay_image is None: return
        try:
            values={k:float(v.get()) for k,v in self.alignment.items()}
            image=overlay(self.original,self.overlay_image,**values)
            self.edit.change('Alignement de cartes',lambda d:d['comparison'].update(values))
            self.image=image; self.save(); self.draw()
        except ValueError as error: self.status.set(str(error))

    def hide_overlay(self): self.rebuild()


    def analyse(self):
        if not self.edit or self.busy: return
        identifier=self.edit.data['id']; snapshot=deepcopy(self.edit.data['metadata'])
        path=Path(self.edit.data['path']); model=self.model.get()
        examples=self.store.examples(limit=2)
        analysis_image=self.image.copy()
        draw=ImageDraw.Draw(analysis_image)
        for obj in self.edit.data['objects']:
            if obj['exclusion']=='ignore' and obj['kind']=='polygon':
                draw.polygon([(x/self.native_size[0]*analysis_image.width,
                               y/self.native_size[1]*analysis_image.height) for x,y in obj['points']],fill='white')
        prompt=SKILL['instructions']+' Renvoyer un objet JSON avec seulement les champs lisibles parmi : '+', '.join(FIELDS)
        prompt+=' Chaque champ contient value (texte), status (visible ou hypothese), justification (texte). '
        prompt+='Exemples humains validés (contexte uniquement): '+json.dumps(examples,ensure_ascii=False)[:2000]
        self.status.set('Analyse Qwen en cours ; les objets restent éditables.')
        def work():
            with tempfile.TemporaryDirectory(prefix='multitools-doc-') as folder:
                view=Path(folder)/'analyse.png'; analysis_image.save(view)
                result=self.services.ollama.chat(model,[{'role':'user','content':prompt}],lambda text:None,self.cancel,
                                                 image=view,schema={'type':'object'})
            return validate_analysis(json.loads(result.text))
        def done(data):
            if self.cancel.is_set() or self.edit.data['id']!=identifier: return
            for name in FIELDS: self.field_change(name)
            def apply(d):
                for name,value in data.items():
                    if d['metadata'][name]==snapshot[name] and snapshot[name]['status'] not in ('corrige','valide'):
                        d['metadata'][name]={**value,'source':'qwen','region':None}
            self.edit.change('Propositions Qwen',apply); self.save(); self.refresh()
            self.status.set('Propositions Qwen enregistrées ; vérifier les informations historiques.')
        self.task(work,done)
