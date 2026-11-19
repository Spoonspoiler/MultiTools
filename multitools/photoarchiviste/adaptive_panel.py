"""Défilement local uniquement quand le contenu dépasse la place disponible."""
import tkinter as tk
from tkinter import ttk


class AdaptivePanel(ttk.Frame):
    def __init__(self,parent,*,style='Photo.Side.TFrame',background='#f5f7fa'):
        super().__init__(parent,style=style)
        self.columnconfigure(0,weight=1)
        self.rowconfigure(0,weight=1)
        self.canvas=tk.Canvas(self,highlightthickness=0,width=1,height=1,background=background)
        self.canvas.grid(row=0,column=0,sticky='nsew')
        self.scrollbar=ttk.Scrollbar(self,command=self.canvas.yview)
        self.scrollbar.grid(row=0,column=1,sticky='ns')
        self.scrollbar.grid_remove()
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.content=ttk.Frame(self.canvas,style=style)
        self.window=self.canvas.create_window(0,0,window=self.content,anchor='nw')
        self.pending=None
        self.overflow=False
        self.wheel_tag='PhotoScroll'+str(id(self))
        self.bind_class(self.wheel_tag,'<MouseWheel>',self.wheel)
        self.bind_class(self.wheel_tag,'<Button-4>',self.wheel)
        self.bind_class(self.wheel_tag,'<Button-5>',self.wheel)
        self.canvas.bind('<Configure>',self.resize)
        self.content.bind('<Configure>',self.schedule_fit)
        self.bind('<Destroy>',self.cleanup)

    def resize(self,event):
        self.canvas.itemconfigure(self.window,width=event.width)
        self.schedule_fit()

    def schedule_fit(self,_event=None):
        if self.pending is None: self.pending=self.after_idle(self.fit)

    def fit(self):
        self.pending=None
        self.canvas.configure(scrollregion=self.canvas.bbox('all'))
        self.overflow=self.content.winfo_reqheight()>self.canvas.winfo_height()+2
        if self.overflow:
            self.scrollbar.grid()
        else:
            self.scrollbar.grid_remove()
            self.canvas.yview_moveto(0)
        def attach(widget):
            tags=widget.bindtags()
            if self.wheel_tag not in tags:
                widget.bindtags((tags[0],self.wheel_tag,*tags[1:]))
            for child in widget.winfo_children(): attach(child)
        attach(self.content)
        attach(self.canvas)

    def wheel(self,event):
        if not self.overflow or isinstance(event.widget,(ttk.Combobox,tk.Text,tk.Listbox)):
            return
        if getattr(event,'num',None)==4: units=-1
        elif getattr(event,'num',None)==5: units=1
        else:
            delta=getattr(event,'delta',0)
            if not delta: return
            units=-max(1,abs(delta)//120) if delta>0 else max(1,abs(delta)//120)
        self.canvas.yview_scroll(units,'units')
        return 'break'

    def cleanup(self,event):
        if event.widget!=self: return
        if self.pending is not None: self.after_cancel(self.pending)
        for sequence in ('<MouseWheel>','<Button-4>','<Button-5>'):
            self.unbind_class(self.wheel_tag,sequence)
