#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
app.py - mvdl indirme yoneticisi (yerel web uygulamasi).

Baslat:  py app.py
Tarayicida kendi penceresinde acilir. Sayfa URL'sini yapistir -> otomatik
m3u8 tespiti -> kuyruga ekle. Birden fazla es zamanli indirme, kaldigi yerden
devam, adaptif paralellik, daima .mp4.
"""

import os
import re
import sys
import json
import time
import shutil
import socket
import tempfile
import threading
import subprocess
import webbrowser
from urllib.parse import urlparse, parse_qs, unquote
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import engine
import detector

HERE = os.path.dirname(os.path.abspath(__file__))
FROZEN = getattr(sys, "frozen", False)
# Paketlenmis (.exe/.app) modda: kaynaklar _MEIPASS'te, kalici veri exe'nin yaninda.
RES = getattr(sys, "_MEIPASS", HERE)                          # gomulu kaynaklar (web/)
DATA = os.path.dirname(sys.executable) if FROZEN else HERE    # kalici veri (jobs/settings/downloads)
WEB = os.path.join(RES, "web")
STATE_FILE = os.path.join(DATA, "jobs.json")
SETTINGS_FILE = os.path.join(DATA, "settings.json")
UA = "Mozilla/5.0"


def headers_for(referer):
    h = {"User-Agent": UA, "Accept": "*/*", "Accept-Encoding": "identity"}
    if referer:
        h["Referer"] = referer
        m = re.match(r"(https?://[^/]+)", referer)
        if m:
            h["Origin"] = m.group(1)
    return h


# ---------------------------------------------------------------------------
# Is yoneticisi
# ---------------------------------------------------------------------------
class Manager:
    def __init__(self):
        self.jobs = {}          # id -> engine.Job
        self.order = []         # id sirasi
        self.detects = {}       # detect_id -> {status, log, result}
        self.lock = threading.Lock()
        self._seq = 0
        self.settings = {
            "download_dir": os.path.join(DATA, "downloads"),
            "max_parallel": 3,
            "default_conn": 16,
        }
        self._load_settings()
        os.makedirs(self.settings["download_dir"], exist_ok=True)
        self._load_jobs()
        threading.Thread(target=self._scheduler, daemon=True).start()

    def _nid(self, prefix="j"):
        self._seq += 1
        return f"{prefix}{int(time.time())%100000}{self._seq}"

    # ---- ayar/durum kalicilastirma ----
    def _load_settings(self):
        try:
            with open(SETTINGS_FILE, encoding="utf-8") as f:
                self.settings.update(json.load(f))
        except Exception:
            pass

    def _save_settings(self):
        try:
            with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump(self.settings, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _job_meta(self, job):
        return {
            "id": job.id, "manifest_url": job.manifest_url, "page_url": job.page_url,
            "out_dir": job.out_dir, "name": job.name, "quality": job.quality,
            "user_max": job.user_max, "referer": job._headers.get("Referer", ""),
            "state": job.state, "out_path": job.out_path,
            "start_sec": job.start_sec, "end_sec": job.end_sec, "subs_lang": job.subs_lang,
            "total": job.total, "done": job.done, "bytes": job.bytes,
        }

    def _save_jobs(self):
        try:
            data = [self._job_meta(self.jobs[i]) for i in self.order if i in self.jobs]
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _load_jobs(self):
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return
        for m in data:
            job = engine.Job(
                m["id"], m["manifest_url"], m["out_dir"], m["name"],
                headers_for(m.get("referer", "")), quality=m.get("quality", "best"),
                user_max=m.get("user_max", 16), page_url=m.get("page_url"),
                start_sec=m.get("start_sec"), end_sec=m.get("end_sec"),
                subs_lang=m.get("subs_lang", ""))
            # bitmis isler bitmis kalsin; digerleri yeniden kuyruga (resume)
            if m.get("state") == "done" and m.get("out_path") and os.path.exists(m["out_path"]):
                job.state = "done"; job.out_path = m["out_path"]
                job.total = m.get("total", 0); job.done = m.get("done", job.total)
                try:
                    job.bytes = os.path.getsize(m["out_path"])
                except OSError:
                    job.bytes = m.get("bytes", 0)
            elif m.get("state") == "error":
                job.state = "error"; job.error = "onceki oturumda hata"
            else:
                job.state = "queued"
            self.jobs[job.id] = job
            self.order.append(job.id)

    # ---- tespit ----
    def start_detect(self, url):
        did = self._nid("d")
        entry = {"status": "running", "log": [], "result": None, "url": url}
        self.detects[did] = entry

        def logf(msg):
            entry["log"].append(msg)
            entry["log"] = entry["log"][-40:]

        def work():
            try:
                res = detector.detect_streams(url, log=logf)
                entry["result"] = res
                entry["status"] = "done"
            except Exception as e:  # noqa: BLE001
                entry["result"] = {"candidates": [], "error": str(e)}
                entry["status"] = "done"

        threading.Thread(target=work, daemon=True).start()
        return did

    # ---- is ekle/kontrol ----
    def add_job(self, manifest_url, referer, title, quality, out_dir, conn,
                page_url=None, start_sec=None, end_sec=None, subs_lang=""):
        with self.lock:
            jid = self._nid("j")
            out_dir = out_dir or self.settings["download_dir"]
            os.makedirs(out_dir, exist_ok=True)
            job = engine.Job(jid, manifest_url, out_dir, title or "video",
                             headers_for(referer), quality=quality or "best",
                             user_max=conn or self.settings["default_conn"],
                             page_url=page_url, start_sec=start_sec, end_sec=end_sec,
                             subs_lang=subs_lang or "")
            self.jobs[jid] = job
            self.order.append(jid)
            self._save_jobs()
            return jid

    def control(self, jid, action):
        job = self.jobs.get(jid)
        if not job:
            return False
        if action == "pause":
            job.pause()
        elif action == "resume":
            if job.state == "paused":
                job.resume()
            elif job.state in ("error", "canceled"):
                job.state = "queued"; job.error = ""; job.error_code = ""  # yeniden dene
        elif action == "cancel":
            job.cancel()
        elif action == "remove":
            job.cancel()
            with self.lock:
                self.jobs.pop(jid, None)
                if jid in self.order:
                    self.order.remove(jid)
        self._save_jobs()
        return True

    def _active(self):
        return sum(1 for j in self.jobs.values()
                   if j.state in ("running", "muxing", "paused", "detecting"))

    def _redetect(self, job):
        """Token bitince sayfadan taze m3u8 al (ayni icerik akisi)."""
        try:
            res = detector.detect_streams(job.page_url)
            cands = res.get("candidates") or []
            return cands[0] if cands else None   # relevance'a gore sirali; en uygun
        except Exception:
            return None

    def _scheduler(self):
        tick = 0
        while True:
            try:
                started = False
                if self._active() < self.settings["max_parallel"]:
                    for jid in list(self.order):
                        job = self.jobs.get(jid)
                        if job and job.state == "queued":
                            job.state = "running"
                            threading.Thread(target=self._run_job, args=(job,),
                                             daemon=True).start()
                            started = True
                            if self._active() >= self.settings["max_parallel"]:
                                break
                tick += 1
                if started or tick % 4 == 0:
                    self._save_jobs()
            except Exception:
                pass
            time.sleep(0.5)

    def _run_job(self, job):
        job.run()
        # Token suresi dolduysa ve sayfa URL'si varsa: taze m3u8 al, kaldigin
        # yerden devam et (parts diskte oldugu icin resume calisir).
        # Not: token hatasi da _cancel'i set eder; her turda temizleyip
        # kullanici iptalini (state == "canceled") ayirt ediyoruz.
        while (job.state == "error" and job.error_code == "token"
               and job.page_url and job._auto_retries < 3):
            job._auto_retries += 1
            job._cancel.clear()                       # token kaynakli iptali temizle
            job.state = "detecting"; job.error = ""; job.error_code = ""
            self._save_jobs()
            fresh = self._redetect(job)
            if job._cancel.is_set() or job.state == "canceled":
                break                                 # kullanici bu sirada iptal etti
            if not fresh:
                job._set_error("token"); break
            job.manifest_url = fresh["url"]
            job._headers = headers_for(fresh.get("referer") or job._headers.get("Referer", ""))
            job.run()
        self._save_jobs()

    def state(self):
        with self.lock:
            jobs = [self.jobs[i].to_dict() for i in self.order if i in self.jobs]
        return {"jobs": jobs, "settings": self.settings,
                "ffmpeg": bool(engine.find_ffmpeg())}


MGR = Manager()


# ---------------------------------------------------------------------------
# Klasor tarayici (yerel)
# ---------------------------------------------------------------------------
def list_folders(path):
    if not path or not os.path.isdir(path):
        path = MGR.settings["download_dir"]
    path = os.path.abspath(path)
    dirs = []
    try:
        for name in sorted(os.listdir(path), key=str.lower):
            full = os.path.join(path, name)
            if os.path.isdir(full):
                dirs.append({"name": name, "path": full})
    except Exception:
        pass
    parent = os.path.dirname(path.rstrip("\\/")) or path
    # Windows surucu koklerini de ekle
    drives = []
    if os.name == "nt":
        import string
        for d in string.ascii_uppercase:
            root = f"{d}:\\"
            if os.path.exists(root):
                drives.append(root)
    return {"path": path, "parent": parent, "dirs": dirs, "drives": drives}


# ---------------------------------------------------------------------------
# HTTP sunucu
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, (bytes, bytearray)) else json.dumps(
            body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(data)
        except Exception:
            pass

    def _body(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(n) if n else b"{}"
        try:
            return json.loads(raw or b"{}")
        except Exception:
            return {}

    def do_GET(self):
        u = urlparse(self.path)
        p = u.path
        if p in ("/", "/index.html"):
            try:
                with open(os.path.join(WEB, "index.html"), "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            except Exception as e:
                self._send(500, {"error": str(e)})
            return
        if p == "/api/state":
            self._send(200, MGR.state()); return
        if p.startswith("/api/detect/"):
            did = p.rsplit("/", 1)[-1]
            self._send(200, MGR.detects.get(did, {"status": "yok"})); return
        if p == "/api/folders":
            q = parse_qs(u.query)
            self._send(200, list_folders(unquote((q.get("path") or [""])[0]))); return
        self._send(404, {"error": "yok"})

    def do_POST(self):
        u = urlparse(self.path)
        p = u.path
        body = self._body()
        if p == "/api/detect":
            did = MGR.start_detect(body.get("url", "").strip())
            self._send(200, {"detect_id": did}); return
        if p == "/api/jobs":
            def _sec(v):
                try:
                    return int(v) if v not in (None, "", False) else None
                except Exception:
                    return None
            jid = MGR.add_job(
                body.get("manifest_url", "").strip(), body.get("referer", ""),
                body.get("title", "video"), body.get("quality", "best"),
                body.get("dir", ""), int(body.get("conn", 0) or 0),
                page_url=body.get("page_url"),
                start_sec=_sec(body.get("start_sec")), end_sec=_sec(body.get("end_sec")),
                subs_lang=body.get("subs_lang", ""))
            self._send(200, {"id": jid}); return
        m = re.match(r"^/api/jobs/([^/]+)/(pause|resume|cancel|remove)$", p)
        if m:
            ok = MGR.control(m.group(1), m.group(2))
            self._send(200, {"ok": ok}); return
        if p == "/api/open":
            job = MGR.jobs.get(body.get("id", ""))
            path = job.out_path if (job and job.out_path) else body.get("path", "")
            try:
                if not path or not os.path.exists(path):
                    self._send(200, {"ok": False, "error": "dosya bulunamadi"}); return
                if body.get("reveal"):
                    reveal_in_folder(path)      # klasorde goster+sec (capraz platform)
                else:
                    open_file(path)             # dosyayi oynat (capraz platform)
                self._send(200, {"ok": True})
            except Exception as e:  # noqa: BLE001
                self._send(200, {"ok": False, "error": str(e)})
            return
        if p == "/api/settings":
            for k in ("download_dir", "max_parallel", "default_conn"):
                if k in body and body[k] not in (None, ""):
                    MGR.settings[k] = body[k]
            if MGR.settings.get("download_dir"):
                try:
                    os.makedirs(MGR.settings["download_dir"], exist_ok=True)
                except Exception:
                    pass
            MGR._save_settings()
            self._send(200, {"ok": True, "settings": MGR.settings}); return
        self._send(404, {"error": "yok"})


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _spawn(args):
    subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def reveal_in_folder(path):
    """Dosyayi klasorde secili gosterir (Win/Mac/Linux)."""
    path = os.path.normpath(path)
    if sys.platform.startswith("win"):
        subprocess.Popen(f'explorer /select,"{path}"')
    elif sys.platform == "darwin":
        _spawn(["open", "-R", path])
    else:
        try:
            _spawn(["dbus-send", "--session", "--print-reply",
                    "--dest=org.freedesktop.FileManager1",
                    "/org/freedesktop/FileManager1",
                    "org.freedesktop.FileManager1.ShowItems",
                    f"array:string:file://{path}", "string:mvdl"])
        except Exception:
            _spawn(["xdg-open", os.path.dirname(path)])


def open_file(path):
    """Dosyayi varsayilan uygulamada acar/oynatir (Win/Mac/Linux)."""
    if sys.platform.startswith("win"):
        os.startfile(path)  # noqa
    elif sys.platform == "darwin":
        _spawn(["open", path])
    else:
        _spawn(["xdg-open", path])


def find_browser():
    """Chromium tabanli bir tarayici bulur (uygulama penceresi icin)."""
    if sys.platform.startswith("win"):
        cands = [
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        ]
    elif sys.platform == "darwin":
        cands = [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
            "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
        ]
    else:
        cands = []
        for n in ("google-chrome", "google-chrome-stable", "chromium",
                  "chromium-browser", "microsoft-edge", "brave-browser"):
            w = shutil.which(n)
            if w:
                cands.append(w)
    for c in cands:
        if os.path.exists(c):
            return c
    # Playwright'in chromium'u (kurulumla gelir) - evrensel yedek
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            exe = pw.chromium.executable_path
        if exe and os.path.exists(exe):
            return exe
    except Exception:
        pass
    return None


def open_window(url):
    """Uygulama penceresini acar; kendi surecini (Popen) dondurur ki
    pencere kapaninca sunucu da kapanabilsin. Tarayici yoksa None doner."""
    time.sleep(0.4)
    browser = find_browser()
    if not browser:
        webbrowser.open(url)
        return None
    profile = os.path.join(tempfile.gettempdir(), "mvdl-profile")
    args = [browser, f"--app={url}", f"--user-data-dir={profile}",
            "--no-first-run", "--no-default-browser-check", "--new-window",
            "--window-size=1180,860"]
    try:
        return subprocess.Popen(args)
    except Exception:
        webbrowser.open(url)
        return None


def setup_logging():
    """pythonw (konsolsuz) altinda stdout/stderr None olur; log dosyasina yonlendir."""
    if sys.stdout is None or sys.stderr is None or os.environ.get("MVDL_LOG"):
        try:
            f = open(os.path.join(DATA, "mvdl.log"), "a", encoding="utf-8", buffering=1)
            sys.stdout = f
            sys.stderr = f
        except Exception:
            pass


def main():
    setup_logging()
    port = int(os.environ.get("MVDL_PORT", "8756"))
    url = f"http://127.0.0.1:{port}/"
    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError:
        # Port dolu -> zaten calisiyor. Tek ornek: mevcut pencereyi ac ve cik.
        if not os.environ.get("MVDL_NOOPEN"):
            open_window(url)
        return

    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    print(f"mvdl calisiyor: {url}")

    if os.environ.get("MVDL_NOOPEN"):          # test/başsız mod
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        return

    proc = open_window(url)
    if proc is not None:
        # Kullanici uygulama penceresini kapatinca sureci biter -> sunucuyu durdur.
        try:
            proc.wait()
        except KeyboardInterrupt:
            pass
        httpd.shutdown()
    else:
        # Tarayici bulunamadi, normal sekmede acildi -> Ctrl+C ile kapat.
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
