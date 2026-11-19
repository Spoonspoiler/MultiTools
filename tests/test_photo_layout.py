import tkinter as tk
import unittest

from multitools.photoarchiviste.ui import PhotoArchiviste


class PhotoLayoutTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root=tk.Tk()
        except tk.TclError as error:
            self.skipTest(str(error))
        self.addCleanup(self.root.destroy)
        self.pane=PhotoArchiviste(self.root)
        self.pane.pack(fill='both',expand=True)

    def settle(self):
        for _ in range(4):
            self.root.update_idletasks()
            self.root.update()

    def assert_accessible(self,widget):
        self.assertTrue(widget.winfo_ismapped(),str(widget))
        x,y=widget.winfo_rootx(),widget.winfo_rooty()
        right,bottom=x+widget.winfo_width(),y+widget.winfo_height()
        self.assertGreaterEqual(widget.winfo_width(),widget.winfo_reqwidth()-2,str(widget))
        ancestor=widget.master
        while ancestor is not None:
            self.assertGreaterEqual(x,ancestor.winfo_rootx()-1,str(widget))
            self.assertGreaterEqual(y,ancestor.winfo_rooty()-1,str(widget))
            self.assertLessEqual(right,ancestor.winfo_rootx()+ancestor.winfo_width()+1,str(widget))
            self.assertLessEqual(bottom,ancestor.winfo_rooty()+ancestor.winfo_height()+1,str(widget))
            if ancestor==self.root: break
            ancestor=ancestor.master

    def test_primary_buttons_remain_visible_with_many_tags_and_long_filename(self):
        owner=self.pane.dedup
        assistant=owner.index_assistant
        proposed=[f'{i} sujet proposé assez long pour deux lignes' for i in range(8)]
        custom=[f'{i} tag personnel assez long pour deux lignes' for i in range(12)]
        assistant.tag_editor.set_tags(proposed,proposed+custom)
        assistant.counter.set('Indexation : '+('nom de photo très long '*30))
        assistant.pause_button.configure(text='Pause après la photo')
        for width,height in [(960,640),(1280,850)]:
            with self.subTest(size=(width,height)):
                self.root.geometry(f'{width}x{height}')
                self.settle()
                for widget in (self.pane.workspace_button,self.pane.source_button,self.pane.scan_button,
                               assistant.pause_button,assistant.validate_button,assistant.reject_group_button,
                               owner.apply_button,owner.new_folder_button):
                    self.assert_accessible(widget)
        self.assertTrue(assistant.details_panel.overflow)

    def test_tag_scrollbar_hides_when_content_fits(self):
        self.root.geometry('1280x900')
        assistant=self.pane.dedup.index_assistant
        assistant.tag_editor.set_tags([],[])
        self.settle()
        self.assertFalse(assistant.details_panel.overflow)
        self.assertFalse(assistant.details_panel.scrollbar.winfo_ismapped())
