# -*- coding: utf-8 -*-
"""
Scraper de lançamentos de edital (Estratégia MED)

Critérios para entrar no JSON:
  (a) Ter RESUMO do edital:
      - preferencialmente em <table> 2 colunas (>=2 linhas úteis), ou
      - fallback: bloco "Resumo Edital ..." com pares "rótulo : valor" ou <strong>rótulo</strong> valor
  (b) Ter ÂNCORA cujo texto contenha "página oficial da (banca organizadora|instituição|processo seletivo)"
      e cujo href seja EXTERNO (não med.estrategia.com e não rede social)

Campos no JSON:
  - slug, nome (título do post), instituicao (OCR da imagem), link, imagem,
    dados (lista [{etapa, data}]), link_banca (externo), captured_at (ISO-UTC)
Histórico: acumulado (merge por URL) e ordenado por captured_at desc.
"""

import io
import json
import re
import time
import hashlib
from pathlib import Path
from urllib.parse import urljoin, urlparse
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

# ---------- OCR (tolerante) ----------
try:
    import numpy as np
    from PIL import Image, ImageOps, ImageFilter
    import pytesseract
    import cv2
    OCR_AVAILABLE = True
except Exception:
    OCR_AVAILABLE = False

LIST_URL = "https://med.estrategia.com/portal/noticias/"
OUT_PATH = Path("data/editais.json")
UA = "ResidMedBot/1.5 (+contato: seu-email)"

S = requests.Session()
S.headers.update({"User-Agent": UA, "Accept-Language": "pt-BR,pt;q=0.9"})

# aceita banca organizadora | instituição | processo seletivo
ANCHOR_TXT = re.compile(
    r"p[aá]gina oficial da (banca organizadora|institui[cç][aã]o|processo seletivo|sele[cç][aã]o)",
    re.I
)

SOCIAL = (
    "facebook.com",
    "twitter.com",
    "t.me",
    "linkedin.com",
    "instagram.com",
    "wa.me",
    "tiktok.com",
    "x.com",
)

# ---------- helpers ----------
def norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())

def slugify(s: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", norm(s).lower()).strip("-")
    return base[:80] or hashlib.md5(s.encode()).hexdigest()[:10]

def soup_of(url: str) -> BeautifulSoup:
    r = S.get(url, timeout=30)
    r.raise_for_status()
    return BeautifulSoup(r.text, "lxml")

# ---------- listagem ----------
def list_article_urls(limit: int = 30):
    print(f"[i] Lendo listagem: {LIST_URL}")
    soup = soup_of(LIST_URL)
    urls, seen = [], set()
    for a in soup.select("a[href]"):
        href = a.get("href", "")
        if "/portal/noticias/" in href:
            u = urljoin(LIST_URL, href.split("?")[0].split("#")[0])
            if urlparse(u).scheme in ("http", "https") and u not in seen:
                seen.add(u)
                urls.append(u)
    print(f"[i] Encontrados {len(urls)} links; checando {min(limit, len(urls))}.")
    return urls[:limit]

# ---------- extração por post ----------
def extract_summary(soup: BeautifulSoup):
    """
    1) tenta tabela 2 colunas (>=2 linhas úteis)
    2) fallback: bloco 'Resumo Edital ...' sem tabela
    """
    # 1) TABELA
    for tb in soup.find_all("table"):
        rows = []
        for tr in tb.find_all("tr"):
            tds = tr.find_all(["td", "th"])
            if len(tds) >= 2:
                etapa = norm(tds[0].get_text(" "))
                data = norm(tds[1].get_text(" "))
                if etapa and data:
                    rows.append({"etapa": etapa, "data": data})
        if len(rows) >= 3:
            if re.search(r"\betapa\b", rows[0]["etapa"], re.I) and re.search(r"\bdata\b", rows[0]["data"], re.I):
                rows = rows[1:]
            if len(rows) >= 2:
                return rows

    # 2) FALLBACK "Resumo Edital ..."
    # tenta achar heading com "resumo" próximo do conteúdo
    heading = soup.find(lambda t: t.name in ["h2", "h3", "h4"] and "resumo" in t.get_text(" ").lower())
    if not heading:
        return []

    rows = []
    for el in heading.find_all_next():
        # para quando chegar em outro título/seção
        if el.name in ["h2", "h3", "h4"]:
            break
        txt = norm(el.get_text(" "))
        if not txt:
            continue

        # caso <li><strong>Rótulo</strong> Valor</li>
        strong = el.find("strong")
        if strong:
            rot = norm(strong.get_text(" "))
            val = norm(el.get_text(" ").replace(strong.get_text(" "), "", 1))
            if rot and val and len(rot) <= 80:
                rows.append({"etapa": rot, "data": val})
            continue

        # split por ":" OU por espaços largos
        parts = re.split(r"\s{2,}|:", txt, maxsplit=1)
        if len(parts) == 2:
            rot, val = norm(parts[0]), norm(parts[1])
            if rot and val and len(rot) <= 80:
                rows.append({"etapa": rot, "data": val})

    return rows if len(rows) >= 2 else []

def extract_official_link(soup: BeautifulSoup, base_url: str):
    """Pega SOMENTE <a> cujo texto contenha ANCHOR_TXT e que seja externo (não social / não Estratégia)."""
    for a in soup.find_all("a", href=True):
        txt = norm(a.get_text(" "))
        if ANCHOR_TXT.search(txt):
            href = urljoin(base_url, a["href"])
            host = (urlparse(href).hostname or "").lower()
            if host and "med.estrategia.com" not in host and not any(s in host for s in SOCIAL):
                return href
    return None

def fetch_image_bytes(url: str):
    try:
        r = S.get(url, timeout=30)
        r.raise_for_status()
        return r.content
    except Exception:
        return None

def ocr_instituicao_from_image(image_url: str):
    """OCR: ignora linha 'SAIU O EDITAL' e devolve a melhor linha como nome da instituição."""
    if not (OCR_AVAILABLE and image_url):
        return None
    raw = fetch_image_bytes(image_url)
    if not raw:
        return None
    try:
        img = Image.open(io.BytesIO(raw)).convert("RGB")
        img = img.resize((int(img.width * 1.6), int(img.height * 1.6)), Image.LANCZOS)
        gray = ImageOps.grayscale(img)
        gray = ImageOps.autocontrast(gray)
        gray = gray.filter(ImageFilter.MedianFilter(size=3))
        arr = np.array(gray)
        arr = cv2.threshold(arr, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]
        text = pytesseract.image_to_string(arr, lang="por+eng", config="--psm 6")
        lines = [re.sub(r"\s+", " ", l).strip() for l in text.splitlines() if l.strip()]
        lines = [l for l in lines if not re.search(r"saiu\s*o\s*edital", l, re.I)]
        if not lines:
            return None
        def score(l):
            letters = sum(1 for ch in l if ch.isalpha())
            caps = sum(1 for ch in l if ch.isupper())
            frac_caps = (caps / letters) if letters else 0
            penal = sum(1 for ch in l if ch in ":|/\\.!?,;")
            return len(l) + 10 * frac_caps - 2 * penal
        best = max(lines, key=score)
        best = re.sub(r"^[^A-Za-z0-9]+|[^A-Za-z0-9]+$", "", best)
        if 2 <= len(best) <= 80:
            print(f"    (OCR) instituição = {best}")
            return best
    except Exception as e:
        print(f"    (OCR) falhou: {e}")
    return None

def parse_post(url: str):
    soup = soup_of(url)

    title = soup.find("meta", {"property": "og:title"})
    title = title.get("content", "") if title else ""
    if not title:
        h1 = soup.find(["h1", "h2"])
        title = h1.get_text(" ", strip=True) if h1 else url
    title = norm(title)

    ogimg = soup.find("meta", {"property": "og:image"})
    image = ogimg.get("content", "") if ogimg else ""
    if not image:
        imgel = soup.find("img")
        if imgel and imgel.get("src"):
            image = urljoin(url, imgel["src"])

    dados = extract_summary(soup)
    link_banca = extract_official_link(soup, url)

    # Filtro: precisa ter resumo + link oficial
    if not dados or not link_banca:
        print(f"  × DESCARTADO: {title} | resumo={len(dados)} | link_banca={'OK' if link_banca else '—'}")
        return None

    instituicao = None
    try:
        if image:
            instituicao = ocr_instituicao_from_image(image)
    except Exception as e:
        print(f"    (OCR) erro inesperado: {e}")

    captured_at = datetime.now(timezone.utc).isoformat()
    print(f"  ✓ {title} | linhas={len(dados)} | banca=OK")

    return {
        "slug": slugify(title),
        "nome": title,
        "instituicao": instituicao,   # pode ser None
        "link": url,
        "imagem": image,
        "dados": dados,
        "link_banca": link_banca,
        "captured_at": captured_at,
    }

# ---------- merge (histórico acumulado) ----------
def merge(existing: list, new_items: list):
    by = {x.get("link"): x for x in existing if isinstance(x, dict) and x.get("link")}
    for it in new_items:
        prev = by.get(it["link"], {})
        # não sobrescreve campos com None
        merged = {**prev, **{k: v for k, v in it.items() if v is not None}}
        by[it["link"]] = merged
    def sort_key(x):
        return x.get("captured_at", "")
    return sorted(by.values(), key=sort_key, reverse=True)

# ---------- main ----------
def main():
    # carrega existente
    try:
        existing = json.loads(OUT_PATH.read_text(encoding="utf-8"))
        if not isinstance(existing, list):
            existing = []
    except Exception:
        existing = []

    urls = list_article_urls(limit=30)
    items = []
    for u in urls:
        try:
            it = parse_post(u)
            if it:
                items.append(it)
            time.sleep(0.5)
        except Exception as e:
            print(f"  ! erro em {u}: {e}")

    if not items and not existing:
        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        OUT_PATH.write_text("[]", encoding="utf-8")
        print("[!] Nenhum item válido; gravado JSON vazio.")
        return

    final = merge(existing, items)
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(final, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[i] Gravado {OUT_PATH} com {len(final)} registros.")

if __name__ == "__main__":
    main()

