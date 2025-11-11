# -*- coding: utf-8 -*-
import io, json, re, time, hashlib
from pathlib import Path
from urllib.parse import urljoin, urlparse
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

# ---- OCR (opcional) ----
try:
    import numpy as np
    from PIL import Image, ImageOps, ImageFilter
    import pytesseract, cv2
    OCR_AVAILABLE = True
except Exception:
    OCR_AVAILABLE = False

# --- util: gera um display_title a partir de "Resumo Edital <NOME> 20xx" ---
import re

def build_display_title(first_section_title: str, instituicao_full: str = "", nome_fallback: str = "") -> str:
    """
    Extrai o nome entre 'Resumo Edital' e o ano final, ex.:
      'Resumo Edital Hospital Edmundo Vasconcelos 2026' -> 'Hospital Edmundo Vasconcelos'
    Se não casar, volta para instituicao_full ou nome_fallback.
    """
    t = (first_section_title or "").strip()
    if t:
        m = re.search(r"(?i)^\s*Resumo\s+Edital\s+(.+?)(?:\s+(?:19|20)\d{2})?\s*$", t, flags=re.I)
        if m:
            return " ".join(m.group(1).split())
    # fallbacks
    if instituicao_full:
        return " ".join(instituicao_full.split())
    return " ".join((nome_fallback or "").split())


LIST_URL = "https://med.estrategia.com/portal/noticias/"
OUT_PATH = Path("data/editais.json")
UA = "ResidMedBot/2.0 (+contato: seu-email)"

S = requests.Session()
S.headers.update({"User-Agent": UA, "Accept-Language": "pt-BR,pt;q=0.9"})

ANCHOR_TXT = re.compile(r"p[aá]gina oficial da (banca organizadora|institui[cç][aã]o|processo seletivo|sele[cç][aã]o)", re.I)
SOCIAL = ("facebook.com","twitter.com","t.me","linkedin.com","instagram.com","wa.me","tiktok.com","x.com")

def norm(s:str)->str:
    return re.sub(r"\s+"," ",(s or "").strip())

def slugify(s:str)->str:
    base = re.sub(r"[^a-z0-9]+","-",norm(s).lower()).strip("-")
    return base[:80] or hashlib.md5(s.encode()).hexdigest()[:10]

def looks_like_sigla(s:str)->bool:
    return bool(re.fullmatch(r"[A-Z]{2,10}(?:-[A-Z]{2,10})*", (s or "").upper()))

def soup_of(url:str)->BeautifulSoup:
    r=S.get(url,timeout=30); r.raise_for_status()
    return BeautifulSoup(r.text,"lxml")

# ---------- listagem ----------
def list_article_urls(limit:int=30):
    print(f"[i] Lendo listagem: {LIST_URL}")
    soup=soup_of(LIST_URL)
    urls,seen=[],set()
    for a in soup.select("a[href]"):
        href=a.get("href","")
        if "/portal/noticias/" in href:
            u=urljoin(LIST_URL, href.split("?")[0].split("#")[0])
            if urlparse(u).scheme in ("http","https") and u not in seen:
                seen.add(u); urls.append(u)
    print(f"[i] Encontrados {len(urls)} links; checando {min(limit,len(urls))}.")
    return urls[:limit]

# ---------- link oficial ----------
def extract_official_link(soup:BeautifulSoup, base_url:str):
    for a in soup.find_all("a",href=True):
        txt=norm(a.get_text(" "))
        if ANCHOR_TXT.search(txt):
            href=urljoin(base_url,a["href"])
            host=(urlparse(href).hostname or "").lower()
            if host and "med.estrategia.com" not in host and not any(s in host for s in SOCIAL):
                return href
    return None

# ---------- OCR (banner) ----------
def fetch_image_bytes(url:str):
    try:
        r=S.get(url,timeout=30); r.raise_for_status(); return r.content
    except Exception: return None

def ocr_instituicao_from_image(image_url:str):
    if not (OCR_AVAILABLE and image_url): return None
    raw=fetch_image_bytes(image_url)
    if not raw: return None
    try:
        img=Image.open(io.BytesIO(raw)).convert("RGB")
        img=img.resize((int(img.width*1.6), int(img.height*1.6)), Image.LANCZOS)
        gray=ImageOps.grayscale(img); gray=ImageOps.autocontrast(gray); gray=gray.filter(ImageFilter.MedianFilter(3))
        arr=np.array(gray); arr=cv2.threshold(arr,0,255,cv2.THRESH_BINARY+cv2.THRESH_OTSU)[1]
        text=pytesseract.image_to_string(arr, lang="por+eng", config="--psm 6")
        lines=[re.sub(r"\s+"," ",l).strip() for l in text.splitlines() if l.strip()]
        lines=[l for l in lines if not re.search(r"saiu\s*o\s*edital",l,re.I)]
        if not lines: return None
        def score(l):
            letters=sum(1 for ch in l if ch.isalpha())
            caps=sum(1 for ch in l if ch.isupper())
            frac=(caps/letters) if letters else 0
            penal=sum(1 for ch in l if ch in ":|/\\.!?,;")
            return len(l)+10*frac-2*penal
        best=max(lines,key=score).upper()
        best=re.sub(r"[^A-Z\- ]","",best)
        best=max(best.split(), key=len) if best.split() else best
        if 2<=len(best)<=80: return best
    except Exception as e:
        print(f"    (OCR) falhou: {e}")
    return None

# ---------- Nome(SIGLA) nos primeiros parágrafos ----------
NAME_SIGLA_RE = re.compile(r"(?P<nome>[A-ZÁÀÂÃÉÊÍÓÔÕÚÇ][^()]{2,120})\s*\(\s*(?P<sigla>[A-Z]{2,10}(?:-[A-Z]{2,10})*)\s*\)")

def find_nome_sigla_pairs(soup:BeautifulSoup):
    pairs=[]
    ps=soup.select("article p") or soup.find_all("p")
    for p in ps[:4]:
        bold=" ".join(t.get_text(" ") for t in p.find_all(["strong","b"]))
        txt=norm(bold) if bold else norm(p.get_text(" "))
        for m in NAME_SIGLA_RE.finditer(txt):
            nome=norm(m.group("nome")); sigla=norm(m.group("sigla"))
            if 2<=len(sigla)<=10 and len(nome)>=4:
                pairs.append({"nome":nome,"sigla":sigla,"sigla_up":sigla.upper()})
    return pairs

# ---------- Fallback de título: 1º <strong>/<b> após o cabeçalho ----------
def first_bold_after_header(soup:BeautifulSoup):
    # pega os primeiros 6 parágrafos do artigo; retorna o primeiro <strong>/<b> não trivial
    ps = soup.select("article p") or soup.find_all("p")
    for p in ps[:6]:
        for b in p.find_all(["strong","b"]):
            txt=norm(b.get_text(" "))
            if txt and len(txt)>=3 and not re.fullmatch(r"aviso",txt,re.I):
                return txt
    return None

# ---------- Títulos por tabela & captura N-colunas ----------
AVISO_RE = re.compile(r"^\s*aviso\s*$", re.I)

def last_bold_before(node)->str|None:
    for prev in node.find_all_previous():
        name=getattr(prev,"name","")
        if name in ("strong","b"):
            txt=norm(prev.get_text(" "))
            if txt and not AVISO_RE.match(txt): return txt
        if name in ("h2","h3","h4"):
            txt=norm(prev.get_text(" "))
            if txt and not AVISO_RE.match(txt): return txt
    return None

def extract_table_sections(soup:BeautifulSoup):
    sections=[]
    for tb in soup.find_all("table"):
        rows=[]
        header=[]
        # descobre cabeçalho
        thead=tb.find("thead")
        if thead:
            ths=thead.find_all(["th","td"])
            header=[norm(th.get_text(" ")) for th in ths]
        else:
            # se a primeira linha tiver TH, trate-a como cabeçalho
            first_tr=tb.find("tr")
            if first_tr and first_tr.find("th"):
                header=[norm(x.get_text(" ")) for x in first_tr.find_all(["th","td"])]
        # todas as linhas
        for tr in tb.find_all("tr"):
            cells=[norm(td.get_text(" ")) for td in tr.find_all(["td","th"])]
            if cells:
                rows.append(cells)

        # remove header duplicado da lista de dados
        if header and rows and [c.lower() for c in rows[0]] == [c.lower() for c in header]:
            rows=rows[1:]

        # valida: ao menos 2 linhas e 2 colunas
        if len(rows)>=2 and max(len(r) for r in rows)>=2:
            cols=max(len(r) for r in rows)
            # normaliza cada linha para o mesmo nº de colunas
            norm_rows=[ (r + [""]*cols)[:cols] for r in rows ]
            titulo=last_bold_before(tb) or "Resumo"
            sec={"titulo":titulo, "headers":header[:cols] if header else [], "rows":norm_rows, "cols":cols}
            # compat: se for 2 colunas, também exponha em 'linhas'
            if cols==2:
                sec["linhas"]=[{"etapa":r[0],"data":r[1]} for r in norm_rows]
            sections.append(sec)
    return sections

# ---------- parse ----------
def parse_post(url:str):
    soup=soup_of(url)

    # título do post (metadado, não exibido se houver display_title)
    title_meta=soup.find("meta",{"property":"og:title"})
    title=title_meta.get("content","") if title_meta else ""
    if not title:
        h1=soup.find(["h1","h2"])
        title=h1.get_text(" ",strip=True) if h1 else url
    title=norm(title)

    # imagem
    ogimg=soup.find("meta",{"property":"og:image"})
    image=ogimg.get("content","") if ogimg else ""
    if not image:
        imgel=soup.find("img")
        if imgel and imgel.get("src"): image=urljoin(url,imgel["src"])

    # publicado em
    posted_at=None
    meta_pub=soup.find("meta",{"property":"article:published_time"}) \
             or soup.find("meta",{"name":"article:published_time"}) \
             or soup.find("time",{"itemprop":"datePublished"})
    if meta_pub:
        posted_at=(meta_pub.get("content") or meta_pub.get("datetime") or "").strip() or None

    # tabelas
    secoes=extract_table_sections(soup)
    if not secoes:
        print(f"  × DESCARTADO: {title} | sem tabelas no padrão")
        return None

    # link oficial
    link_banca=extract_official_link(soup, url)
    if not link_banca:
        print(f"  × DESCARTADO: {title} | link_banca —")
        return None

    # OCR + Nome(SIGLA) + fallback de título por <strong>
    instituicao=ocr_instituicao_from_image(image)
    pairs=find_nome_sigla_pairs(soup)

    display_title=None          # título provisório (de pares/strong)
    instituicao_full=None       # "Nome Completo (SIGLA)" para fallback do display_title

    if pairs:
        ocr_sig=instituicao.upper() if looks_like_sigla(instituicao) else None
        picked=None
        if ocr_sig:
            for pr in pairs:
                if pr["sigla_up"]==ocr_sig: picked=pr; break
        if not picked: picked=pairs[0]
        display_title=f'{picked["nome"]} ({picked["sigla"]})'
        instituicao_full=display_title
        instituicao=picked["sigla"]
    else:
        # sem Nome(SIGLA): usa primeiro <strong>/<b> após cabeçalho
        fb=first_bold_after_header(soup)
        if fb:
            display_title=fb
            instituicao_full=fb

    # ---- NOVO: força o display_title a vir do padrão "Resumo Edital <NOME> 20xx" ----
    first_section_title = secoes[0].get("titulo") or ""
    display_title = build_display_title(
        first_section_title,
        instituicao_full=instituicao_full or "",
        nome_fallback=display_title or title
    )

    captured_at=datetime.now(timezone.utc).isoformat()
    print(f"  ✓ {title} | tabelas={len(secoes)} | banca=OK")

    dados_first=secoes[0].get("linhas") or []  # compat 2-col

    return {
        "slug": slugify(title),
        "nome": title,
        "display_title": display_title,
        "instituicao": instituicao,
        "link": url,
        "imagem": image,
        "dados": dados_first,   # compat (apenas se 2-col)
        "secoes": secoes,       # novo (N colunas)
        "link_banca": link_banca,
        "posted_at": posted_at,
        "captured_at": captured_at,
    }

def merge(existing:list, new_items:list):
    by={x.get("link"):x for x in existing if isinstance(x,dict) and x.get("link")}
    for it in new_items:
        prev=by.get(it["link"],{})
        merged={**prev, **{k:v for k,v in it.items() if v is not None}}
        by[it["link"]]=merged
    def key(x): return (x.get("posted_at") or x.get("captured_at") or "")
    return sorted(by.values(), key=key, reverse=True)

def main():
    try:
        existing=json.loads(OUT_PATH.read_text(encoding="utf-8"))
        if not isinstance(existing,list): existing=[]
    except Exception: existing=[]
    urls=list_article_urls(limit=30)
    items=[]
    for u in urls:
        try:
            it=parse_post(u)
            if it: items.append(it)
            time.sleep(0.5)
        except Exception as e:
            print(f"  ! erro em {u}: {e}")
    if not items and not existing:
        OUT_PATH.parent.mkdir(parents=True,exist_ok=True)
        OUT_PATH.write_text("[]",encoding="utf-8"); print("[!] Sem itens.")
        return
    final=merge(existing,items)
    OUT_PATH.parent.mkdir(parents=True,exist_ok=True)
    OUT_PATH.write_text(json.dumps(final,ensure_ascii=False,indent=2),encoding="utf-8")
    print(f"[i] Gravado {OUT_PATH} com {len(final)} registros.")

if __name__=="__main__":
    main()
