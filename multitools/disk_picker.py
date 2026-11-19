"""Choix explicite d'un volume avant le sélecteur de dossiers standard."""
from pathlib import Path
from queue import Empty, SimpleQueue
from threading import Thread
import tkinter as tk
from tkinter import filedialog, ttk, simpledialog, messagebox

from .storage import mounts
from .folders import create_named_folder


def disk_locations(inventory):
    """Emplacements montés ; aucun parcours du contenu des disques."""
    result = [("Dossier personnel", Path.home())]
    seen = {Path.home()}
    for volume in inventory:
        path = volume.path
        if not volume.uuid or path in seen or str(path).startswith(("/snap/", "/boot/", "/home/")):
            continue
        name = "Disque système" if path == Path('/') else path.name
        result.append((name, path))
        seen.add(path)
    return result


def askdirectory(*, parent, title="Choisir un dossier", initialdir=None, mustexist=True,allow_create=True):
    return _choose_path(parent=parent, title=title, initialdir=initialdir, mustexist=mustexist,allow_create=allow_create)


def ask_new_directory(*,parent,initialdir=None):
    folder=askdirectory(parent=parent,title='Emplacement du nouveau dossier',initialdir=initialdir,allow_create=False)
    if not folder: return ''
    label=Path(folder).name or str(Path(folder).anchor)
    name=simpledialog.askstring('Nouveau dossier','Nom du dossier à créer dans « '+label[:55]+' » :',parent=parent)
    if name is None: return ''
    try: return str(create_named_folder(folder,name))
    except (OSError,ValueError) as error:
        messagebox.showerror('Création du dossier impossible',str(error),parent=parent)
        return ''


def askopenfilename(*, parent, title="Choisir un fichier", initialdir=None, filetypes=(("Tous", "*"),)):
    return _choose_path(parent=parent, title=title, initialdir=initialdir, filetypes=filetypes)


def _choose_path(*, parent, title, initialdir=None, mustexist=True, filetypes=None,allow_create=True):
    dialog = tk.Toplevel(parent)
    dialog.title("Mes disques — " + title)
    dialog.geometry("760x360")
    dialog.transient(parent.winfo_toplevel())
    frame = ttk.Frame(dialog, padding=12)
    frame.pack(fill="both", expand=True)
    ttk.Label(frame, text="Choisis un disque, puis parcours ses fichiers." if filetypes is not None
              else "Choisis un disque, puis parcours ses dossiers.").pack(anchor="w")
    tree = ttk.Treeview(frame, columns=("name", "path"), show="headings", selectmode="browse")
    tree.heading("name", text="Disque")
    tree.heading("path", text="Emplacement")
    tree.column("name", width=180)
    tree.column("path", width=470)
    tree.pack(fill="both", expand=True, pady=8)
    locations = {"home": Path.home()}
    tree.insert('', 'end', iid='home', values=("Dossier personnel", str(Path.home())))
    tree.selection_set('home')
    status = tk.StringVar(dialog, "Recherche des disques montés…")
    ttk.Label(frame, textvariable=status).pack(anchor="w")
    result = []

    def browse():
        selection = tree.selection()
        if not selection:
            return
        start = locations[selection[0]]
        if filetypes is not None:
            folder = filedialog.askopenfilename(parent=dialog, title=title, initialdir=str(start), filetypes=filetypes)
        else:
            folder = filedialog.askdirectory(parent=dialog, title=title, initialdir=str(start), mustexist=mustexist)
        if folder:
            result.append(folder)
            dialog.destroy()

    controls = ttk.Frame(frame)
    controls.pack(fill="x", pady=(8, 0))
    ttk.Button(controls, text="Choisir un fichier…" if filetypes is not None else "Parcourir les dossiers…",
               command=browse).pack(side="right")
    ttk.Button(controls, text="Annuler", command=dialog.destroy).pack(side="right", padx=8)
    if filetypes is None and allow_create:
        def create():
            selected=tree.selection()
            start=locations[selected[0]] if selected else Path.home()
            folder=ask_new_directory(parent=dialog,initialdir=str(start))
            if folder:
                result.append(folder)
                dialog.destroy()
            elif dialog.winfo_exists():
                dialog.grab_set()
        ttk.Button(controls,text='Nouveau dossier…',command=create).pack(side='left')
    tree.bind('<Double-1>', lambda _: browse())
    if initialdir and Path(initialdir).is_dir():
        locations['current'] = Path(initialdir)
        tree.insert('', 'end', iid='current', values=("Dernier dossier", initialdir))
        tree.selection_set('current')
    messages = SimpleQueue()

    def load():
        try:
            messages.put((disk_locations(mounts()), None))
        except Exception as error:
            messages.put(([], str(error)))

    def poll():
        try:
            rows, error = messages.get_nowait()
        except Empty:
            dialog.after(80, poll)
            return
        for index, (name, path) in enumerate(rows):
            if path == Path.home():
                continue
            key = f'disk-{index}'
            locations[key] = path
            tree.insert('', 'end', iid=key, values=(name, str(path)))
        status.set(f"Détection impossible : {error}" if error else
                   "Les disques doivent être montés pour apparaître ici.")

    Thread(target=load, daemon=True, name='multitools-disques').start()
    dialog.after(80, poll)
    dialog.grab_set()
    parent.wait_window(dialog)
    return result[0] if result else ''
