import os,sys
sys.path.insert(0, "src")
from PyQt6.QtWidgets import QApplication
from ningbingjue.app import Application
ROOT = r"D://下载//localsend//凝冰结魄-覆盖任务栏-修复-完全解耦-备份-20260717172824//凝冰结魄-覆盖任务栏-修复-完全解耦-备份"
print("booting...")
app = Application(ROOT, headless=False).boot()
print("booted, window=", getattr(app, "_window", None))
