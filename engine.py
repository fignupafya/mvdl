#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
engine.py - Cok isli, adaptif, kesintiye dayanikli HLS indirme motoru.

Ozellikler:
  * Adaptif paralellik: 429/403/reset gorunce baglanti sayisini otomatik dusurur,
    isler yolundayken kademeli artirir (AIMD - TCP tikaniklik kontrolu gibi).
  * Resume: yarim kalan indirme ayni klasorden kaldigi yerden devam eder.
  * AES-128 (DRM degil) destegi; DRM (Widevine/FairPlay) tespit edip reddeder.
  * ffmpeg ile daima .mp4 ciktisi (bin/ffmpeg.exe otomatik bulunur).
  * pause / resume / cancel.

Parcalama (m3u8 ayristirma) mantigi hlsdl.py'den gelir (tek kaynak).
"""

import os
import re
import time
import json
import random
import shutil
import threading
import subprocess
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor

from hlsdl import (parse_master, parse_media, parse_subtitles, DRMError,
                   get_aes_decryptor, pick_rendition, UA_FALLBACKS)

HERE = os.path.dirname(os.path.abspath(__file__))
HARD_MAX_CONN = 64          # guvenlik tavani (kullanici 100 dese de gecmez)


# ---------------------------------------------------------------------------
# ffmpeg / yardimcilar
# ---------------------------------------------------------------------------
def find_ffmpeg():
    """ffmpeg yolunu bulur: once bin/, sonra PATH, sonra imageio-ffmpeg (capraz platform)."""
    exe = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    local = os.path.join(HERE, "bin", exe)
    if os.path.exists(local):
        return local
    p = shutil.which("ffmpeg") or shutil.which("ffmpeg.exe")
    if p:
        return p
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def sanitize_filename(name, default="video"):
    if not name:
        return default
    name = re.sub(r"[\\/:*?\"<>|]+", " ", name).strip()
    name = re.sub(r"\s+", " ", name)
    return name[:150] or default


# ---------------------------------------------------------------------------
# Tipli HTTP hatalari (adaptif kontrol icin)
# ---------------------------------------------------------------------------
class ThrottleError(Exception):
    """429/503 - sunucu 'yavasla' diyor. Baglanti sayisini dusur."""


class AuthError(Exception):
    """401/403 - token/UA reddi. Once UA yedegi, sonra 'token bitti'."""


class FatalHTTP(Exception):
    """Kalici 4xx (404/400 vb.)."""


class RetryableError(Exception):
    """Gecici ag hatasi/timeout - beklet, tekrar dene (paralelligi dusurme)."""


def fetch_bytes(url, headers, timeout=30, byterange=None):
    h = dict(headers)
    if byterange is not None:
        s, l = byterange
        h["Range"] = f"bytes={s}-{s + l - 1}"
    req = urllib.request.Request(url, headers=h, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        if e.code in (429, 503):
            raise ThrottleError(f"HTTP {e.code}") from e
        if e.code in (401, 403):
            raise AuthError(f"HTTP {e.code}") from e
        raise FatalHTTP(f"HTTP {e.code}") from e
    except urllib.error.URLError as e:
        raise RetryableError(str(getattr(e, "reason", e))) from e
    except (TimeoutError, ConnectionError, OSError) as e:
        raise RetryableError(str(e)) from e


# ---------------------------------------------------------------------------
# Dinamik semafor - aktif es zamanli istek sayisini canli degistirir
# ---------------------------------------------------------------------------
class DynamicSemaphore:
    def __init__(self, initial):
        self._cap = max(1, int(initial))
        self._used = 0
        self._cond = threading.Condition()

    @property
    def cap(self):
        return self._cap

    def set_cap(self, n):
        with self._cond:
            self._cap = max(1, min(int(n), HARD_MAX_CONN))
            self._cond.notify_all()

    def acquire(self, stop_check):
        with self._cond:
            while self._used >= self._cap:
                if stop_check():
                    return False
                self._cond.wait(0.2)
            self._used += 1
            return True

    def release(self):
        with self._cond:
            self._used -= 1
            self._cond.notify_all()


# ---------------------------------------------------------------------------
# Tek indirme isi
# ---------------------------------------------------------------------------
class Job:
    def __init__(self, jid, manifest_url, out_dir, out_name, headers,
                 quality="best", user_max=16, page_url=None, retries=6, timeout=30,
                 start_sec=None, end_sec=None, subs_lang=""):
        self.id = jid
        self.manifest_url = manifest_url
        self.page_url = page_url
        self.out_dir = out_dir
        self.name = sanitize_filename(out_name)
        self.quality = quality
        self.start_sec = start_sec      # sadece bu saniyeden itibaren indir
        self.end_sec = end_sec          # bu saniyeye kadar (izlenen kismi atlamak icin)
        self.subs_lang = subs_lang or ""  # altyazi dili (bos=altyazi yok)
        self._sub_tracks = []           # master'daki altyazi izleri
        self.user_max = max(1, min(int(user_max), HARD_MAX_CONN))
        self.retries = retries
        self.timeout = timeout

        self._headers = dict(headers)
        self._hlock = threading.Lock()

        # durum
        self.state = "queued"       # queued|running|paused|muxing|done|error|canceled
        self.total = 0
        self.done = 0
        self.bytes = 0
        self.speed = 0.0
        self.eta = 0
        self.error = ""
        self.error_code = ""        # i18n icin kod (drm/token/no_segments/...)
        self.out_path = ""
        self.renditions = []        # UI'da gostermek icin (cozunurlukler)

        # adaptif paralellik
        self.start_c = max(2, min(self.user_max, 16))
        self.grow_max = max(self.start_c, min(self.user_max, HARD_MAX_CONN))
        self.target_c = self.start_c
        self.throttled = False
        self._throttle_events = 0

        # ic durum
        self._sem = DynamicSemaphore(self.start_c)
        self._lock = threading.Lock()
        self._go = threading.Event(); self._go.set()      # set=devam, clear=duraklat
        self._cancel = threading.Event()
        self._failures = []
        self._fatal = ""
        self._auto_retries = 0      # token bitince otomatik yeniden algilama sayaci
        self._cooldown_until = 0.0
        self._success_streak = 0
        self._aes = None
        self._keys = {}
        self._start_ts = 0.0
        self._last_ts = 0.0
        self._last_bytes = 0

    # ---- kamuya acik kontrol ----
    def pause(self):
        if self.state in ("running",):
            self._go.clear()
            self.state = "paused"

    def resume(self):
        if self.state == "paused":
            self._go.set()
            self.state = "running"

    def cancel(self):
        self._cancel.set()
        self._go.set()
        self.state = "canceled"

    def to_dict(self):
        pct = (self.done / self.total * 100) if self.total else 0
        elapsed = int(time.time() - self._start_ts) if self._start_ts else 0
        est_mb = round(self.bytes / self.done * self.total / 1048576) if self.done else 0
        return {
            "id": self.id, "name": self.name, "state": self.state,
            "done": self.done, "total": self.total, "percent": round(pct, 1),
            "bytes": self.bytes, "mb": round(self.bytes / 1048576, 1),
            "est_mb": est_mb, "elapsed": elapsed, "dir": self.out_dir,
            "speed_mbps": round(self.speed / 1048576, 2),
            "eta": int(self.eta), "conn": self._sem.cap,
            "throttled": self.throttled, "quality": self.quality,
            "error": self.error, "error_code": self.error_code,
            "out_path": self.out_path,
            "page_url": self.page_url, "range": [self.start_sec, self.end_sec],
        }

    def _set_error(self, code, raw=""):
        self.state = "error"
        self.error_code = code
        self.error = raw or code

    # ---- ic yardimcilar ----
    def _stop(self):
        return self._cancel.is_set()

    def _wait_if_paused(self):
        while not self._go.is_set():
            if self._cancel.is_set():
                return
            self._go.wait(0.2)

    def _on_throttle(self):
        with self._lock:
            self._throttle_events += 1
            new = max(2, self._sem.cap // 2)
            self._sem.set_cap(new)
            self.target_c = new
            self._cooldown_until = time.time() + 3.0
            self.throttled = True
            self._success_streak = 0

    def _on_success(self):
        self._success_streak += 1
        if time.time() > self._cooldown_until:
            self.throttled = False
        if self._success_streak >= 15 and self._sem.cap < self.grow_max:
            self._sem.set_cap(self._sem.cap + 1)
            self.target_c = self._sem.cap
            self._success_streak = 0

    def _calc(self):
        now = time.time()
        dt = now - self._last_ts
        if dt >= 0.5:
            rate = (self.bytes - self._last_bytes) / dt
            self.speed = rate if self.speed == 0 else 0.55 * self.speed + 0.45 * rate
            self._last_bytes = self.bytes
            self._last_ts = now
        el = max(now - self._start_ts, 1e-6)
        if self.done:
            self.eta = int((self.total - self.done) * (el / self.done))

    def _get(self, url, byterange=None):
        """Tekrar denemeli, tipli-hata farkindali, UA-yedekli indirme."""
        last = None
        for attempt in range(1, self.retries + 1):
            if self._stop():
                raise RetryableError("iptal")
            cd = self._cooldown_until - time.time()
            if cd > 0:
                time.sleep(min(cd, 5))
            with self._hlock:
                hdrs = dict(self._headers)
            try:
                return fetch_bytes(url, hdrs, self.timeout, byterange)
            except ThrottleError as e:
                last = e
                self._on_throttle()
                time.sleep(min(2 ** attempt, 10) + random.random())
            except AuthError as e:
                last = e
                data = self._ua_fallback(url, byterange, hdrs.get("User-Agent"))
                if data is not None:
                    return data
                if attempt >= 2:
                    raise
                time.sleep(1 + random.random())
            except RetryableError as e:
                last = e
                time.sleep(min(2 ** attempt, 8) + random.random())
            except FatalHTTP:
                raise
        raise RetryableError(f"{self.retries} denemede olmadi ({last})")

    def _ua_fallback(self, url, byterange, current_ua):
        for ua in UA_FALLBACKS:
            if ua == current_ua:
                continue
            try:
                with self._hlock:
                    h = dict(self._headers)
                h["User-Agent"] = ua
                data = fetch_bytes(url, h, self.timeout, byterange)
                with self._hlock:            # calisan UA'yi tum ise yay
                    self._headers["User-Agent"] = ua
                return data
            except Exception:
                continue
        return None

    def _get_key(self, uri):
        with self._lock:
            if uri in self._keys:
                return self._keys[uri]
        data = self._get(uri)
        with self._lock:
            self._keys[uri] = data
        return data

    # ---- ana akis ----
    def run(self):
        try:
            self._start_ts = self._last_ts = time.time()
            self.state = "running"
            self.error = ""; self.error_code = ""

            text = self._get(self.manifest_url).decode("utf-8", "replace")
            rends = parse_master(text, self.manifest_url)
            self._sub_tracks = parse_subtitles(text, self.manifest_url)
            if rends:
                self.renditions = [
                    {"height": r.height, "bandwidth": r.bandwidth,
                     "resolution": r.resolution} for r in sorted(
                        rends, key=lambda r: (r.height, r.bandwidth))]
                chosen = pick_rendition(rends, self.quality)
                media_text = self._get(chosen.url).decode("utf-8", "replace")
                media_base = chosen.url
            else:
                media_text = text
                media_base = self.manifest_url

            media = parse_media(media_text, media_base)
            if not media.segments:
                self._set_error("no_segments"); return

            self._apply_range(media)      # izlenen kismi atla (aralik secildiyse)
            self.total = len(media.segments)
            if not media.segments:
                self._set_error("range_empty"); return
            self._download(media)

            if self._stop():
                return
            if self._fatal:
                self._set_error(self._fatal); return
            if self._failures:
                self._set_error("parts_failed",
                                f"{len(self._failures)} parca inemedi"); return

            self._assemble_and_mux(media)
            self.state = "done"
        except DRMError:
            self._set_error("drm")
        except AuthError:
            self._set_error("token")
        except Exception as e:  # noqa: BLE001
            self._set_error("", str(e))

    def _apply_range(self, media):
        """start_sec/end_sec verildiyse sadece o zaman araligindaki parcalari birak."""
        if self.start_sec is None and self.end_sec is None:
            return
        lo = self.start_sec or 0
        hi = self.end_sec if self.end_sec else float("inf")
        sel, t = [], 0.0
        for s in media.segments:
            seg_start, seg_end = t, t + (s.duration or 0)
            if seg_end > lo and seg_start < hi:      # araliklar kesisiyorsa al
                sel.append(s)
            t = seg_end
        media.segments = sel

    def _parts_dir(self):
        tag = ""
        if self.start_sec is not None or self.end_sec is not None:
            tag = f".{int(self.start_sec or 0)}-{int(self.end_sec or 0)}"
        return os.path.join(self.out_dir, f".{self.name}{tag}.parts")

    def _download(self, media):
        parts_dir = self._parts_dir()
        os.makedirs(parts_dir, exist_ok=True)
        if any(s.key for s in media.segments):
            self._aes = get_aes_decryptor()

        # fMP4 init segmenti
        self._map_path = None
        if media.map_uri:
            self._map_path = os.path.join(parts_dir, "init.mp4")
            if not (os.path.exists(self._map_path) and os.path.getsize(self._map_path)):
                with open(self._map_path, "wb") as f:
                    f.write(self._get(media.map_uri))

        maxidx = max((s.index for s in media.segments), default=0)
        width = max(4, len(str(maxidx)))
        ext = "m4s" if media.is_fmp4 else "ts"
        self._ordered = []                 # birlestirme sirasi (dosya yollari)
        pairs = []
        for s in media.segments:
            dest = os.path.join(parts_dir, f"seg-{s.index:0{width}d}.{ext}")
            self._ordered.append(dest)
            pairs.append((s, dest))

        workers = self.grow_max
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = [pool.submit(self._seg_worker, s, dest) for s, dest in pairs]
            for f in futs:
                f.result()

    def _seg_worker(self, seg, dest):
        if self._stop():
            return
        self._wait_if_paused()
        if self._stop():
            return
        # resume: bitmis parcayi atla
        if os.path.exists(dest) and os.path.getsize(dest) > 0:
            with self._lock:
                self.done += 1
                self.bytes += os.path.getsize(dest)
                self._calc()
            return
        if not self._sem.acquire(self._stop):
            return
        try:
            data = self._get(seg.url, seg.byterange)
            if seg.key is not None:
                if self._aes is None:
                    raise RuntimeError("AES kutuphanesi yok (pip install pycryptodome)")
                data = self._aes(data, self._get_key(seg.key.uri), seg.key.iv)
            tmp = dest + ".part"
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, dest)
            with self._lock:
                self.done += 1
                self.bytes += len(data)
                self._on_success()
                self._calc()
        except AuthError:
            with self._lock:
                self._fatal = "token"
            self._cancel.set()
        except Exception as e:  # noqa: BLE001
            with self._lock:
                self._failures.append((seg.index, str(e)))
        finally:
            self._sem.release()

    def _unique_path(self, path):
        """Dosya varsa uzerine YAZMA; ' (2)', ' (3)'... ekleyerek bos ad bul."""
        if not os.path.exists(path):
            return path
        base, ext = os.path.splitext(path)
        i = 2
        while os.path.exists(f"{base} ({i}){ext}"):
            i += 1
        return f"{base} ({i}){ext}"

    def _assemble_and_mux(self, media):
        self.state = "muxing"
        parts_dir = self._parts_dir()
        # Ara birlestirme dosyasi is klasorunun ICINDE (cakisma olmaz)
        raw = os.path.join(parts_dir, "_merged." + ("mp4" if media.is_fmp4 else "ts"))
        with open(raw, "wb") as out:
            if getattr(self, "_map_path", None):
                with open(self._map_path, "rb") as f:
                    shutil.copyfileobj(f, out, 1 << 20)
            for d in self._ordered:
                with open(d, "rb") as f:
                    shutil.copyfileobj(f, out, 1 << 20)

        ff = find_ffmpeg()
        sub_srt = self._fetch_subs() if self.subs_lang else None
        if ff and not raw.endswith(".mp4"):
            final = self._unique_path(os.path.join(self.out_dir, self.name + ".mp4"))
            if not self._remux(ff, raw, final):
                final = self._unique_path(os.path.join(self.out_dir, self.name + ".ts"))
                shutil.move(raw, final)          # remux olmadi -> .ts (VLC oynar)
        else:
            ext = ".mp4" if raw.endswith(".mp4") else ".ts"
            final = self._unique_path(os.path.join(self.out_dir, self.name + ext))
            shutil.move(raw, final)              # ffmpeg yok ya da zaten mp4

        # Altyaziyi mp4'e gom (ikinci gecis; olmazsa harici .srt zaten yanda kalir)
        if sub_srt and ff and final.lower().endswith(".mp4"):
            self._embed_subs(ff, final, sub_srt)
        self.out_path = final

        shutil.rmtree(parts_dir, ignore_errors=True)

    def _remux(self, ff, src, dst):
        for extra in (["-bsf:a", "aac_adtstoasc"], []):
            cmd = [ff, "-y", "-loglevel", "error", "-i", src, "-c", "copy"] + extra + [dst]
            try:
                subprocess.run(cmd, check=True,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                return True
            except Exception:
                continue
        return False

    def _embed_subs(self, ff, mp4, srt):
        """Uretilen mp4'e altyaziyi (mov_text) gomer; olmazsa dosyayi bozmadan birakir."""
        tmp = mp4 + ".sub.mp4"
        cmd = [ff, "-y", "-loglevel", "error", "-i", mp4, "-i", srt,
               "-map", "0", "-map", "1", "-c", "copy", "-c:s", "mov_text", tmp]
        try:
            subprocess.run(cmd, check=True,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if os.path.exists(tmp) and os.path.getsize(tmp) > 0:
                os.replace(tmp, mp4)
                return
        except Exception:
            pass
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass

    def _fetch_subs(self):
        """Secilen dildeki altyaziyi PYTHON ile indirir (ffmpeg'in HLS-altyazi
        yolu bazi statik derlemelerde cokuyor). WebVTT segmentlerini birlestirip
        harici .vtt olarak kaydeder; yolunu doner. Token'li altyazilarda self._get
        Referer/UA'yi da gonderir."""
        tracks = self._sub_tracks or []
        if not tracks or not self.subs_lang:
            return None
        track = next((x for x in tracks
                      if x["lang"].lower().startswith(self.subs_lang.lower())), tracks[0])
        try:
            pl = self._get(track["uri"]).decode("utf-8", "replace")
            if "#EXTINF" in pl:                       # altyazi playlist'i (.vtt segmentleri)
                media = parse_media(pl, track["uri"])
                parts = [self._get(s.url).decode("utf-8", "replace")
                         for s in media.segments]
            else:                                     # dogrudan tek .vtt
                parts = [pl]
            merged = self._merge_vtt(parts)
            if not merged.strip() or "-->" not in merged:
                return None
            suffix = ("." + track["lang"]) if track["lang"] else ""
            vtt = self._unique_path(os.path.join(self.out_dir, self.name + suffix + ".vtt"))
            with open(vtt, "w", encoding="utf-8") as f:
                f.write(merged)
            return vtt
        except Exception:
            return None

    @staticmethod
    def _merge_vtt(parts):
        """Segmentli WebVTT parcalarini tek .vtt'de birlestirir (basliklari atlar)."""
        out = ["WEBVTT", ""]
        for p in parts:
            lines = p.replace("\r\n", "\n").replace("\r", "\n").split("\n")
            i = 0
            if lines and lines[0].lstrip("﻿").startswith("WEBVTT"):
                i = 1
            while i < len(lines) and lines[i].strip() != "":   # bas meta blogunu atla
                i += 1
            body = "\n".join(lines[i:]).strip()
            if body:
                out.append(body)
                out.append("")
        return "\n".join(out).strip() + "\n"
