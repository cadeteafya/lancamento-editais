# -*- coding: utf-8 -*-
"""
Scraper de lançamentos de edital (Estratégia MED)
- Lê /portal/noticias/ (primeira página)
- Mantém SOMENTE posts que tenham:
    (a) TABELA de resumo (2 colunas, >=2 linhas úteis)  e
    (b) Âncora com texto "página oficial da banca organizadora" apontando para domínio EXTERNO
- Extrai:
    - titulo do post
    - imagem (og:image)
    - tabela [{etapa, data}]
    - link_banca (externo)
    - instituicao (OCR na imagem abaixo do selo "SAIU O EDITAL!")
- Atualiza data/editais.json (máx. 30), deduplicando por URL
"""

import io, json, re, time, hashlib
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

# OCR stack (tolerante: se não estiver instalado, segue sem OCR)
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
UA = "ResidMedBot/1.4 (+contato: seu-email)"

S = requests.Session()
S.headers.update({"User-Agent": UA, "Accept-Language": "pt-BR,pt;q=0.9"})

ANCHOR_TXT = re.compile(r"p[aá]gina oficial da banca organizadora", re.I)
SOCIAL = ("facebook.com","twitter.com","t.me","linkedin.com","instagram.com","wa.me","tiktok.com","x.com")

def norm(s): return re.sub(r"\s+"," ",(s or "").strip())
def slugify(s):
    base = re.sub(r"[^a-z0-9]+","-",norm(s).lower()).strip("-")
    return base[:80] or hashlib.md5(s.encode()).hexdigest()[:10]

def soup_of(url:str)->BeautifulSoup:
    r = S.get(url, timeout=30); r.raise_for_status()
    return BeautifulSoup(r.text, "lxml")

def list_article_urls(limit=30):
    soup = soup_of(LIST_URL)
    urls, seen = [], set()
    for a in soup.select("a[href]"):
        href = a.get("href","")
        if "/portal/noticias/" in href:
            u = urljoin(LIST_URL, href.split("?")[0].split("#")[0])
            if urlparse(u).scheme in ("http","https") and u not in seen:
                seen.add(u); urls.append(u)
    return urls[:limit]

def extract_table(soup:BeautifulSoup):
    """retorna [{etapa,data}] se achar tabela 2 colunas com >=2 linhas úteis"""
    for tb in soup.find_all("table"):
        rows = []
        for tr in tb.find_all("tr"):
            tds = tr.find_all(["td","th"])
            if len(tds) >= 2:
                etapa = norm(tds[0].get_text(" "))
                data  = norm(tds[1].get_text(" "))
                if etapa and data:
                    rows.append({"etapa":etapa,"data":data})
        if len(rows) >= 3:
            # remove header "Etapa | Data"
            if re.search(r"\betapa\b", rows[0]["etapa"], re.I) and re.search(r"\bdata\b", rows[0]["data"], re.I):
                rows = rows[1:]
            if len(rows) >= 2:
                return rows
    return []

def extract_official_link(soup:BeautifulSoup, base_url:str):
    """pega SOMENTE <a> cujo texto contenha 'página oficial da banca organizadora' e não seja social/mesmo domínio"""
    for a in soup.find_all("a", href=True):
        txt = norm(a.get_text(" "))
        if ANCHOR_TXT.search(txt):
            href = urljoin(base_url, a["href"])
            host = (urlparse(href).hostname or "").lower()
            if host and "med.estrategia.com" not in host and not any(s in host for s in SOCIAL):
                return href
    return None

def fetch_image_bytes(url):
    try:
        r = S.get(url, timeout=30); r.raise_for_status()
        return r.content
    except Exception:
        return None

def ocr_instituicao_from_image(image_url:str):
    """OCR: ignora linha com 'SAIU O EDITAL' e devolve a melhor linha como nome da instituição"""
    if not (OCR_AVAILABLE and image_url): return None
    raw = fetch_image_bytes(image_url)
    if not raw: return None
    try:
        img = Image.open(io.BytesIO(raw)).convert("RGB")
        # upscale
        img = img.resize((int(img.width*1.6), int(img.height*1.6)), Image.LANCZOS)
        # gray + autocontraste + mediana
        gray = ImageOps.grayscale(img)
        gray = ImageOps.autocontrast(gray)
        gray = gray.filter(ImageFilter.MedianFilter(size=3))
        # binarização Otsu
        arr = np.array(gray)
        arr = cv2.threshold(arr, 0, 255, cv2.THRESH_BINARY+cv2.THRESH_OTSU)[1]
        # OCR
        text = pytesseract.image_to_string(arr, lang="por+eng", config="--psm 6")
        lines = [re.sub(r"\s+"," ",l).strip() for l in text.splitlines() if l.strip()]
        lines = [l for l in lines if not re.search(r"saiu\s*o\s*edital", l, re.I)]
        if not lines: return None

        def score(l):
            letters = sum(1 for ch in l if ch.isalpha())
            caps = sum(1 for ch in l if ch.isupper())
            frac_caps = (caps/letters) if letters else 0
            penal = sum(1 for ch in l if ch in ":|/\\.!?,;")
            return len(l) + 10*frac_caps - 2*penal

        best = re.sub(r"^[^A-Za-z0-9]+|[^A-Za-z0-9]+$","", max(lines, key=score))
        if 2 <= len(best) <= 80:
            print(f"    (OCR) instituição = {best}")
            return best
    except Exception as e:
        print(f"    (OCR) falhou: {e}")
    return None

def parse_post(url:str):
    soup = soup_of(url)

    # título e imagem
    title = soup.find("meta", {"property":"og:title"})
    title = title.get("content","") if title else ""
    if not title:
        h1 = soup.find(["h1","h2"])
        title = h1.get_text(" ", strip=True) if h1 else url
    title = norm(title)

    ogimg = soup.find("meta", {"property":"og:image"})
    image = ogimg.get("content","") if ogimg else ""
    if not image:
        imgel = soup.find("img")
        if imgel and imgel.get("src"):
            image = urljoin(url, imgel["src"])

    dados = extract_table(soup)
    link_banca = extract_official_link(soup, url)

    # >>> filtro estrito: precisa ter tabela + link oficial no texto correto
    if not dados or not link_banca:
        print(f"  × DESCARTADO: {title} | tabela={len(dados)} | link_banca={'OK' if link_banca else '—'}")
        return None

    instituicao = ocr_instituicao_from_image(image) if image else None
    print(f"  ✓ {title} | linhas={len(dados)} | banca=OK")

    return {
        "slug": slugify(title),
        "nome": title,
        "instituicao": instituicao,   # pode ser None se OCR não pegar
        "link": url,
        "imagem": image,              # guardo para debug; não usamos no card
        "dados": dados,
        "link_banca": link_banca
    }

def merge(existing:list, new_items:list, limit:int=30):
    by = {x.get("link"): x for x in existing if isinstance(x, dict) and x.get("link")}
    for it in new_items:
        by[it["link"]] = it
    return list(by.values())[:limit]

def main():
    try:
        existing = json.loads(OUT_PATH.read_text(encoding="utf-8"))
        if not isinstance(existing, list): existing = []
    except Exception:
        existing = []

    urls = list_article_urls(30)
    items = []
    for u in urls:
        try:
            it = parse_post(u)
            if it: items.append(it)
            time.sleep(0.5)
        except Exception as e:
            print(f"  ! erro em {u}: {e}")

    if not items and not existing:
        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        OUT_PATH.write_text("[]", encoding="utf-8")
        print("[!] Nenhum item válido; gravado JSON vazio.")
        return

    final = merge(existing, items, 30)
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(final, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[i] Gravado {OUT_PATH} com {len(final)} registros.")

if __name__ == "__main__":
    main()
