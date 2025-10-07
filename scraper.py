import re, json, time, hashlib
from pathlib import Path
from urllib.parse import urljoin, urlparse
import requests
from bs4 import BeautifulSoup

LIST_URL = "https://med.estrategia.com/portal/noticias/"
OUT_PATH = Path("data/editais.json")
UA = "ResidMedBot/1.1 (+contato: cadete.afya@gmail.com)"

S = requests.Session()
S.headers.update({"User-Agent": UA, "Accept-Language": "pt-BR,pt;q=0.9"})

def norm(s): return re.sub(r"\s+", " ", (s or "").strip())
def slug(s):
    t = re.sub(r"[^a-z0-9]+", "-", norm(s).lower()).strip("-")
    return t[:80] or hashlib.md5(s.encode()).hexdigest()[:10]

def soup_of(url):
    r = S.get(url, timeout=30)
    r.raise_for_status()
    return BeautifulSoup(r.text, "lxml")

def list_article_urls():
    print(f"[i] Lendo lista: {LIST_URL}")
    soup = soup_of(LIST_URL)
    urls = []
    # pega todos links de posts dentro da listagem
    for a in soup.select("a[href]"):
        href = a.get("href", "")
        if "/portal/noticias/" in href:
            u = urljoin(LIST_URL, href.split("?")[0].split("#")[0])
            if urlparse(u).scheme in ("http", "https"):
                urls.append(u)
    # manter somente os 20 mais recentes e sem duplicatas
    seen, out = set(), []
    for u in urls:
        if u not in seen:
            seen.add(u); out.append(u)
    print(f"[i] Encontrados {len(out)} links na listagem (vou checar os 20 primeiros).")
    return out[:20]

def parse_post(url):
    soup = soup_of(url)

    # título
    title = soup.find("meta", {"property": "og:title"})
    title = title.get("content","") if title else ""
    if not title:
        h1 = soup.find(["h1","h2"])
        title = h1.get_text(" ", strip=True) if h1 else url
    title = norm(title)

    # imagem principal
    ogimg = soup.find("meta", {"property":"og:image"})
    image = ogimg.get("content","") if ogimg else ""
    if not image:
        imgel = soup.find("img")
        if imgel and imgel.get("src"): image = urljoin(url, imgel["src"])

    # TABELA DE RESUMO (duas colunas)
    dados = []
    for tb in soup.find_all("table"):
        rows = []
        for tr in tb.find_all("tr"):
            tds = tr.find_all(["td","th"])
            if len(tds) >= 2:
                etapa = norm(tds[0].get_text(" "))
                data  = norm(tds[1].get_text(" "))
                if etapa and data: rows.append({"etapa":etapa,"data":data})
        if len(rows) >= 3:
            # remove cabeçalho "Etapa | Data" se existir
            if re.search(r"\betapa\b", rows[0]["etapa"], re.I) and re.search(r"\bdata\b", rows[0]["data"], re.I):
                rows = rows[1:]
            if len(rows) >= 2:
                dados = rows
                break

    # LINK DA BANCA (texto "página oficial da banca organizadora")
    link_banca = ""
    for t in soup.find_all(string=re.compile(r"página oficial da banca organizadora", re.I)):
        a = (t.parent.find("a", href=True) or t.find_next("a", href=True))
        if a:
            link_banca = urljoin(url, a["href"]); break

    # Só considera post válido se achou a tabela de resumo
    valido = len(dados) > 0
    print(f"  {'✓' if valido else '×'} {title} | tabela={len(dados)} linhas | banca={'sim' if link_banca else 'não'}")
    if not valido: return None

    return {
        "slug": slug(title),
        "nome": title,
        "link": url,
        "imagem": image,
        "dados": dados,
        "link_banca": link_banca or None
    }

def main():
    # carrega existente
    try:
        existing = json.loads(OUT_PATH.read_text(encoding="utf-8"))
        if not isinstance(existing, list): existing = []
    except Exception:
        existing = []

    urls = list_article_urls()
    items = []
    for u in urls:
        try:
            item = parse_post(u)
            if item: items.append(item)
            time.sleep(0.6)
        except Exception as e:
            print(f"  ! erro em {u}: {e}")

    if not items:
        print("[!] Nenhum post válido encontrado (sem tabela). Vou manter o arquivo atual.")
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
