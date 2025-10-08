import re, json, time, hashlib
from pathlib import Path
from urllib.parse import urljoin, urlparse
import requests
from bs4 import BeautifulSoup

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
    """
    Retorna o href da âncora cujo TEXTO contém exatamente
    'página oficial da banca organizadora'. Ignora links do próprio
    med.estrategia.com e redes sociais. Não usa find_next fora da âncora.
    """
    PHRASE = re.compile(r"p[aá]gina oficial da banca organizadora", re.I)
    SOCIAL = ("facebook.com", "twitter.com", "t.me", "linkedin.com",
              "instagram.com", "wa.me", "tiktok.com", "x.com")

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

    # Critério de "card SAIU O EDITAL": precisa ter TABELA + FRASE EXATA + LINK EXTERNO
    if not dados or not link_banca:
        print(f"  × DESCARTADO: {title} | tabela={len(dados)} | link_banca={'ok' if link_banca else 'none'}")
        return None

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


