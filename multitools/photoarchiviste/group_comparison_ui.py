"""Vue d'ensemble de toutes les variantes et choix explicites de conservation."""
from collections import OrderedDict
import tkinter as tk
from tkinter import ttk

from PIL import Image, ImageTk

from .adaptive_panel import AdaptivePanel
from .comparison_group import highest_resolution
from .preview import _thumbnail


class GroupComparison(ttk.Frame):
    THUMB_SIZE=(170,105)

    def __init__(self,parent,executor,on_focus,on_detail,on_apply,can_edit):
        super().__init__(parent)
        self.executor,self.on_focus,self.on_detail=executor,on_focus,on_detail
        self.on_apply,self.can_edit=on_apply,can_edit
        self.photos=()
        self.signature=()
        self.states={}
        self.checked=set()
        self.focus=None
        self.dirty=False
        self.cards={}
        self.pending={}
        self.failed=set()
        self.cache=OrderedDict()
        self.timer=None
        self.loading=None
        self.columns=0
        self.layout=None
        self.columnconfigure(0,weight=1)
        self.rowconfigure(2,weight=1)
        self.title=tk.StringVar(self)
        self.count=tk.StringVar(self)
        title=ttk.Label(self,textvariable=self.title,wraplength=550,width=1,justify='left')
        title.grid(row=0,column=0,sticky='ew',pady=(0,4))
        self.bind('<Configure>',lambda event:title.configure(wraplength=max(240,event.width-12)))
        controls=ttk.Frame(self)
        controls.grid(row=1,column=0,sticky='ew',pady=(0,5))
        self.all_button=ttk.Button(controls,text='Tout garder',command=self.keep_all)
        self.reject_button=ttk.Button(controls,text='Tout rejeter…',command=lambda:self.apply(True))
        self.apply_button=ttk.Button(controls,text='Valider cette répartition…',command=self.apply)
        def layout(event):
            mode=(event.width if event is not None else 400)>=600
            if self.layout==mode: return
            self.layout=mode
            self.all_button.grid(row=0,column=0,sticky='w',padx=(0,4))
            self.reject_button.grid(row=0,column=1,sticky='w',padx=(0,4))
            self.apply_button.grid(row=0 if mode else 1,column=2 if mode else 0,
                                   columnspan=1 if mode else 2,sticky='w',pady=2)
        controls.bind('<Configure>',layout)
        layout(None)
        self.panel=AdaptivePanel(self)
        self.panel.grid(row=2,column=0,sticky='nsew')
        self.panel.canvas.configure(yscrollcommand=self.scrolled)
        self.panel.canvas.bind('<Configure>',self.resize,add='+')
        ttk.Label(self,textvariable=self.count,wraplength=550).grid(row=3,column=0,sticky='ew',pady=(4,0))
        self.placeholder=ImageTk.PhotoImage(Image.new('RGB',self.THUMB_SIZE,'#17252d'),master=self)
        self.bind('<Destroy>',self.cleanup,add='+')
        self.bind('<Map>',lambda _:self.queue_loading(),add='+')

    def set_group(self,photos,states,focus):
        signature=tuple((p.path,p.sha256,p.size,p.width,p.height,p.format,p.mtime_ns) for p in photos)
        self.states=states
        self.focus=focus
        if signature==self.signature:
            if not self.dirty: self.checked={p.path for p in photos if states.get(p.path)=='garder'}
            self.refresh_labels()
            self.queue_loading()
            return
        self.cancel_loading()
        self.signature,self.photos=signature,tuple(photos)
        self.checked={p.path for p in photos if states.get(p.path)=='garder'}
        self.dirty=False
        self.cache.clear()
        self.failed.clear()
        for card in self.cards.values(): card['frame'].destroy()
        self.cards={}
        exact=len({p.sha256 for p in photos})==1
        self.title.set(f'{len(photos)} fichiers · '+('Contenus identiques : qualité égale.' if exact else
            'Photos proches : contenus différents. La résolution aide à comparer, sans garantir la qualité.'))
        maximum=highest_resolution(photos)
        for photo in photos:
            card=ttk.Frame(self.panel.content,padding=8,relief='solid',borderwidth=1)
            flag=tk.BooleanVar(self,photo.path in self.checked)
            image=ttk.Button(card,image=self.placeholder,command=lambda photo=photo:self.on_focus(photo))
            image.pack()
            name=photo.path.name
            ttk.Label(card,text=name if len(name)<=30 else name[:22]+'…'+name[-5:],font=('TkDefaultFont',9,'bold')).pack(anchor='w',pady=(4,0))
            text=f'{photo.width} × {photo.height} · {photo.width*photo.height/1_000_000:.1f} MP\n'
            text+=f'{photo.format or photo.path.suffix} · {photo.size/1024**2:.2f} Mio'
            if photo.path in maximum and not exact: text+='\nRésolution maximale du groupe'
            ttk.Label(card,text=text,wraplength=195).pack(anchor='w')
            ttk.Label(card,text=str(photo.path.parent),wraplength=195).pack(anchor='w',pady=(3,0))
            state=tk.StringVar(card)
            label=ttk.Label(card,textvariable=state,wraplength=195)
            label.pack(anchor='w',pady=(4,0))
            ttk.Checkbutton(card,text='Garder',variable=flag,
                            command=lambda photo=photo,flag=flag:self.toggle(photo,flag)).pack(anchor='w')
            ttk.Button(card,text='Examiner les détails à 100 %',command=lambda photo=photo:self.on_detail(photo)).pack(fill='x',pady=(4,0))
            self.cards[photo.path]={'frame':card,'image':image,'flag':flag,'state':state,'label':label}
        self.columns=0
        self.place_cards()
        self.panel.canvas.yview_moveto(0)
        self.refresh_labels()

    def toggle(self,photo,flag):
        if not self.can_edit():
            flag.set(photo.path in self.checked)
            return
        if flag.get(): self.checked.add(photo.path)
        else: self.checked.discard(photo.path)
        self.dirty=True
        self.refresh_labels()

    def keep_all(self):
        if not self.can_edit(): return
        self.checked={photo.path for photo in self.photos}
        self.dirty=True
        self.refresh_labels()

    def refresh_labels(self):
        count=len(self.checked)
        self.count.set(f'{count} à garder · {len(self.photos)-count} proposés À jeter — aucune extraction avant validation du plan'
                       if count else 'Cocher les exemplaires à garder, ou choisir explicitement Tout rejeter.')
        self.apply_button.configure(state='normal' if count and self.can_edit() else 'disabled')
        for photo in self.photos:
            card=self.cards.get(photo.path)
            if card is None: continue
            chosen=photo.path in self.checked
            card['flag'].set(chosen)
            state='À garder' if chosen else ('À jeter si répartition validée' if count else
                {'a_jeter':'À jeter · déjà validé','a_revoir':'À revoir','garder':'Garder'}.get(self.states.get(photo.path),'À revoir'))
            if photo.path==self.focus: state+=' · photo commentée'
            card['state'].set(state)
            card['label'].configure(foreground='#17644b' if chosen else '#9d3535' if count or self.states.get(photo.path)=='a_jeter' else '#354557')

    def apply(self,reject_all=False):
        if not self.can_edit(): return
        selected=set() if reject_all else set(self.checked)
        if not reject_all and not selected: return
        if self.on_apply(self.photos,selected,reject_all):
            self.checked=selected
            self.dirty=False
            self.refresh_labels()

    def resize(self,_event=None):
        self.place_cards()

    def place_cards(self):
        columns=max(1,min(4,self.panel.canvas.winfo_width()//230))
        if columns!=self.columns:
            self.columns=columns
            for column in range(4): self.panel.content.columnconfigure(column,weight=1 if column<columns else 0)
            for index,photo in enumerate(self.photos):
                self.cards[photo.path]['frame'].grid(row=index//columns,column=index%columns,sticky='nsew',padx=4,pady=4)
            self.panel.schedule_fit()
        self.queue_loading()

    def scrolled(self,first,last):
        self.panel.scrollbar.set(first,last)
        self.queue_loading()

    def queue_loading(self):
        if self.loading is None: self.loading=self.after_idle(self.load_visible)

    def load_visible(self):
        self.loading=None
        if not self.winfo_ismapped(): return
        top=self.panel.canvas.canvasy(0)-150
        bottom=top+self.panel.canvas.winfo_height()+300
        visible=[]
        for photo in self.photos:
            card=self.cards[photo.path]
            y=card['frame'].winfo_y()
            if y+card['frame'].winfo_height()>=top and y<=bottom: visible.append(photo)
        for photo in visible:
            path=photo.path
            if path in self.cache:
                self.cache.move_to_end(path)
                self.cards[path]['image'].configure(image=self.cache[path])
            elif path not in self.pending and path not in self.failed and len(self.pending)<12:
                self.pending[path]=self.executor.submit(_thumbnail,path,self.THUMB_SIZE)
        if self.pending and self.timer is None: self.timer=self.after(80,self.poll)

    def poll(self):
        self.timer=None
        for path,future in list(self.pending.items()):
            if not future.done(): continue
            self.pending.pop(path)
            try:
                raw=future.result()
                tile=Image.new('RGBA',self.THUMB_SIZE,'#17252d')
                tile.alpha_composite(raw,((tile.width-raw.width)//2,(tile.height-raw.height)//2))
                picture=ImageTk.PhotoImage(tile,master=self)
                self.cache[path]=picture
                self.cards[path]['image'].configure(image=picture)
                while len(self.cache)>32: self.cache.popitem(last=False)
            except Exception:
                self.failed.add(path)
                if path in self.cards: self.cards[path]['state'].set('Aperçu indisponible · vérifier le fichier')
        self.queue_loading()

    def cancel_loading(self):
        if self.timer is not None: self.after_cancel(self.timer)
        if self.loading is not None: self.after_cancel(self.loading)
        self.timer=self.loading=None
        for future in self.pending.values(): future.cancel()
        self.pending={}

    def cleanup(self,event):
        if event.widget==self: self.cancel_loading()
