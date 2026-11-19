"""Suggestions de tags distinctes des ajouts et choix humains."""
import unicodedata
import tkinter as tk
from tkinter import ttk


def clean(values):
    result={}
    for value in values:
        if not isinstance(value,str): raise ValueError('Un tag doit être un texte.')
        value=unicodedata.normalize('NFC',value).strip()[:40]
        if value: result.setdefault(value.casefold(),value)
    return result


class TagSelection:
    def __init__(self,proposed=(),selected=()):
        self.proposed=clean(proposed)
        self.chosen=clean(selected)
        if len(self.chosen)>20: raise ValueError('20 tags maximum par photo.')

    def values(self):
        return list(self.chosen.values())

    def personal(self):
        return [value for key,value in self.chosen.items() if key not in self.proposed]

    def toggle(self,tag,enabled):
        key=next(iter(clean([tag])),None)
        if not key: return
        if enabled:
            self.add([tag])
        else:
            self.chosen.pop(key,None)

    def add(self,values):
        candidate={**self.chosen,**clean(values)}
        if len(candidate)>20: raise ValueError('20 tags maximum par photo.')
        self.chosen=candidate


class TagEditor(ttk.Frame):
    def __init__(self,parent,variable,*,on_change=lambda:None,on_error=lambda message:None,can_edit=lambda:True):
        super().__init__(parent,style='Photo.Side.TFrame')
        self.variable=variable
        self.on_change,self.on_error,self.can_edit=on_change,on_error,can_edit
        self.model=TagSelection()
        self.syncing=False
        self.personal_page=0
        self.new_tag=tk.StringVar(self)
        ttk.Label(self,text='Qwen propose · coche les tags à retenir',style='Photo.Side.TLabel').pack(anchor='w')
        self.proposals=ttk.Frame(self,style='Photo.Side.TFrame')
        self.proposals.pack(fill='x',pady=(4,6))
        personal_bar=ttk.Frame(self,style='Photo.Side.TFrame')
        personal_bar.pack(fill='x')
        self.personal_title=tk.StringVar(self,'Mes ajouts')
        ttk.Label(personal_bar,textvariable=self.personal_title,style='Photo.Side.TLabel').pack(side='left')
        self.previous=ttk.Button(personal_bar,text='‹',width=2,command=lambda:self.page(-1))
        self.following=ttk.Button(personal_bar,text='›',width=2,command=lambda:self.page(1))
        self.personal_tags=ttk.Frame(self,style='Photo.Side.TFrame')
        self.personal_tags.pack(fill='x',pady=(3,4))
        add=ttk.Frame(self,style='Photo.Side.TFrame')
        add.pack(fill='x')
        self.entry=ttk.Entry(add,textvariable=self.new_tag,width=18)
        self.entry.pack(side='left',fill='x',expand=True)
        self.entry.bind('<Return>',self.add_entered)
        ttk.Button(add,text='Ajouter',command=self.commit_pending).pack(side='left',padx=(4,0))
        self.trace=variable.trace_add('write',self.external_change)
        self.bind('<Destroy>',self.close)
        self.render()

    def close(self,event):
        if event.widget==self:
            self.variable.trace_remove('write',self.trace)

    def set_tags(self,proposed,selected):
        self.model=TagSelection(proposed,selected)
        self.personal_page=0
        self.new_tag.set('')
        self.publish(notify=False)

    def external_change(self,*args):
        if self.syncing: return
        try:
            self.model=TagSelection(self.model.proposed.values(),self.variable.get().split(','))
            self.render()
        except ValueError as error:
            self.on_error(str(error))

    def publish(self,notify=True):
        self.syncing=True
        try: self.variable.set(', '.join(self.model.values()))
        finally: self.syncing=False
        self.render()
        if notify: self.on_change()

    def toggle(self,tag,flag):
        if not self.can_edit():
            self.render()
            return
        try:
            self.model.toggle(tag,flag.get())
            self.publish()
        except ValueError as error:
            self.render()
            self.on_error(str(error))

    def remove(self,tag):
        if not self.can_edit(): return
        self.model.toggle(tag,False)
        self.publish()

    def add_entered(self,event):
        self.commit_pending()
        return 'break'

    def commit_pending(self):
        if not self.new_tag.get().strip(): return True
        if not self.can_edit():
            self.on_error('Arrêter le passage avant de valider les tags.')
            return False
        try:
            self.model.add(self.new_tag.get().split(','))
            self.personal_page=max(0,(len(self.model.personal())-1)//4)
            self.new_tag.set('')
            self.publish()
            return True
        except ValueError as error:
            self.on_error(str(error))
            return False

    def page(self,offset):
        self.personal_page=max(0,min(self.personal_page+offset,max(0,(len(self.model.personal())-1)//4)))
        self.render()

    def render(self):
        for frame in (self.proposals,self.personal_tags):
            for child in frame.winfo_children(): child.destroy()
            frame.columnconfigure((0,1),weight=1)
        if not self.model.proposed:
            ttk.Label(self.proposals,text='Aucun tag proposé pour cette photo',style='Photo.Side.TLabel').grid(sticky='w')
        for index,(key,tag) in enumerate(self.model.proposed.items()):
            box=ttk.Frame(self.proposals,style='Photo.Side.TFrame')
            box.grid(row=index//2,column=index%2,sticky='nw',padx=(0,4),pady=1)
            flag=tk.BooleanVar(self,key in self.model.chosen)
            button=ttk.Checkbutton(box,variable=flag,command=lambda tag=tag,flag=flag:self.toggle(tag,flag))
            button.pack(side='left',anchor='n')
            label=ttk.Label(box,text=tag,wraplength=102,style='Photo.Side.TLabel')
            label.pack(side='left',anchor='w')
            def click(_event,tag=tag,flag=flag):
                flag.set(not flag.get())
                self.toggle(tag,flag)
            label.bind('<Button-1>',click)
        personal=self.model.personal()
        self.personal_page=min(self.personal_page,max(0,(len(personal)-1)//4))
        self.personal_title.set(f'Mes ajouts ({len(personal)})' if personal else 'Mes ajouts')
        if len(personal)>4:
            self.following.pack(side='right')
            self.previous.pack(side='right',padx=3)
            self.previous.configure(state='normal' if self.personal_page else 'disabled')
            self.following.configure(state='normal' if (self.personal_page+1)*4<len(personal) else 'disabled')
        else:
            self.previous.pack_forget()
            self.following.pack_forget()
        for index,tag in enumerate(personal[self.personal_page*4:self.personal_page*4+4]):
            chip=ttk.Frame(self.personal_tags,style='Photo.Side.TFrame')
            chip.grid(row=index//2,column=index%2,sticky='nw',padx=(0,4))
            ttk.Label(chip,text=tag,wraplength=96,style='Photo.Side.TLabel').pack(side='left')
            ttk.Button(chip,text='×',width=2,command=lambda tag=tag:self.remove(tag)).pack(side='left')
