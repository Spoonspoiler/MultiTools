from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime
import os
from pathlib import Path
import subprocess
import tkinter as tk
from tkinter import messagebox, ttk

from PIL import Image, ImageOps, ImageTk

from .inventory import Photo


def _thumbnail(path: Path, max_size: tuple[int, int]) -> Image.Image:
    with Image.open(path) as source:
        source.draft("RGB", max_size)
        image = ImageOps.exif_transpose(source).convert("RGBA")
        image.thumbnail(max_size, Image.Resampling.LANCZOS)
        return image


def _detail_crop(path: Path,size: tuple[int,int],center=(0.5,0.5)) -> Image.Image:
    """Pixels natifs : recadrage sans redimensionnement ni réécriture du fichier."""
    with Image.open(path) as source:
        image=ImageOps.exif_transpose(source)
        width,height=min(size[0],image.width),min(size[1],image.height)
        left=max(0,min(round(image.width*center[0]-width/2),image.width-width))
        top=max(0,min(round(image.height*center[1]-height/2),image.height-height))
        return image.crop((left,top,left+width,top+height)).convert('RGBA')


class Preview(ttk.Frame):
    def __init__(self, parent, executor: ThreadPoolExecutor, *, compact=False):
        super().__init__(parent, padding=6)
        self.executor = executor
        self.photo: Photo | None = None
        self.pending: Future | None = None
        self.timer: str | None = None
        self.picture = None
        self.zoom = 1
        self.native_pixels=False
        self.native_center=(0.5,0.5)

        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        self.canvas = tk.Canvas(self, width=240, height=150, bg="#17252d", highlightthickness=0)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.canvas.bind("<Configure>", self._resize)
        self.canvas.bind("<ButtonPress-1>", lambda event: self.canvas.scan_mark(event.x, event.y))
        self.canvas.bind("<B1-Motion>", lambda event: self.canvas.scan_dragto(event.x, event.y, gain=1))

        bar = ttk.Frame(self)
        self.toolbar=bar
        bar.grid(row=1, column=0, sticky="ew", pady=(5, 0))
        ttk.Button(bar, text="Ajuster", command=lambda: self._zoom(1)).pack(side="left")
        ttk.Button(bar, text="×2", width=4, command=lambda: self._zoom(2)).pack(side="left", padx=4)
        ttk.Button(bar, text="×4", width=4, command=lambda: self._zoom(4)).pack(side="left")
        self.open_button = ttk.Button(bar, text="Ouvrir le fichier", command=self._open, state="disabled")
        self.open_button.pack(side="right")
        self.info = tk.Text(self, wrap="char", height=4, borderwidth=0, relief="flat", padx=3, pady=3)
        self.info.configure(state="disabled", takefocus=False)
        self.info.grid(row=2, column=0, sticky="ew", pady=(6, 0))
        self.caption=tk.StringVar(self)
        if compact:
            bar.grid_remove()
            self.info.grid_remove()
            ttk.Label(self,textvariable=self.caption,anchor='center').grid(row=1,column=0,sticky='ew',pady=(5,0))
        self.canvas.bind('<Double-1>',self.open_fullscreen)
        self.bind("<Destroy>", lambda event: self._stop_pending() if event.widget == self else None)

    def set_photo(self, photo: Photo | None) -> None:
        self._stop_pending()
        self.photo = photo
        self.zoom = 1
        self.open_button.configure(state="normal" if photo else "disabled")
        details = ""
        if photo:
            resolution = f"{photo.width} × {photo.height}" if photo.width else "résolution inconnue"
            modified = datetime.fromtimestamp(photo.mtime_ns / 1_000_000_000).strftime("%Y-%m-%d %H:%M") if photo.mtime_ns else ""
            details = f"{photo.path}\n{photo.format} · {resolution} · {photo.size:,} octets\n"
            details += f"EXIF {photo.taken or 'date absente'}" + (f" · {photo.camera}" if photo.camera else "")
            if modified:
                details += f" · Modifié {modified}"
            if photo.warning:
                details += f"\n{photo.warning}"
        name=photo.path.name if photo else ''
        if len(name)>55: name=name[:38]+'…'+name[-13:]
        self.caption.set(f'{name}  ·  {photo.width} × {photo.height}' if photo else '')
        self.info.configure(state="normal")
        self.info.delete("1.0", "end")
        self.info.insert("1.0", details)
        self.info.configure(state="disabled")
        self._resize()

    def open_fullscreen(self,_event=None,*,native=False):
        if self.photo is None: return
        window=tk.Toplevel(self)
        window.title(self.photo.path.name)
        window.attributes('-fullscreen',True)
        preview=Preview(window,self.executor,compact=True)
        preview.native_pixels=native
        preview.canvas.bind('<Double-1>',lambda _:window.destroy())
        preview.pack(fill='both',expand=True)
        preview.set_photo(self.photo)
        tools=ttk.Frame(window)
        tools.pack(fill='x',pady=5)
        ttk.Button(tools,text='Ajuster',command=lambda:preview.set_native(False)).pack(side='left',padx=4)
        ttk.Button(tools,text='100 % · pixels natifs',command=lambda:preview.set_native(True)).pack(side='left',padx=4)
        for label,dx,dy in [('←',-0.15,0),('→',0.15,0),('↑',0,-0.15),('↓',0,0.15)]:
            ttk.Button(tools,text=label,width=3,command=lambda dx=dx,dy=dy:preview.pan_native(dx,dy)).pack(side='left')
        ttk.Button(tools,text='Fermer · Échap',command=window.destroy).pack(side='right',padx=4)
        window.bind('<Escape>',lambda _:window.destroy())

    def set_native(self,enabled):
        self.native_pixels=enabled
        self._resize()

    def pan_native(self,dx,dy):
        self.native_pixels=True
        self.native_center=tuple(max(0,min(1,value+delta)) for value,delta in zip(self.native_center,(dx,dy)))
        self._resize()

    def _zoom(self, level: int) -> None:
        self.zoom = level
        self._resize()

    def _stop_pending(self) -> None:
        if self.timer is not None:
            self.after_cancel(self.timer)
            self.timer = None
        if self.pending is not None:
            self.pending.cancel()
            self.pending = None

    def _resize(self, _event=None) -> None:
        self._stop_pending()
        self.canvas.delete("all")
        self.picture = None
        if self.photo:
            self.timer = self.after(60, self._load)

    def _load(self) -> None:
        self.timer = None
        size = (max(100, min(2200, self.canvas.winfo_width() * self.zoom)),
                max(100, min(2200, self.canvas.winfo_height() * self.zoom)))
        if self.native_pixels:
            self.pending=self.executor.submit(_detail_crop,self.photo.path,size,self.native_center)
        else:
            self.pending = self.executor.submit(_thumbnail, self.photo.path, size)
        self._display()

    def _display(self) -> None:
        self.timer = None
        if not self.pending.done():
            self.timer = self.after(50, self._display)
            return
        self.canvas.delete("all")
        try:
            image = self.pending.result()
        except Exception:
            self.canvas.create_text(12, 12, anchor="nw", fill="white", text="Aperçu indisponible")
        else:
            self.picture = ImageTk.PhotoImage(image, master=self)
            width = max(image.width, self.canvas.winfo_width())
            height = max(image.height, self.canvas.winfo_height())
            self.canvas.configure(scrollregion=(0, 0, width, height))
            self.canvas.create_image(width // 2, height // 2, image=self.picture)
            self.canvas.xview_moveto(0)
            self.canvas.yview_moveto(0)
        self.pending = None

    def _open(self) -> None:
        if self.photo is None:
            return
        path: Path = self.photo.path
        try:
            if not path.is_file():
                raise FileNotFoundError(str(path))
            if os.name == "nt":
                os.startfile(path)
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except OSError as error:
            messagebox.showerror("Ouverture du fichier", str(error), parent=self)


class SeriesGallery(ttk.Frame):
    PAGE_SIZE = 6
    THUMB_SIZE = (99, 56)

    def __init__(self, parent, executor: ThreadPoolExecutor, on_page, on_preview, on_selection):
        super().__init__(parent)
        self.executor = executor
        self.on_page, self.on_preview, self.on_selection = on_page, on_preview, on_selection
        self.series: tuple[Photo, ...] = ()
        self.visible: tuple[Photo, ...] = ()
        self.checked: set[Path] = set()
        self.focused: Photo | None = None
        self.page = 0
        self.timer: str | None = None
        self.pending: dict[int, Future] = {}
        self.pictures = {}
        self.placeholder = ImageTk.PhotoImage(Image.new("RGBA", self.THUMB_SIZE, "#17252d"), master=self)

        controls = ttk.Frame(self)
        controls.pack(fill="x")
        self.previous = ttk.Button(controls, text="◀", width=3, command=lambda: self.change_page(-1))
        self.next = ttk.Button(controls, text="▶", width=3, command=lambda: self.change_page(1))
        self.previous.pack(side="left")
        self.next.pack(side="left", padx=(4, 8))
        self.range = tk.StringVar(self)
        ttk.Label(controls, textvariable=self.range).pack(side="left")

        tiles = ttk.Frame(self)
        tiles.pack(fill="x", pady=(4, 5))
        self.tiles, self.buttons, self.marks, self.flags = [], [], [], []
        for slot in range(self.PAGE_SIZE):
            tiles.columnconfigure(slot, weight=1, uniform="miniatures")
            tile = ttk.Frame(tiles)
            tile.grid(row=0, column=slot, sticky="nsew", padx=3)
            button = ttk.Button(tile, compound="top", width=12, command=lambda slot=slot: self.on_preview(slot))
            button.pack(fill="x")
            flag = tk.BooleanVar(self)
            mark = ttk.Checkbutton(tile, text="Sélectionner", variable=flag, command=lambda slot=slot: self._toggle(slot))
            mark.pack(anchor="center")
            self.tiles.append(tile)
            self.buttons.append(button)
            self.marks.append(mark)
            self.flags.append(flag)
        self.bind("<Destroy>", lambda event: self._cancel_loading() if event.widget == self else None)

    def set_series(self, photos: tuple[Photo, ...]) -> None:
        if photos == self.series:
            return
        self.series = photos
        self.checked.clear()
        self.page = 0
        self._render()

    def set_focus(self, photo: Photo | None) -> None:
        self.focused = photo

    def selection(self) -> tuple[Photo, ...]:
        if self.checked:
            return tuple(photo for photo in self.series if photo.path in self.checked)
        return (self.focused,) if self.focused else ()

    def change_page(self, offset: int) -> None:
        target = max(0, min(self.page + offset, (len(self.series) - 1) // self.PAGE_SIZE))
        if target != self.page:
            self.page = target
            self._render()

    def _toggle(self, slot: int) -> None:
        path = self.visible[slot].path
        if self.flags[slot].get():
            self.checked.add(path)
        else:
            self.checked.discard(path)
        self.on_selection()

    def _cancel_loading(self) -> None:
        if self.timer is not None:
            self.after_cancel(self.timer)
            self.timer = None
        for future in self.pending.values():
            future.cancel()
        self.pending.clear()

    def _render(self) -> None:
        self._cancel_loading()
        self.pictures.clear()
        start = self.page * self.PAGE_SIZE
        self.visible = self.series[start:start + self.PAGE_SIZE]
        end = start + len(self.visible)
        self.range.set(f"Photos {start + 1}–{end} sur {len(self.series)}" if self.visible else "")
        self.previous.configure(state="normal" if start else "disabled")
        self.next.configure(state="normal" if end < len(self.series) else "disabled")
        for slot, tile in enumerate(self.tiles):
            if slot >= len(self.visible) or len(self.series) == 1:
                tile.grid_remove()
                continue
            photo = self.visible[slot]
            name = photo.path.name
            label = name if len(name) <= 16 else f"{name[:10]}…{name[-5:]}"
            tile.grid()
            self.flags[slot].set(photo.path in self.checked)
            self.buttons[slot].configure(image=self.placeholder,
                text=f"{start + slot + 1} · {label}\n{photo.format or photo.path.suffix} · {photo.width}×{photo.height}\n{photo.size / 1024**2:.2f} Mio")
            self.pending[slot] = self.executor.submit(_thumbnail, photo.path, self.THUMB_SIZE)
        self.on_page(self.visible, start)
        if self.pending:
            self.timer = self.after(50, self._display)

    def _display(self) -> None:
        self.timer = None
        for slot, future in list(self.pending.items()):
            if not future.done():
                continue
            del self.pending[slot]
            try:
                image = future.result()
            except Exception:
                continue
            width, height = self.THUMB_SIZE
            tile = Image.new("RGBA", (width, height), "#17252d")
            tile.alpha_composite(image, ((width - image.width) // 2, (height - image.height) // 2))
            self.pictures[slot] = ImageTk.PhotoImage(tile, master=self)
            self.buttons[slot].configure(image=self.pictures[slot])
        if self.pending:
            self.timer = self.after(50, self._display)
