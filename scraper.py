import re, json, time, hashlib
from pathlib import Path
from urllib.parse import urljoin, urlparse
import requests
from bs4 import BeautifulSoup
import io, numpy as np
from PIL import Image, ImageOps, ImageFilter
import pytesseract
import cv2

def fetch_image_bytes(url):
    try:
        r = S.get(url, timeout=30)
        r.raise_for_status()
        return r.content
    except Exception:
        return None

def ocr_instituicao_from_image(image_url):
    """
    Baixa a imagem do post (og:image) e extrai o nome logo abaixo do selo 'SAIU O EDITAL!'.
    Estratégia: OCR por linhas; ignora linhas com 'SAIU O EDITAL'; retorna a linha mais 'forte'.
    """
    raw = fetch_image_bytes(image_url)
    if not raw:
        return None

    # abre e pré-processa
    img = Image.open(io.BytesIO(raw)).convert("RGB")
    # aumenta tamanho para melhorar OCR
    scale = 1.6
    img = img.resize((int(img.width*scale), int(img.height*scale)), Image.LANCZOS)

    # realce de contraste + tons de cinza
    gray = ImageOps.grayscale(img)
    gray = ImageOps.autocontrast(gray)

    # leve desfoque para reduzir ruído
    gray = gray.filter(ImageFilter.MedianFilter(size=3))

    # binarização com Otsu (via OpenCV)
    arr = np.array(gray)
    arr = cv2.threshold(arr, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]

    # OCR
    text = pytesseract.image_to_string(arr, lang="por+eng", config="--psm 6")
    lines = [re.sub(r"\s+", " ", l).strip() for l in text.splitlines()]
    lines = [l for l in lines if l]

    # remove linhas do selo
    lines = [l for l in lines if not re.search(r"saiu\s*o\s*edital", l, re.I)]

    if not lines:
        return None

    # heurística: escolher a linha mais "institucional":
    # - prioriza linhas com ≥ 2 caracteres, poucas pontuações, com letras maiúsculas predominantes
    def score(l):
        caps = sum(1 for ch in l if ch.isupper())
        letters = sum(1 for ch in l if ch.isalpha())
        frac_caps = caps / letters if letters else 0
        penal = sum(1 for ch in l if ch in ":|/\\.!?,;")  # menos sinais
        return (len(l) * 1.0) + (frac_caps * 10.0) - (penal * 2.0)

    best = max(lines, key=score)
    # limpar ruído comum tipo artefatos
    best = re.sub(r"^[^A-Za-z0-9]+|[^A-Za-z0-9]+$", "", best)
    return best or None


LIST_URL = "https://med.estrategia.com/portal/noticias/"
OUT_PATH = Path("data/editais.json")
UA = "ResidMedBot/1.2 (+contato: seu-email)"

S = requests.Session()
S.headers.update({"User-Agent": UA, "Accept-Language": "pt-BR,pt;q=0.9"})

PHRASE_RE = re.compile(
    r"p[aá]gina oficial da banca organizadora", re.I
)
ATENCAO_BLOCK_RE = re.compile(
    r"Aten[cç][aã]o!\s*É essencial que o candidato.*p[aá]gina oficial da banca organizadora",
    re.I | re.S
)

def norm(s): return re.sub(r"\s+", " ", (s or "").strip())
def slug(s):
    t = re.sub(r"[^a-z0-9]+", "-", norm(s).lower()).strip("-")
    return t[:80] or hashlib.md5(s.encode()).hexdigest()[:10]

def soup_of(url):
    r = S.get(url, timeout=30)
    r.raise_for_status()
    return BeautifulSoup(r.text, "lxml")

def list_article_urls():
    soup = soup_of(LIST_URL)
    urls, seen = [], set()
    # pega os links dos cards da listagem /portal/noticias/
    for a in soup.select("a[href]"):
        href = a.get("href", "")
        if "/portal/noticias/" in href:
            u = urljoin(LIST_URL, href.split("?")[0].split("#")[0])
            if urlparse(u).scheme in ("http", "https") and u not in seen:
                seen.add(u); urls.append(u)
    return urls[:30]  # checa só os mais recentes

def extract_table(soup):
    """Encontra a tabela 'Resumo edital …' (2 colunas) e retorna linhas [{etapa,data}]."""
    # ajuda: muitos posts têm subtítulo 'Resumo edital ...'
    # mas confiamos na forma (tabela 2 colunas com >=3 linhas)
    for tb in soup.find_all("table"):
        rows = []
        for tr in tb.find_all("tr"):
            tds = tr.find_all(["td","th"])
            if len(tds) >= 2:
                etapa = norm(tds[0].get_text(" "))
                data  = norm(tds[1].get_text(" "))
                if etapa and data:
                    rows.append({"etapa": etapa, "data": data})
        if len(rows) >= 3:
            # remove header "Etapa | Data"
            if re.search(r"\betapa\b", rows[0]["etapa"], re.I) and re.search(r"\bdata\b", rows[0]["data"], re.I):
                rows = rows[1:]
            if len(rows) >= 2:
                return rows
    return []

def extract_official_link(soup, base_url):
    PHRASE = re.compile(r"p[aá]gina oficial da banca organizadora", re.I)
    SOCIAL = ("facebook.com","twitter.com","t.me","linkedin.com","instagram.com","wa.me","tiktok.com","x.com")
    for a in soup.find_all("a", href=True):
        text = re.sub(r"\s+"," ",a.get_text(" ").strip())
        if PHRASE.search(text):
            href = urljoin(base_url, a["href"])
            host = (urlparse(href).hostname or "").lower()
            if host and "med.estrategia.com" not in host and not any(s in host for s in SOCIAL):
                return href
    return None

    def is_valid(href):
        host = (urlparse(href).hostname or "").lower()
        if not host:
            return False
        if "med.estrategia.com" in host:
            return False
        if any(s in host for s in SOCIAL):
            return False
        return True

    # Procura diretamente <a> cujo innerText contenha a frase exata
    for a in soup.find_all("a", href=True):
        text = norm(a.get_text(" "))
        if PHRASE.search(text):
            href = urljoin(base_url, a["href"])
            if is_valid(href):
                return href

    # Se não achar, não retorna nada (melhor vazio do que errado)
    return None

def parse_post(url):
    soup = soup_of(url)

    # título e imagem
    title = soup.find("meta", {"property": "og:title"})
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

    instituicao = ocr_instituicao_from_image(image) if image else None

    return {
        "slug": slug(title),
        "nome": title,
        "instituicao": instituicao,   # <— novo
        "link": url,
        "imagem": image,
        "dados": dados,
        "link_banca": link_banca
    }

    # Critério de "card SAIU O EDITAL": precisa ter TABELA + FRASE EXATA + LINK EXTERNO
    # Só descartamos se NÃO houver tabela de resumo (isso indica que não é lançamento).
    if not dados:
        print(f"  × DESCARTADO (sem tabela): {title}")
        return None
    
    # Mantém o item mesmo sem link_banca; quando existir, incluímos.
    print(f"  ✓ {title} | linhas={len(dados)} | banca={'OK' if link_banca else '—'}")


    print(f"  ✓ {title} | linhas={len(dados)} | banca={link_banca}")
    return {
        "slug": slug(title),
        "nome": title,
        "link": url,
        "imagem": image,
        "dados": dados,
        "link_banca": link_banca
    }

def main():
    try:
        existing = json.loads(OUT_PATH.read_text(encoding="utf-8"))
        if not isinstance(existing, list): existing = []
    except Exception:
        existing = []

    urls = list_article_urls()
    items = []
    for u in urls:
        try:
            it = parse_post(u)
            if it: items.append(it)
            time.sleep(0.5)
        except Exception as e:
            print(f"  ! erro em {u}: {e}")

    if not items:
        print("[!] Nenhum post válido encontrado. Mantendo arquivo atual.")
        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        OUT_PATH.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
        return

    # merge por URL (recente primeiro, limita 30)
    by = {x["link"]: x for x in existing}
    for it in items: by[it["link"]] = it
    merged = list(by.values())[:30]

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[i] Gravado {OUT_PATH} com {len(merged)} registros.")

if __name__ == "__main__":
    main()





