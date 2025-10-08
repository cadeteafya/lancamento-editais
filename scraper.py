# -*- coding: utf-8 -*-
"""
Scraper de lançamentos de edital (Estratégia MED)
- Lê /portal/noticias/ (primeira página)
- Mantém apenas posts que têm TABELA de resumo (2 colunas, >=2 linhas úteis)
- Extrai link oficial apenas quando a âncora contém exatamente
  "página oficial da banca organizadora" (e é domínio externo)
- Faz OCR na imagem (og:image) para tentar obter o nome da instituição
- Atualiza data/editais.json (máx. 30 registros), deduplicando por URL
"""

import io
import json
import re
import time
import hashlib
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

# OCR stack (tolerante: se não existir, continua sem OCR)
try:
    import numpy as np
    from PIL import Image, ImageOps, ImageFilter
    import pytesseract
    import cv2
    OCR_AVAILABLE = True
except Exception as _:
    OCR_AVAILABLE = False

# ---------------------- Config ----------------------

LIST_URL = "https://med.estrategia.com/portal/noticias/"
OUT_PATH = Path("data/editais.json")
UA = "ResidMedBot/1.3 (+contato: seu-email)"

# ---------------------- Helpers ---------------------

S = requests.Session()
S.headers.update({
    "User-Agent": UA,
    "Accept-Language": "pt-BR,pt;q=0.9"
})

PHRASE_ANCHOR = re.compile(r"p[aá]gina oficial da banca organizadora", re.I)

def norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())

def slugify(s: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", norm(s).lower()).strip("-")
    return base[:80] or hashlib.md5(s.encode()).hexdigest()[:10]

def soup_of(url: str) -> BeautifulSoup:
    r = S.get(url, timeout=30)
    r.raise_for_status()
    return BeautifulSoup(r.text, "lxml")

# ---------------------- Listagem --------------------

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
    print(f"[i] Encontrados {len(urls)} links; vou checar os {min(limit, len(urls))} mais recentes.")
    return urls[:limit]

# ---------------------- Extração por post -----------

def extract_table(soup: BeautifulSoup):
    """Retorna linhas [{etapa,data}] de uma tabela 2 colunas com >=3 linhas (tirando cabeçalho)."""
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
            # remove header "Etapa | Data" se existir
            if re.search(r"\betapa\b", rows[0]["etapa"], re.I) and re.search(r"\bdata\b", rows[0]["data"], re.I):
                rows = rows[1:]
            if len(rows) >= 2:
                return rows
    return []

def extract_official_link(soup: BeautifulSoup, base_url: str):
    """Pega SOMENTE <a> cujo texto contenha 'página oficial da banca organizadora', externo e não-social."""
    SOCIAL = ("facebook.com", "twitter.com", "t.me", "linkedin.com", "instagram.com", "wa.me", "tiktok.com", "x.com")
    for a in soup.find_all("a", href=True):
        txt = norm(a.get_text(" "))
        if PHRASE_ANCHOR.search(txt):
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
    """OCR heurístico: remove linha 'SAIU O EDITAL' e pega a melhor linha restante como nome da instituição."""
    if not OCR_AVAILABLE or not image_url:
        return None
    raw = fetch_image_bytes(image_url)
    if not raw:
        return None
    try:
        img = Image.open(io.BytesIO(raw)).convert("RGB")
        # upscale para ajudar OCR
        scale = 1.6
        img = img.resize((int(img.width * scale), int(img.height * scale)), Image.LANCZOS)

        # tons de cinza + autocontraste
        gray = ImageOps.grayscale(img)
        gray = ImageOps.autocontrast(gray)
        # reduzir ruído
        gray = gray.filter(ImageFilter.MedianFilter(size=3))

        # binarização Otsu
        arr = np.array(gray)
        arr = cv2.threshold(arr, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]

        # OCR (PT + EN; se 'por' não existir no runner, pytesseract cai no eng)
        text = pytesseract.image_to_string(arr, lang="por+eng", config="--psm 6")
        lines = [re.sub(r"\s+", " ", l).strip() for l in text.splitlines()]
        lines = [l for l in lines if l]

        # descarta o selo
        lines = [l for l in lines if not re.search(r"saiu\s*o\s*edital", l, re.I)]
        if not lines:
            return None

        # escolhe linha mais "institucional"
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

    # título e imagem
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

    # tabela de resumo (critério principal)
    dados = extract_table(soup)
    # link oficial (opcional, quando existir a âncora exata)
    link_banca = extract_official_link(soup, url)

    if not dados:
        print(f"  × DESCARTADO (sem tabela): {title}")
        return None

    instituicao = ocr_instituicao_from_image(image) if image else None
    print(f"  ✓ {title} | linhas={len(dados)} | banca={'OK' if link_banca else '—'}")

    return {
        "slug": slugify(title),
        "nome": title,
        "instituicao": instituicao,   # pode ser None se OCR não conseguir
        "link": url,
        "imagem": image,              # mantemos no JSON (mesmo se não exibir no front)
        "dados": dados,
        "link_banca": link_banca or None
    }

# ---------------------- Orquestração ----------------

def merge(existing: list, new_items: list, limit: int = 30):
    by_url = {x.get("link"): x for x in existing if isinstance(x, dict) and x.get("link")}
    for it in new_items:
        by_url[it["link"]] = it
    merged = list(by_url.values())
    # simples: mantém na mesma ordem de inserção (recentes por cima no run atual)
    return merged[:limit]

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
            time.sleep(0.5)  # gentileza
        except Exception as e:
            print(f"  ! erro em {u}: {e}")

    if not items and not existing:
        # garante arquivo válido (lista vazia)
        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        OUT_PATH.write_text("[]", encoding="utf-8")
        print("[!] Nenhum item novo e sem histórico. Gravado JSON vazio.")
        return

    final = merge(existing, items, limit=30)
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(final, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[i] Gravado {OUT_PATH} com {len(final)} registros.")

if __name__ == "__main__":
    main()
