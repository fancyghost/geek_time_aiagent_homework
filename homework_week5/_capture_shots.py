"""Reproducible screenshot capture: headless Chrome renders the app inside a
same-origin iframe (_diag.html) at exact viewport sizes, then crops to the
iframe box. Run:  python _capture_shots.py   (server must be running on 8770)"""
import os
import subprocess
import sys
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
SS = os.path.join(HERE, "screenshots")
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
BASE = "http://127.0.0.1:8770/_diag.html"

# name, hash, window-size, crop-box
JOBS = [
    ("01-desktop-default", "d", "1440,1560", (0, 0, 1440, 1560)),
    ("02-viewport-top", "dv", "1440,800", (0, 0, 1440, 800)),
    ("03-detail-drawer", "dv?select=c8", "1440,800", (0, 0, 1440, 800)),
    ("04-type-lanes", "dv?group=type&scroll=chart", "1440,800", (0, 0, 1440, 800)),
    ("05-ember-theme", "dv?theme=ember", "1440,800", (0, 0, 1440, 800)),
    ("06-rag-trace", "dv?trace=rag-qa", "1440,800", (0, 0, 1440, 800)),
    ("09-blueprint-theme", "dv?theme=blueprint", "1440,800", (0, 0, 1440, 800)),
    ("07-mobile-390", "m", "500,900", (0, 0, 390, 844)),
    ("07b-mobile-390-tall", "mt", "500,1560", (0, 0, 390, 1500)),
    ("08-tablet-820", "t", "900,1250", (0, 0, 820, 1180)),
]


def main():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    os.makedirs(SS, exist_ok=True)
    for name, hash_, ws, box in JOBS:
        raw = os.path.join(SS, "_raw_%s.png" % name)
        dst = os.path.join(SS, name + ".png")
        cmd = [CHROME, "--headless=new", "--disable-gpu", "--no-sandbox",
               "--force-device-scale-factor=1", "--virtual-time-budget=10000",
               "--window-size=" + ws, "--screenshot=" + raw, BASE + "#" + hash_]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=90)
        if not os.path.exists(raw):
            print("FAIL capture", name)
            continue
        im = Image.open(raw)
        im.crop(box).save(dst)
        os.remove(raw)
        print("ok", name, im.size, "->", box)


if __name__ == "__main__":
    main()