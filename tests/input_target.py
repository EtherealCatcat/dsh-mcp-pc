"""Small Tk target used to verify real keyboard/mouse injection.

It opens a window with a focused entry, writes its screen coordinates to
``.tmp/input-target.json``, then waits for Return. On Return it writes the entry
contents to ``.tmp/input-result.txt`` and exits.
"""

import json
import time
import tkinter as tk
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = ROOT / ".tmp"
TMP.mkdir(exist_ok=True)

window = tk.Tk()
window.title("DSHPC input test target")
window.geometry("520x180+220+180")
window.attributes("-topmost", True)

label = tk.Label(window, text="Type here, then press Enter", font=("Segoe UI", 12))
label.pack(pady=(18, 6))

entry = tk.Entry(window, font=("Segoe UI", 14), width=42)
entry.pack(pady=6, padx=20)
entry.focus_set()

window.update_idletasks()
window.update()
coords = {
    "window_x": window.winfo_rootx(),
    "window_y": window.winfo_rooty(),
    "entry_x": entry.winfo_rootx(),
    "entry_y": entry.winfo_rooty(),
    "entry_width": entry.winfo_width(),
    "entry_height": entry.winfo_height(),
    "pid": __import__("os").getpid(),
}
(TMP / "input-target.json").write_text(json.dumps(coords), encoding="utf-8")


def finish(_event=None):
    (TMP / "input-result.txt").write_text(entry.get(), encoding="utf-8")
    window.destroy()


entry.bind("<Return>", finish)
label.configure(text=f"Ready at {coords['entry_x']},{coords['entry_y']}")

window.after(30000, finish)
window.mainloop()
