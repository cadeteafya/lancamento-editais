# -*- coding: utf-8 -*-
"""
Scraper de lançamentos de edital (Estratégia MED)

- Captura posts apenas com tabelas-resumo válidas e link oficial externo.
- Extrai TODAS as tabelas 2 colunas do post (cada uma como uma "seção"),
  definindo o título como a última frase em negrito antes da tabela
  (ignora "Aviso" como título).
- Monta 'display_title' a partir de Nome(SIGLA) no primeiro(s) parágrafos.
- Tenta extrair sigla via OCR do banner (Tesseract) só como apoio.
- Mantém histórico em data/editais.json (merge por URL) e ordena por posted_at||captured_at desc.
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
UA = "ResidMedBot/1.9 (+contato: seu-email)"

S = requests.Session()
S.headers.update({"User-Agent": UA, "Accept-Language": "pt-BR,pt;q=0.9"})

ANCHOR_TXT = re.compile(
    r"p[aá]gina oficial da (banca organizadora|institui[cç][aã]o|processo seletivo|sele[cç][aã]o)",
    re.I,
)
SOCIAL = ("facebook.com","twitter.com","t.me","linkedin.com","instagram.com","wa.me","tiktok.com","x.com")

# ---------- helpers ----------
def norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())

def slugify(s: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", norm(s).lower()).strip("-")
    return base[:80] or hashlib.md5(s.encode()).hexdigest()[:10]

def looks_like_sigla(s: str) -> bool:
    return bool(re.fullmatch(r"[A-Z]{2,10}(?:-[A-Z]{2,10})*", (s or "").upper()))

def soup_of(url: str) -> BeautifulSoup:
    r = S.get(url, timeout=30)
    r.raise_for_status()
    return BeautifulSoup(r.text, "lxml")

# ---------- LISTAGEM (FALTAVA) ----------
def list_article_urls(limit: int = 30):
    """Retorna os URLs dos posts da página de notícias (primeira página)."""
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

# ---------- link oficial ----------
def extract_official_link(soup: BeautifulSoup, base_url: str):
    for a in soup.find_all("a", href=True):
        txt = norm(a.get_text(" "))
        if ANCHOR_TXT.search(txt):
            href = urljoin(base_url, a["href"])
            host = (urlparse(href).hostname or "").lower()
            if host and "med.estrategia.com" not in host and not any(s in host for s in SOCIAL):
                return href
    return None

# ---------- OCR ----------
def fetch_image_bytes(url: str):
    try:
        r = S.get(url, timeout=30)
        r.raise_for_status()
        return r.content
    except Exception:
        return None

def ocr_instituicao_from_image(image_url: str):
    """OCR do banner: ignora 'SAIU O EDITAL' e retorna uma linha candidata (sigla/nome curto)."""
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
        best = best.upper()
        best = re.sub(r"[^A-Z\- ]", "", best)            # preserva hífen
        best = max(best.split(), key=len) if best.split() else best
        if 2 <= len(best) <= 80:
            print(f"    (OCR) candidato = {best}")
            return best
    except Exception as e:
        print(f"    (OCR) falhou: {e}")
    return None

# ---------- Nome (SIGLA) nos primeiros parágrafos ----------
NAME_SIGLA_RE = re.compile(
    r"(?P<nome>[A-ZÁÀÂÃÉÊÍÓÔÕÚÇ][^()]{2,120})\s*\(\s*(?P<sigla>[A-Z]{2,10}(?:-[A-Z]{2,10})*)\s*\)"
)

def find_nome_sigla_pairs(soup: BeautifulSoup):
    """Pega 'Nome (SIGLA)' nos 3–4 primeiros parágrafos do artigo (preserva caixa)."""
    pairs = []
    ps = soup.select("article p") or soup.find_all("p")
    for p in ps[:4]:
        txt_strong = " ".join(t.get_text(" ") for t in p.find_all(["strong","b"]))
        txt = norm(txt_strong) if txt_strong else norm(p.get_text(" "))
        for m in NAME_SIGLA_RE.finditer(txt):
            nome = norm(m.group("nome"))
            sigla = norm(m.group("sigla"))
            if 2 <= len(sigla) <= 10 and len(nome) >= 4:
                pairs.append({"nome": nome, "sigla": sigla, "sigla_up": sigla.upper()})
    return pairs

# ---------- Títulos por tabela ----------
AVISO_RE = re.compile(r"^\s*aviso\s*$", re.I)

def last_bold_before(node) -> str | None:
    for prev in node.find_all_previous():
        name = getattr(prev, "name", "")
        if name in ("strong", "b"):
            txt = norm(prev.get_text(" "))
            if txt and not AVISO_RE.match(txt):
                return txt
        if name in ("h2", "h3", "h4"):
            txt = norm(prev.get_text(" "))
            if txt and not AVISO_RE.match(txt):
                return txt
    return None

def extract_table_sections(soup: BeautifulSoup):
    """Todas as tabelas 2 colunas com título (último negrito antes; ignora 'Aviso')."""
    sections = []
    for tb in soup.find_all("table"):
        rows = []
        for tr in tb.find_all("tr"):
            tds = tr.find_all(["td", "th"])
            if len(tds) >= 2:
                etapa = norm(tds[0].get_text(" "))
                data = norm(tds[1].get_text(" "))
                if etapa and data:
                    rows.append({"etapa": etapa, "data": data})

        # remove header óbvio
        if len(rows) >= 3 and re.search(r"\betapa\b", rows[0]["etapa"], re.I) and re.search(r"\bdata\b", rows[0]["data"], re.I):
            rows = rows[1:]
        if len(rows) >= 2:
            titulo = last_bold_before(tb) or "Resumo"
            sections.append({"titulo": titulo, "linhas": rows})
    return sections

# ---------- parse de um post ----------
def parse_post(url: str):
    soup = soup_of(url)

    # título do post (metadado)
    title = soup.find("meta", {"property": "og:title"})
    title = title.get("content", "") if title else ""
    if not title:
        h1 = soup.find(["h1", "h2"])
        title = h1.get_text(" ", strip=True) if h1 else url
    title = norm(title)

    # imagem
    ogimg = soup.find("meta", {"property": "og:image"})
    image = ogimg.get("content", "") if ogimg else ""
    if not image:
        imgel = soup.find("img")
        if imgel and imgel.get("src"):
            image = urljoin(url, imgel["src"])

    # publicado em
    posted_at = None
    meta_pub = soup.find("meta", {"property": "article:published_time"}) \
               or soup.find("meta", {"name": "article:published_time"}) \
               or soup.find("time", {"itemprop": "datePublished"})
    if meta_pub:
        posted_at = (meta_pub.get("content") or meta_pub.get("datetime") or "").strip() or None

    # seções/tabelas
    secoes = extract_table_sections(soup)
    if not secoes:
        print(f"  × DESCARTADO: {title} | sem tabelas no padrão")
        return None

    # link oficial
    link_banca = extract_official_link(soup, url)
    if not link_banca:
        print(f"  × DESCARTADO: {title} | link_banca —")
        return None

    # OCR + Nome(SIGLA)
    instituicao = ocr_instituicao_from_image(image)
    pairs = find_nome_sigla_pairs(soup)
    display_title = None
    if pairs:
        ocr_sigla = instituicao.upper() if looks_like_sigla(instituicao) else None
        picked = None
        if ocr_sigla:
            for pr in pairs:
                if pr["sigla_up"] == ocr_sigla:
                    picked = pr; break
        if not picked:
            picked = pairs[0]
        display_title = f'{picked["nome"]} ({picked["sigla"]})'
        instituicao = picked["sigla"]

    captured_at = datetime.now(timezone.utc).isoformat()
    print(f"  ✓ {title} | tabelas={len(secoes)} | banca=OK")

    # compat: 'dados' = primeira tabela
    dados_first = secoes[0]["linhas"] if secoes else []

    return {
        "slug": slugify(title),
        "nome": title,
        "display_title": display_title,
        "instituicao": instituicao,
        "link": url,
        "imagem": image,
        "dados": dados_first,
        "secoes": secoes,
        "link_banca": link_banca,
        "posted_at": posted_at,
        "captured_at": captured_at,
    }

# ---------- merge/ordenar ----------
def merge(existing: list, new_items: list):
    by = {x.get("link"): x for x in existing if isinstance(x, dict) and x.get("link")}
    for it in new_items:
        prev = by.get(it["link"], {})
        merged = {**prev, **{k: v for k, v in it.items() if v is not None}}
        by[it["link"]] = merged

    def sort_key(x):
        return (x.get("posted_at") or x.get("captured_at") or "")

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

    urls = list_article_urls(limit=30)  # <-- agora existe
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
