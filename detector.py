#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
detector.py - Sayfa URL'sinden m3u8 akisini OTOMATIK bulur.

Gorunmez (headless) Edge tarayicisini surer, sayfayi gercek kullanici gibi
yukler, oynaticiyi baslatir ve olusan .m3u8 isteklerini agdan yakalar.
Reklam manifestlerini eler, master/kalite bilgisini cikarir.

Kullanim (test): py detector.py "https://example.com/video-page"
"""

import re
import sys
import time
from urllib.parse import urlparse

from hlsdl import parse_master, parse_subtitles


def _relevance(cand_url, page_url):
    """Aday URL'nin sayfa URL'siyle ne kadar ortustugu (sayilar daha degerli)."""
    pt = set(re.findall(r"[a-z]+|\d+", urlparse(page_url).path.lower()))
    ct = set(re.findall(r"[a-z]+|\d+", urlparse(cand_url).path.lower()))
    return sum(3 if t.isdigit() else 1 for t in (pt & ct))

# Reklam / analitik ana bilgisayarlari (bunlardan gelen m3u8'ler icerik degildir)
AD_HOSTS = ("doubleclick", "googlesyndication", "imasdk", "2mdn", "adform",
            "moatads", "teads", "adnxs", "adsafeprotected", "yieldmo", "innovid",
            "spotxchange", "spotx", "smartadserver", "criteo", "pubmatic",
            "amazon-adsystem", "serverbid", "aniview", "springserve", "gemius")

PLAY_SELECTORS = [
    ".vjs-big-play-button", "button.vjs-big-play-button",
    "[aria-label*='Oynat']", "[aria-label*='Play']", "[title*='Oynat']",
    ".play-button", ".play", ".btn-play", ".player-play", ".jw-icon-display",
    ".plyr__control--overlaid", "button[aria-label='Play']",
]
CONSENT_TEXTS = ["Kabul Et", "Tümünü Kabul Et", "Kabul", "Accept All", "Accept",
                 "Onayla", "Anladım", "Tamam", "Kapat", "İzin Ver", "Allow all"]


def _host(url):
    return urlparse(url).netloc.lower()


def _is_ad(url):
    return any(a in _host(url) for a in AD_HOSTS)


def _base(url):
    """Token/query'siz temel yol - ayni akisin kopyalarini birlestirmek icin."""
    p = urlparse(url)
    return f"{p.netloc}{p.path}"


def _try_consent(page, log):
    for txt in CONSENT_TEXTS:
        try:
            loc = page.get_by_role("button", name=txt, exact=False)
            if loc.count() > 0:
                loc.first.click(timeout=1500)
                log(f"consent: clicked '{txt}'")
                return
        except Exception:
            pass
    # iframe icindeki consent (or. OneTrust)
    try:
        page.evaluate("""() => {
            for (const b of document.querySelectorAll('button,a')) {
                const t=(b.innerText||'').trim().toLowerCase();
                if (t.includes('kabul')||t.includes('accept')||t.includes('onayla')) { b.click(); return; }
            }
        }""")
    except Exception:
        pass


# Ana oynaticiyi bulan JS: sayfanin EN USTUNDEKI, yeterince buyuk oynatici/video
# kapsayicisini secer (asagidaki "diger bolumler" kucuk oynaticilarini degil).
_FIND_MAIN = """() => {
  const sels = ['#player','.video-js','[id*="player"]','[class*="player"]','video'];
  let best=null, bestTop=1e9;
  for (const s of sels) {
    for (const e of document.querySelectorAll(s)) {
      const r = e.getBoundingClientRect();
      if (r.width>=320 && r.height>=160 && r.top>=-50 && r.top<bestTop) { best=e; bestTop=r.top; }
    }
  }
  if (!best) return null;
  const r = best.getBoundingClientRect();
  return {x: r.left + r.width/2, y: r.top + r.height/2};
}"""

_PLAY_MAIN = """() => {
  const sels = ['#player','.video-js','[id*="player"]','[class*="player"]'];
  let cont=null, top=1e9;
  for (const s of sels) for (const e of document.querySelectorAll(s)) {
    const r=e.getBoundingClientRect();
    if (r.width>=320 && r.height>=160 && r.top<top) { cont=e; top=r.top; }
  }
  const v = (cont||document).querySelector('video') || document.querySelector('video');
  if (v) { try { v.muted=true; const p=v.play(); if (p&&p.catch) p.catch(()=>{}); } catch(e){} }
  return !!v;
}"""


def _try_play(page, log, click=True):
    # sadece ANA oynaticiyi hedefle
    try:
        page.evaluate("() => window.scrollTo(0,0)")
    except Exception:
        pass
    if click:
        try:
            box = page.evaluate(_FIND_MAIN)
        except Exception:
            box = None
        if box:
            try:
                page.mouse.click(box["x"], box["y"])   # gercek tiklama (jest)
                log(f"main player clicked @({int(box['x'])},{int(box['y'])})")
            except Exception:
                pass
    try:
        page.evaluate(_PLAY_MAIN)      # ana video'yu programatik baslat
    except Exception:
        pass


def detect_streams(page_url, timeout_s=45, log=None):
    """{'candidates':[...], 'title':..., 'referer':..., 'note':...} dondurur."""
    log = log or (lambda m: None)
    captured = {}
    title_holder = {"t": ""}

    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        return {"candidates": [], "error": f"Playwright yok: {e}"}

    with sync_playwright() as p:
        for channel in ("msedge", "chrome", None):
            try:
                launch_kw = dict(headless=True, args=[
                    "--autoplay-policy=no-user-gesture-required", "--mute-audio",
                    "--disable-blink-features=AutomationControlled"])
                if channel:
                    launch_kw["channel"] = channel
                browser = p.chromium.launch(**launch_kw)
                break
            except Exception as e:
                last = e
        else:
            return {"candidates": [], "error": f"Tarayici acilamadi: {last}"}

        ctx = browser.new_context(viewport={"width": 1280, "height": 720},
                                  ignore_https_errors=True)
        page = ctx.new_page()

        def on_response(resp):
            try:
                url = resp.url
                path = url.split("?")[0].lower()
                if ".m3u8" not in path and not path.endswith(".mpd"):
                    return
                if url in captured:
                    return
                body = ""
                if ".m3u8" in path:
                    try:
                        body = resp.text()
                    except Exception:
                        body = ""
                captured[url] = {
                    "url": url, "body": body, "ad": _is_ad(url),
                    "dash": path.endswith(".mpd"),
                    "is_master": "#EXT-X-STREAM-INF" in body,
                    "is_media": "#EXTINF" in body,
                }
                log(f"m3u8 captured{' (ad)' if _is_ad(url) else ''}: …{url[-60:]}")
            except Exception:
                pass

        page.on("response", on_response)

        try:
            page.goto(page_url, wait_until="domcontentloaded", timeout=timeout_s * 1000)
        except Exception as e:
            log(f"page load warning: {str(e)[:80]}")
        try:
            title_holder["t"] = page.title()
        except Exception:
            pass

        _try_consent(page, log)
        _try_play(page, log)

        deadline = time.time() + timeout_s
        replayed = 0
        while time.time() < deadline:
            content = [c for c in captured.values() if not c["ad"]]
            if any(c["is_master"] for c in content):
                page.wait_for_timeout(1500)   # ek rendition/varyant icin biraz bekle
                break
            if any(c["is_media"] or c["dash"] for c in content):
                page.wait_for_timeout(2500)
                break
            page.wait_for_timeout(700)
            if replayed < 3:                  # reklam bitince tekrar oynat dene (tiklama YOK)
                _try_play(page, log, click=False)
                replayed += 1
        try:
            if not title_holder["t"]:
                title_holder["t"] = page.title()
        except Exception:
            pass
        browser.close()

    return _build(page_url, captured, title_holder["t"], log)


def _build(page_url, captured, title, log):
    origin = "{0}://{1}/".format(*urlparse(page_url)[:2])
    content = [c for c in captured.values() if not c["ad"]]

    # DASH-only durumu
    if content and all(c["dash"] for c in content):
        return {"candidates": [], "title": title, "referer": origin, "note": "dash_only"}

    # master'lari topla (base'e gore tekille)
    masters, seen = [], set()
    for c in content:
        if c["is_master"] and _base(c["url"]) not in seen:
            seen.add(_base(c["url"]))
            try:
                rends = parse_master(c["body"], c["url"])
                heights = sorted({r.height for r in rends if r.height}, reverse=True)
                subs = [{"lang": s["lang"], "name": s["name"]}
                        for s in parse_subtitles(c["body"], c["url"])]
            except Exception:
                heights, subs = [], []
            masters.append({"url": c["url"], "kind": "master", "qualities": heights,
                            "subs": subs, "referer": origin})

    if masters:
        cands = masters
    else:
        # master yok: dogrudan media playlist'leri aday yap
        cands, seen = [], set()
        for c in content:
            if c["is_media"] and _base(c["url"]) not in seen:
                seen.add(_base(c["url"]))
                cands.append({"url": c["url"], "kind": "media",
                              "qualities": [], "subs": [], "referer": origin})

    # sayfayla en alakali akisi one al (or. dogru bolum numarasi)
    cands.sort(key=lambda c: _relevance(c["url"], page_url), reverse=True)

    note = ""
    if not cands:
        note = "none"          # otomatik akis bulunamadi
    elif len(cands) > 1:
        note = "multiple"      # birden fazla akis - kullanici secsin

    return {"candidates": cands, "title": title, "referer": origin, "note": note}


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("kullanim: py detector.py <sayfa-url>")
        sys.exit(1)
    res = detect_streams(sys.argv[1], log=lambda m: print("  [log]", m))
    print("\n=== SONUC ===")
    print("baslik:", res.get("title"))
    print("referer:", res.get("referer"))
    print("not:", res.get("note"))
    for i, c in enumerate(res.get("candidates", [])):
        print(f"  aday#{i}: {c['kind']} kaliteler={c['qualities']}")
        print(f"          {c['url'][:110]}")
    if res.get("error"):
        print("HATA:", res["error"])
