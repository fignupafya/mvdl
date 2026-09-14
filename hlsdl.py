#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
hlsdl - Basit, dayanikli, paralel HLS (m3u8) indirici / indirme yoneticisi.

Ne yapar:
  * DRM'siz HLS akislarini indirir (DRM'siz video / yayin siteleri).
  * Parcalari PARALEL indirir (varsayilan 16 baglanti).
  * KESINTIYE DAYANIKLI: kaldigi yerden devam eder (resume), her parca icin
    otomatik yeniden dener (exponential backoff).
  * Standart AES-128 sifrelemesini destekler (pycryptodome ya da cryptography
    kuruluysa). Bu DRM DEGILDIR; anahtar HTTP uzerinden gelir.
  * DRM (Widevine / FairPlay / SAMPLE-AES) TESPIT EDER VE REDDEDER - kirmaz.
  * ffmpeg varsa temiz .mp4 uretir; yoksa .ts birakir (VLC ile oynar).

Sadece Python standart kutuphanesi gerekir (AES haric). Python 3.8+.

Kullanim:
  py hlsdl.py "MASTER_YA_DA_MEDIA_M3U8_URL" -o cikti.mp4
  py hlsdl.py "URL" -o cikti.mp4 -n 16 --quality best --referer https://example.com/
  py hlsdl.py "URL" --list                 # mevcut kaliteleri listele, indirme
  py hlsdl.py "URL" -o cikti.mp4 --quality 720   # 720p sec

Ipucu: m3u8 adresini tarayicinin gelistirici araclari (F12) -> Network sekmesinde
".m3u8" diye aratarak bulabilirsin. Videoyu oynat, master.m3u8 istegini kopyala.
"""

import argparse
import os
import re
import sys
import time
import threading
import random
import shutil
import subprocess
import urllib.request
import urllib.error
from urllib.parse import urljoin
from concurrent.futures import ThreadPoolExecutor, as_completed

# Not: Bazi CDN'lerin bot filtresi TAM tarayici UA'sini
# (Chrome/Firefox/... adi geceni) bot sayip 403 dondurur; sade "Mozilla/5.0"
# en genis uyumlulugu verir. Gerekirse --user-agent ile degistir.
DEFAULT_UA = "Mozilla/5.0"

# 403 alinirsa sirayla denenecek yedek UA'lar (siteye gore hangisi calisirsa)
UA_FALLBACKS = [
    "Mozilla/5.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.6778.140 Safari/537.36",
    "VLC/3.0.20 LibVLC/3.0.20",
]


# ---------------------------------------------------------------------------
# Hatalar
# ---------------------------------------------------------------------------
class DRMError(Exception):
    """Akis DRM ile korunuyor - indirilemez."""


class SegmentError(Exception):
    """Bir parca tum denemelere ragmen indirilemedi."""


# ---------------------------------------------------------------------------
# HTTP - yeniden denemeli, header'li, gerekirse byte-range'li indirme
# ---------------------------------------------------------------------------
_UA_LOCK = threading.Lock()


def http_get(url, headers, timeout=30, retries=5, backoff=1.5, byterange=None):
    """URL'yi indirir, bytes dondurur. Basarisizsa yeniden dener.

    byterange: (start, length) verilirse sadece o araligi ister (Range header).
    403/401 alinirsa UA kaynakli olabilir diye yedek UA'lari dener; calisan UA'yi
    paylasilan `headers` sozlugune yazar ki sonraki istekler dogrudan onu kullansin.
    """
    last_exc = None
    hdrs = dict(headers)
    if byterange is not None:
        start, length = byterange
        hdrs["Range"] = f"bytes={start}-{start + length - 1}"

    def fetch(extra_ua=None):
        h = dict(hdrs)
        if extra_ua is not None:
            h["User-Agent"] = extra_ua
        req = urllib.request.Request(url, headers=h, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()

    for attempt in range(retries):
        try:
            return fetch()
        except urllib.error.HTTPError as e:
            last_exc = e
            if e.code in (401, 403):
                # UA kaynakli olabilir: yedek UA'lari sirayla dene
                cur = hdrs.get("User-Agent")
                for ua in UA_FALLBACKS:
                    if ua == cur:
                        continue
                    try:
                        data = fetch(ua)
                        hdrs["User-Agent"] = ua
                        with _UA_LOCK:      # calisan UA'yi tum oturuma yay
                            headers["User-Agent"] = ua
                        return data
                    except Exception:       # noqa: BLE001 - sonraki UA'yi dene
                        continue
                if attempt >= 1:
                    raise SegmentError(
                        f"HTTP {e.code} (token suresi dolmus ya da UA/yetki reddi): "
                        f"{url[:90]}...") from e
            elif e.code == 410:
                raise SegmentError(
                    f"HTTP 410 (token suresi doldu): {url[:90]}...") from e
            elif e.code in (400, 404):
                raise SegmentError(f"HTTP {e.code}: {url[:90]}...") from e
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            last_exc = e
        if attempt < retries - 1:
            time.sleep(backoff ** attempt + random.uniform(0, 0.4))

    raise SegmentError(f"{retries} denemede indirilemedi: {url[:90]}... ({last_exc})")


def http_get_text(url, headers, timeout=30, retries=5):
    return http_get(url, headers, timeout=timeout, retries=retries).decode(
        "utf-8", "replace"
    )


# ---------------------------------------------------------------------------
# m3u8 ayristirma
# ---------------------------------------------------------------------------
def parse_attributes(s):
    """`KEY=VAL,KEY="VAL,ic virgul",KEY=0x..` bicimini dict'e cevirir."""
    attrs = {}
    i, n = 0, len(s)
    while i < n:
        eq = s.find("=", i)
        if eq < 0:
            break
        key = s[i:eq].strip()
        if eq + 1 < n and s[eq + 1] == '"':
            end = s.find('"', eq + 2)
            if end < 0:
                end = n
            val = s[eq + 2:end]
            i = end + 1
            if i < n and s[i] == ",":
                i += 1
        else:
            end = s.find(",", eq + 1)
            if end < 0:
                end = n
            val = s[eq + 1:end]
            i = end + 1
        attrs[key] = val
    return attrs


class Rendition:
    def __init__(self, url, bandwidth=0, resolution="", codecs=""):
        self.url = url
        self.bandwidth = bandwidth
        self.resolution = resolution
        self.codecs = codecs

    @property
    def height(self):
        m = re.search(r"x(\d+)", self.resolution)
        return int(m.group(1)) if m else 0

    def __str__(self):
        res = self.resolution or "?"
        mbps = self.bandwidth / 1_000_000 if self.bandwidth else 0
        return f"{res:>11}  {mbps:5.2f} Mbps  {self.codecs}"


def parse_master(text, base_url):
    """Master playlist -> Rendition listesi. Master degilse bos liste."""
    rends = []
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("#EXT-X-STREAM-INF:"):
            attrs = parse_attributes(line[len("#EXT-X-STREAM-INF:"):])
            # bir sonraki bos olmayan / yorum olmayan satir URI'dir
            uri = ""
            for j in range(i + 1, len(lines)):
                cand = lines[j].strip()
                if cand and not cand.startswith("#"):
                    uri = cand
                    break
            if uri:
                rends.append(Rendition(
                    url=urljoin(base_url, uri),
                    bandwidth=int(attrs.get("BANDWIDTH", attrs.get("AVERAGE-BANDWIDTH", 0)) or 0),
                    resolution=attrs.get("RESOLUTION", ""),
                    codecs=attrs.get("CODECS", ""),
                ))
    return rends


def parse_subtitles(text, base_url):
    """Master'daki altyazi izleri: [{'lang','name','uri'}] (WebVTT playlist'leri)."""
    tracks = []
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("#EXT-X-MEDIA:") and "TYPE=SUBTITLES" in s:
            a = parse_attributes(s[len("#EXT-X-MEDIA:"):])
            uri = a.get("URI", "")
            if uri:
                tracks.append({
                    "lang": a.get("LANGUAGE", ""),
                    "name": a.get("NAME", a.get("LANGUAGE", "sub")),
                    "uri": urljoin(base_url, uri),
                })
    return tracks


class Key:
    def __init__(self, method, uri, iv):
        self.method = method      # NONE, AES-128, SAMPLE-AES, ...
        self.uri = uri
        self.iv = iv              # bytes(16) ya da None


class Segment:
    def __init__(self, index, url, duration, key, byterange, seq):
        self.index = index
        self.url = url
        self.duration = duration
        self.key = key                # Key ya da None
        self.byterange = byterange    # (start, length) ya da None
        self.seq = seq                # media sequence numarasi (IV icin)


class Media:
    def __init__(self):
        self.segments = []
        self.map_uri = None           # fMP4 init segmenti (#EXT-X-MAP)
        self.total_duration = 0.0
        self.is_fmp4 = False


def _parse_iv(hexstr):
    hexstr = hexstr.strip()
    if hexstr.lower().startswith("0x"):
        hexstr = hexstr[2:]
    return bytes.fromhex(hexstr.rjust(32, "0"))


def parse_media(text, base_url):
    """Media playlist -> Media. DRM tespit ederse DRMError firlatir."""
    media = Media()
    lines = text.splitlines()
    cur_key = None
    seq = 0
    for line in lines:
        if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            seq = int(line.split(":", 1)[1].strip() or "0")
    idx = 0
    running_seq = seq
    pending_duration = 0.0
    pending_range = None
    last_range_end = 0

    for line in lines:
        s = line.strip()
        if s.startswith("#EXT-X-KEY:"):
            attrs = parse_attributes(s[len("#EXT-X-KEY:"):])
            method = attrs.get("METHOD", "NONE")
            uri = attrs.get("URI", "")
            if method == "NONE":
                cur_key = None
                continue
            # --- DRM kontrolu ---
            if method in ("SAMPLE-AES", "SAMPLE-AES-CTR", "SAMPLE-AES-CENC"):
                raise DRMError(
                    f"Akis '{method}' ile korunuyor (FairPlay/DRM). Indirilemez."
                )
            if uri.startswith("skd://") or uri.startswith("data:") and "widevine" in uri:
                raise DRMError("Widevine/FairPlay DRM tespit edildi. Indirilemez.")
            if method != "AES-128":
                raise DRMError(f"Desteklenmeyen sifreleme METHOD={method}.")
            iv = _parse_iv(attrs["IV"]) if "IV" in attrs else None
            cur_key = Key("AES-128", urljoin(base_url, uri), iv)
        elif s.startswith("#EXT-X-MAP:"):
            attrs = parse_attributes(s[len("#EXT-X-MAP:"):])
            if "URI" in attrs:
                media.map_uri = urljoin(base_url, attrs["URI"])
                media.is_fmp4 = True
        elif s.startswith("#EXTINF:"):
            dur = s[len("#EXTINF:"):].split(",", 1)[0]
            try:
                pending_duration = float(dur)
            except ValueError:
                pending_duration = 0.0
        elif s.startswith("#EXT-X-BYTERANGE:"):
            spec = s[len("#EXT-X-BYTERANGE:"):]
            if "@" in spec:
                length_s, offset_s = spec.split("@", 1)
                pending_range = (int(offset_s), int(length_s))
            else:
                pending_range = (last_range_end, int(spec))
            last_range_end = pending_range[0] + pending_range[1]
        elif s and not s.startswith("#"):
            key_for_iv = cur_key
            if cur_key is not None and cur_key.iv is None:
                # IV verilmemisse media sequence numarasindan uret
                key_for_iv = Key(cur_key.method, cur_key.uri,
                                 running_seq.to_bytes(16, "big"))
            media.segments.append(Segment(
                index=idx,
                url=urljoin(base_url, s),
                duration=pending_duration,
                key=key_for_iv,
                byterange=pending_range,
                seq=running_seq,
            ))
            media.total_duration += pending_duration
            idx += 1
            running_seq += 1
            pending_duration = 0.0
            pending_range = None
    return media


# ---------------------------------------------------------------------------
# AES-128 cozucu (opsiyonel - sadece sifreli akislar icin gerekir)
# ---------------------------------------------------------------------------
def get_aes_decryptor():
    """Kuruluysa bir AES-128-CBC cozme fonksiyonu dondurur, yoksa None."""
    try:
        from Crypto.Cipher import AES  # pycryptodome

        def dec(data, key, iv):
            out = AES.new(key, AES.MODE_CBC, iv).decrypt(data)
            return _strip_pkcs7(out)
        return dec
    except Exception:
        pass
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

        def dec(data, key, iv):
            c = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
            return _strip_pkcs7(c.update(data) + c.finalize())
        return dec
    except Exception:
        return None


def _strip_pkcs7(data):
    if not data:
        return data
    pad = data[-1]
    if 1 <= pad <= 16 and data[-pad:] == bytes([pad]) * pad:
        return data[:-pad]
    return data


# ---------------------------------------------------------------------------
# Ilerleme gostergesi
# ---------------------------------------------------------------------------
class Progress:
    def __init__(self, total):
        self.total = total
        self.done = 0
        self.bytes = 0
        self.start = time.time()
        self.lock = threading.Lock()
        self._last_print = 0.0

    def add(self, nbytes):
        with self.lock:
            self.done += 1
            self.bytes += nbytes
            now = time.time()
            if now - self._last_print > 0.15 or self.done == self.total:
                self._last_print = now
                self._print()

    def _print(self):
        elapsed = max(time.time() - self.start, 1e-6)
        speed = self.bytes / elapsed
        pct = self.done / self.total * 100 if self.total else 0
        mb = self.bytes / 1_048_576
        eta = (self.total - self.done) / (self.done / elapsed) if self.done else 0
        bar_len = 28
        filled = int(bar_len * self.done / self.total) if self.total else 0
        bar = "#" * filled + "-" * (bar_len - filled)
        sys.stderr.write(
            f"\r[{bar}] {self.done}/{self.total} ({pct:4.1f}%)  "
            f"{mb:7.1f} MB  {speed / 1_048_576:4.1f} MB/s  ETA {int(eta):4d}s "
        )
        sys.stderr.flush()

    def finish(self):
        self._print()
        sys.stderr.write("\n")
        sys.stderr.flush()


# ---------------------------------------------------------------------------
# Indirme motoru
# ---------------------------------------------------------------------------
class Downloader:
    def __init__(self, headers, concurrency=16, retries=5, timeout=30):
        self.headers = headers
        self.concurrency = concurrency
        self.retries = retries
        self.timeout = timeout
        self._key_cache = {}
        self._key_lock = threading.Lock()
        self._aes = None
        self._stop = threading.Event()

    def _fetch_key(self, uri):
        with self._key_lock:
            if uri in self._key_cache:
                return self._key_cache[uri]
        data = http_get(uri, self.headers, timeout=self.timeout, retries=self.retries)
        with self._key_lock:
            self._key_cache[uri] = data
        return data

    def _download_one(self, seg, dest, progress):
        # Resume: tamamlanmis parca varsa atla
        if os.path.exists(dest) and os.path.getsize(dest) > 0:
            progress.add(os.path.getsize(dest))
            return
        if self._stop.is_set():
            raise SegmentError("iptal")

        data = http_get(seg.url, self.headers, timeout=self.timeout,
                        retries=self.retries, byterange=seg.byterange)

        if seg.key is not None:
            if self._aes is None:
                raise SegmentError(
                    "Akis AES-128 sifreli ama AES kutuphanesi yok. "
                    "Kur: py -m pip install pycryptodome"
                )
            key = self._fetch_key(seg.key.uri)
            data = self._aes(data, key, seg.key.iv)

        tmp = dest + ".part"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, dest)          # atomik: yarim dosya birakma
        progress.add(len(data))

    def download(self, media, parts_dir, ext):
        os.makedirs(parts_dir, exist_ok=True)

        # Sifreli mi? AES cozucuyu hazirla
        if any(s.key is not None for s in media.segments):
            self._aes = get_aes_decryptor()

        # init segment (fMP4)
        map_path = None
        if media.map_uri:
            map_path = os.path.join(parts_dir, "init.mp4")
            if not (os.path.exists(map_path) and os.path.getsize(map_path) > 0):
                data = http_get(media.map_uri, self.headers,
                               timeout=self.timeout, retries=self.retries)
                with open(map_path, "wb") as f:
                    f.write(data)

        progress = Progress(len(media.segments))
        width = len(str(len(media.segments)))
        dests = {}
        failures = []

        with ThreadPoolExecutor(max_workers=self.concurrency) as pool:
            futs = {}
            for seg in media.segments:
                dest = os.path.join(parts_dir, f"seg-{seg.index:0{width}d}.{ext}")
                dests[seg.index] = dest
                futs[pool.submit(self._download_one, seg, dest, progress)] = seg
            try:
                for fut in as_completed(futs):
                    seg = futs[fut]
                    try:
                        fut.result()
                    except Exception as e:  # noqa: BLE001
                        failures.append((seg.index, str(e)))
                        # Token suresi dolmussa erken dur
                        if isinstance(e, SegmentError) and "token" in str(e).lower():
                            self._stop.set()
            except KeyboardInterrupt:
                self._stop.set()
                progress.finish()
                print("\n[!] Durduruldu. Ilerleme kaydedildi - ayni komutla "
                      "kaldigi yerden devam eder.", file=sys.stderr)
                raise

        progress.finish()
        if failures:
            failures.sort()
            n = len(failures)
            print(f"\n[!] {n} parca indirilemedi. Ornek: "
                  f"#{failures[0][0]} -> {failures[0][1]}", file=sys.stderr)
            print("[!] Cikti olusturulmadi. Sebep token suresi dolmasi ise "
                  "m3u8 adresini tarayicidan yeniden al ve komutu tekrar calistir "
                  "(inen parcalar korunur, sadece eksikler iner).", file=sys.stderr)
            return None, None

        ordered = [dests[i] for i in range(len(media.segments))]
        return ordered, map_path


# ---------------------------------------------------------------------------
# Birlestirme + ffmpeg ile remux
# ---------------------------------------------------------------------------
def assemble(parts, map_path, raw_out):
    """Parcalari sirayla tek dosyada birlestirir."""
    with open(raw_out, "wb") as out:
        if map_path:
            with open(map_path, "rb") as f:
                shutil.copyfileobj(f, out, length=1024 * 1024)
        for p in parts:
            with open(p, "rb") as f:
                shutil.copyfileobj(f, out, length=1024 * 1024)


def remux_to_mp4(raw_ts, mp4_out):
    """ffmpeg varsa .ts -> .mp4 (yeniden kodlamadan, hizli)."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return False
    cmd = [ffmpeg, "-y", "-loglevel", "error", "-i", raw_ts,
           "-c", "copy", "-bsf:a", "aac_adtstoasc", mp4_out]
    try:
        subprocess.run(cmd, check=True)
        return True
    except subprocess.CalledProcessError:
        # Bazi akislarda bsf gereksiz/hatali olabilir; sade dene
        try:
            subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-i", raw_ts,
                            "-c", "copy", mp4_out], check=True)
            return True
        except subprocess.CalledProcessError:
            return False


# ---------------------------------------------------------------------------
# Kalite secimi
# ---------------------------------------------------------------------------
def pick_rendition(rends, quality):
    rends = sorted(rends, key=lambda r: (r.height, r.bandwidth))
    if quality == "best":
        return rends[-1]
    if quality == "worst":
        return rends[0]
    # sayisal yukseklik (or. "720") -> en yakin <= hedef, yoksa en dusuk
    try:
        target = int(quality)
    except ValueError:
        return rends[-1]
    le = [r for r in rends if r.height <= target]
    return (le[-1] if le else rends[0])


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def build_headers(args):
    headers = {
        "User-Agent": args.user_agent,
        "Accept": "*/*",
        "Accept-Encoding": "identity",   # gzip'i kapat, urllib acmaz
    }
    if args.referer:
        headers["Referer"] = args.referer
        # Origin, referer'dan turet
        m = re.match(r"(https?://[^/]+)", args.referer)
        if m:
            headers["Origin"] = m.group(1)
    for h in args.header or []:
        if ":" in h:
            k, v = h.split(":", 1)
            headers[k.strip()] = v.strip()
    return headers


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Dayanikli, paralel HLS (m3u8) indirici. DRM'siz akislar icin.")
    ap.add_argument("url", help="master.m3u8 ya da media stream.m3u8 adresi")
    ap.add_argument("-o", "--output", default=None,
                    help="cikti dosyasi (or. bolum.mp4). Varsayilan: video.mp4")
    ap.add_argument("-n", "--concurrency", type=int, default=16,
                    help="es zamanli indirme sayisi (varsayilan 16)")
    ap.add_argument("--quality", default="best",
                    help="best | worst | yukseklik (or. 720). Varsayilan best")
    ap.add_argument("--referer", default=None, help="Referer header'i")
    ap.add_argument("--header", action="append",
                    help="ek header 'Ad: Deger' (tekrarlanabilir)")
    ap.add_argument("--user-agent", default=DEFAULT_UA)
    ap.add_argument("--retries", type=int, default=5)
    ap.add_argument("--timeout", type=int, default=30)
    ap.add_argument("--list", action="store_true",
                    help="mevcut kaliteleri listele ve cik")
    ap.add_argument("--keep", action="store_true",
                    help="birlestirdikten sonra parca dosyalarini silme")
    args = ap.parse_args(argv)

    headers = build_headers(args)

    if args.url.lower().split("?")[0].endswith(".mpd"):
        print("[!] Bu bir DASH (.mpd) akisi. Bu arac sadece HLS (.m3u8) destekler.\n"
              "    DASH icin yt-dlp oneririm: yt-dlp \"<mpd-url>\"", file=sys.stderr)
        return 2

    print(f"[*] Manifest aliniyor: {args.url[:80]}...", file=sys.stderr)
    try:
        text = http_get_text(args.url, headers, timeout=args.timeout,
                             retries=args.retries)
    except SegmentError as e:
        print(f"[HATA] Manifest alinamadi: {e}", file=sys.stderr)
        return 1

    rends = parse_master(text, args.url)
    if rends:
        rends_sorted = sorted(rends, key=lambda r: (r.height, r.bandwidth))
        print("[*] Bulunan kaliteler:", file=sys.stderr)
        for r in rends_sorted:
            print(f"      {r}", file=sys.stderr)
        if args.list:
            return 0
        chosen = pick_rendition(rends, args.quality)
        print(f"[*] Secilen: {chosen}", file=sys.stderr)
        media_text = http_get_text(chosen.url, headers, timeout=args.timeout,
                                   retries=args.retries)
        media_base = chosen.url
    else:
        if args.list:
            print("[*] Bu bir media playlist (tek kalite), master degil.",
                  file=sys.stderr)
            return 0
        media_text = text
        media_base = args.url

    try:
        media = parse_media(media_text, media_base)
    except DRMError as e:
        print(f"\n[DRM] {e}\n"
              "    Bu icerik korumali. Yasal olarak kiramam ve bu arac denemez.\n"
              "    Icerik saglayicinin kendi 'cevrimdisi indir' ozelligini kullan.",
              file=sys.stderr)
        return 3

    if not media.segments:
        print("[HATA] Playlist'te parca bulunamadi.", file=sys.stderr)
        return 1

    mins = media.total_duration / 60
    print(f"[*] {len(media.segments)} parca, ~{mins:.1f} dakika. Indiriliyor "
          f"({args.concurrency} paralel)...", file=sys.stderr)

    output = args.output or "video.mp4"
    base, out_ext = os.path.splitext(output)
    seg_ext = "m4s" if media.is_fmp4 else "ts"
    parts_dir = base + ".parts"

    dl = Downloader(headers, concurrency=args.concurrency,
                    retries=args.retries, timeout=args.timeout)
    try:
        parts, map_path = dl.download(media, parts_dir, seg_ext)
    except KeyboardInterrupt:
        return 130
    if parts is None:
        return 1

    # Birlestir
    raw_out = base + (".mp4" if media.is_fmp4 else ".ts")
    print(f"[*] Parcalar birlestiriliyor -> {raw_out}", file=sys.stderr)
    assemble(parts, map_path, raw_out)

    # mp4 istendiyse ve elde ham .ts varsa remux dene
    final = raw_out
    if out_ext.lower() == ".mp4" and raw_out.lower().endswith(".ts"):
        mp4_out = base + ".mp4"
        print("[*] ffmpeg ile .mp4'e cevriliyor...", file=sys.stderr)
        if remux_to_mp4(raw_out, mp4_out):
            os.remove(raw_out)
            final = mp4_out
        else:
            print("[!] ffmpeg yok/basarisiz. Dosya .ts olarak birakildi "
                  "(VLC ile oynar). ffmpeg kurarsan .mp4 uretilir.",
                  file=sys.stderr)

    # Temizlik
    if not args.keep:
        shutil.rmtree(parts_dir, ignore_errors=True)

    size_mb = os.path.getsize(final) / 1_048_576
    print(f"\n[OK] Bitti: {final}  ({size_mb:.1f} MB)", file=sys.stderr)
    print(final)   # stdout'a sadece dosya yolu (betiklerde kullanmak icin)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n[!] Iptal edildi.", file=sys.stderr)
        sys.exit(130)
