# scraper.py
# Atualiza data/editais.json com os últimos "SAIU O EDITAL!" da 1ª página do Estratégia MED
# Requisitos: requests, beautifulsoup4, lxml

import re, json, time, hashlib
from pathlib import Path
from urllib.parse import urljoin, urlparse
import requests
from bs4 import BeautifulSoup

HOME = "https://med.estrategia.com/portal/"
OUT_PATH = Path("data/editais.json")
USER_AGENT = "ResidMedBot/1.0 (+contato: cadete.afya@gmail.com)"

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "pt-BR,pt;q=0.9"})

def norm_space(s):
    return re.sub(r"\s+", " ", (s or "").strip())

def make_slug(name: str) -> str:
    s = norm_space(name).lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return s[:80] or hashlib.md5(name.encode()).hexdigest()[:10]

def get_soup(url: str) -> BeautifulSoup:
    r = SESSION.get(url, timeout=30)
    r.raise_for_status()
    return BeautifulSoup(r.text, "lxml")

def find_cards_with_saiu_o_edital(soup: BeautifulSoup):
    """Heurística ampla: procura cards/links/imagens que contenham 'SAIU O EDITAL'."""
    cards = []
    # 1) Qualquer nó com texto "SAIU O EDITAL"
    for el in soup.find_all(string=re.compile(r"saiu\s*o\s*edital", re.I)):
        # tentar subir até um <a> que leve ao post
        a = el.find_parent("a")
        if not a:
            # às vezes está dentro de div; procurar <a> próximo
            a = el.find_parent().find("a", href=True) if el.find_parent() else None
        if a and a.get("href"):
            href = urljoin(HOME, a["href"])
            cards.append((a, href))
    # 2) Imagens com alt/src com "saiu-o-edital"
    for img in soup.find_all("img"):
        alt = " ".join([img.get("alt",""), img.get("aria-label","")])
        src = img.get("src","")
        if re.search(r"saiu[\s\-]?o[\s\-]?edital", f"{alt} {src}", re.I):
            a = img.find_parent("a")
            if a and a.get("href"):
                href = urljoin(HOME, a["href"])
                cards.append((a, href))
    # normalizar e deduplicar pela URL
    seen = set()
    urls = []
    for _, href in cards:
        u = href.split("?")[0].split("#")[0]
        if u not in seen and urlparse(u).scheme in ("http","https"):
            seen.add(u)
            urls.append(u)
    return urls

def extract_post(url: str):
    soup = get_soup(url)

    # título: preferir <meta property="og:title">, senão <h1>
    title = soup.find("meta", attrs={"property":"og:title"})
    if title: title = title.get("content","")
    if not title:
        h1 = soup.find(["h1","h2"])
        title = h1.get_text(" ", strip=True) if h1 else url

    # imagem principal: og:image ou primeira <img>
    img = soup.find("meta", attrs={"property":"og:image"})
    image = img.get("content","") if img else ""
    if not image:
        imgel = soup.find("img")
        if imgel and imgel.get("src"): image = urljoin(url, imgel["src"])

    # tabela de resumo: buscar por tabela com 2 colunas e cabeçalhos tipo "Etapa" / "Data"
    dados = []
    tables = soup.find_all("table")
    for tb in tables:
        # conferir se parece 2 colunas
        rows = tb.find_all("tr")
        cands = []
        for tr in rows:
            tds = tr.find_all(["td","th"])
            if len(tds) >= 2:
                etapa = norm_space(tds[0].get_text(" "))
                data = norm_space(tds[1].get_text(" "))
                if etapa and data:
                    cands.append({"etapa": etapa, "data": data})
        # heurística: boa se pelo menos 3 linhas
        if len(cands) >= 3:
            dados = cands
            # se a primeira linha contém 'Etapa' e 'Data', remova header da lista
            if re.search(r"\betapa\b", cands[0]["etapa"], re.I) and re.search(r"\bdata\b", cands[0]["data"], re.I):
                dados = cands[1:]
            break

    # link da banca: procurar texto "Página oficial da banca organizadora"
    link_banca = ""
    # buscar o texto e capturar o <a> mais próximo
    txt_nodes = soup.find_all(string=re.compile(r"página oficial da banca organizadora", re.I))
    for t in txt_nodes:
        # 1) link irmão
        sib_a = t.parent.find("a", href=True)
        if sib_a:
            link_banca = urljoin(url, sib_a["href"])
            break
        # 2) link seguinte
        next_a = t.find_next("a", href=True)
        if next_a:
            link_banca = urljoin(url, next_a["href"])
            break

    nome = norm_space(title)
    item = {
        "slug": make_slug(nome),
        "nome": nome,
        "link": url,
        "imagem": image,
        "dados": dados,
        "link_banca": link_banca or None
    }
    return item

def merge_into(existing: list, new_items: list):
    by_url = {x.get("link"): x for x in existing}
    for it in new_items:
        by_url[it["link"]] = it  # atualiza/insere
    # ordena por inserção recente (simples: tempo agora primeiro)
    merged = list(by_url.values())
    # limitar para não crescer demais (mantém 30)
    return merged[:30]

def main():
    # carregar existente
    if OUT_PATH.exists():
        try:
            existing = json.loads(OUT_PATH.read_text(encoding="utf-8"))
            if not isinstance(existing, list): existing = []
        except Exception:
            existing = []
    else:
        existing = []

    print("[i] Buscando homepage…")
    soup = get_soup(HOME)

    urls = find_cards_with_saiu_o_edital(soup)
    if not urls:
        print("[!] Nenhum card 'SAIU O EDITAL!' encontrado na homepage.")
        # ainda assim gravar existing para garantir arquivo
        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        OUT_PATH.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
        return

    print(f"[i] Encontrados {len(urls)} posts. Extraindo páginas individuais…")
    new_items = []
    for u in urls:
        try:
            item = extract_post(u)
            # se não achar tabela, ainda mantém com dados=[]
            new_items.append(item)
            time.sleep(0.8)  # gentileza
            print(f"  ✓ {item['nome']}")
        except Exception as e:
            print(f"  x Erro em {u}: {e}")

    merged = merge_into(existing, new_items)

    # gravar
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[i] Atualizado {OUT_PATH} com {len(merged)} registros.")

if __name__ == "__main__":
    main()
