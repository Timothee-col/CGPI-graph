"""Scraper CNCEF annuaire (filter domaine=patrimoine).

Test mode: --limit N pour limiter le nombre de fiches scrapées.
"""
import argparse
import csv
import re
import sys
import time
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

BASE = "https://www.cncef.org"
LIST_URL = BASE + "/annuaire/page/{page}/?filter_domaine=patrimoine"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
}
SLEEP = 0.7


def fetch(session, url, retries=3):
    for i in range(retries):
        try:
            r = session.get(url, headers=HEADERS, timeout=20)
            if r.status_code == 200:
                return r.text
            if r.status_code == 404:
                return None
        except requests.RequestException as e:
            print(f"  ! {url}: {e}", file=sys.stderr)
        time.sleep(2 ** i)
    return None


def parse_listing(html):
    """Extract fiche URLs from a listing page. Returns list of absolute URLs."""
    soup = BeautifulSoup(html, "lxml")
    urls = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/annuaire/" not in href:
            continue
        if href.rstrip("/").endswith("/annuaire"):
            continue
        if "/page/" in href or "?" in href or "#" in href:
            continue
        full = urljoin(BASE, href)
        # garde uniquement les URLs de type /annuaire/{slug}/
        m = re.match(r"^https://www\.cncef\.org/annuaire/([^/]+)/?$", full)
        if m and m.group(1) not in ("", "page"):
            urls.append(full.rstrip("/") + "/")
    # dédup en gardant l'ordre
    seen = set()
    out = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def collect_urls(session, max_urls):
    """Crawle les pages de listing jusqu'à atteindre max_urls."""
    collected = []
    seen = set()
    page = 1
    while len(collected) < max_urls:
        url = LIST_URL.format(page=page)
        print(f"[list] page {page} -> {url}")
        html = fetch(session, url)
        if not html:
            break
        page_urls = parse_listing(html)
        if not page_urls:
            print(f"  pas de fiche trouvée, arrêt")
            break
        new = [u for u in page_urls if u not in seen]
        for u in new:
            seen.add(u)
            collected.append(u)
            if len(collected) >= max_urls:
                break
        print(f"  +{len(new)} fiches (total {len(collected)})")
        page += 1
        time.sleep(SLEEP)
    return collected[:max_urls]


def text_or_none(el):
    return el.get_text(" ", strip=True) if el else None


def split_prenom_nom(full):
    """'SERALY Valentin' -> ('Valentin', 'SERALY').
    'BONO Etienne' -> ('Etienne', 'BONO').
    'Louis GIBOIN' -> ('Louis', 'GIBOIN').
    Heuristique: les tokens en MAJUSCULES sont le nom de famille."""
    if not full:
        return None, None
    tokens = full.split()
    upper = [t for t in tokens if t == t.upper() and any(c.isalpha() for c in t)]
    other = [t for t in tokens if not (t == t.upper() and any(c.isalpha() for c in t))]
    if upper and other:
        return " ".join(other), " ".join(upper)
    # fallback: dernier token = nom
    if len(tokens) >= 2:
        return " ".join(tokens[:-1]), tokens[-1]
    return None, full


def parse_fiche(html, url):
    soup = BeautifulSoup(html, "lxml")
    out = {"url": url}

    # Masterhead
    out["raison_sociale"] = text_or_none(soup.select_one(".fiche-masterhead__title"))
    region_raw = text_or_none(soup.select_one(".fiche-masterhead__region"))
    out["region"] = re.sub(r"^Région d'activité\s*:\s*", "", region_raw) if region_raw else None
    orias_raw = text_or_none(soup.select_one(".fiche-masterhead__numero"))
    out["orias"] = re.sub(r"^Numéro ORIAS\s*:\s*", "", orias_raw) if orias_raw else None

    # Domaines (badges dans masterhead__right, hors title/region/numero/share)
    domaines = []
    right = soup.select_one(".fiche-masterhead__right")
    if right:
        skip_classes = {"fiche-masterhead__title", "fiche-masterhead__region",
                        "fiche-masterhead__numero", "fiche-masterhead__share"}
        for child in right.find_all(recursive=False):
            cls = set(child.get("class") or [])
            if cls & skip_classes:
                continue
            t = child.get_text(" ", strip=True)
            if t and len(t) < 80:
                domaines.append(t)
    out["domaines"] = " | ".join(domaines) if domaines else None

    # Vos contacts (premier contact)
    contact_name = soup.select_one(".fiche-contact__list__name")
    contact_fonc = soup.select_one(".fiche-contact__list__fonction")
    name_text = text_or_none(contact_name)
    out["contact_full"] = name_text
    out["prenom"], out["nom"] = split_prenom_nom(name_text)
    fonc_text = text_or_none(contact_fonc)
    out["role"] = re.sub(r"\s+", " ", fonc_text) if fonc_text else None

    # Plusieurs contacts ?
    all_contacts = soup.select(".fiche-contact__list__name")
    out["nb_contacts"] = len(all_contacts)

    # Adresse (siège social)
    siege_h2 = soup.find("h2", string=lambda s: s and "Siège social" in s)
    if siege_h2:
        # Le bloc qui contient l'adresse
        block = siege_h2.find_parent()
        if block:
            txt = block.get_text("\n", strip=True)
            # retirer le titre
            lines = [l for l in txt.split("\n") if l and l != "Siège social"
                     and "Contacter" not in l]
            out["adresse"] = " ".join(lines) if lines else None
        else:
            out["adresse"] = None
    else:
        out["adresse"] = None

    # LinkedIn + site web : on cherche les liens dans le bloc fiche, pas dans le footer
    fiche_section = soup.find("section", class_=re.compile(r"fiche-")) or soup
    linkedin = None
    site_web = None
    # chercher dans toute la page
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        text = a.get_text(" ", strip=True).lower()
        if "linkedin.com" in href and "linkedin.com/company/cncef" not in href:
            if not linkedin:
                linkedin = href
        elif text in ("accéder au site", "acceder au site", "site web"):
            site_web = href
    out["linkedin"] = linkedin
    out["site_web"] = site_web

    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--out", default="cncef_test.csv")
    args = ap.parse_args()

    session = requests.Session()

    print(f"=== Étape 1 : collecte d'au moins {args.limit} URLs ===")
    urls = collect_urls(session, args.limit)
    print(f"{len(urls)} URLs collectées")

    print(f"\n=== Étape 2 : scraping des fiches ===")
    fields = ["prenom", "nom", "raison_sociale", "role", "linkedin", "site_web",
              "orias", "domaines", "region", "adresse", "contact_full",
              "nb_contacts", "url"]
    rows = []
    for i, url in enumerate(urls, 1):
        print(f"[{i}/{len(urls)}] {url}")
        html = fetch(session, url)
        if not html:
            print(f"  ! échec")
            continue
        try:
            row = parse_fiche(html, url)
            rows.append(row)
        except Exception as e:
            print(f"  ! parse error: {e}")
        time.sleep(SLEEP)

    with open(args.out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fields})
    print(f"\n=> {len(rows)} lignes écrites dans {args.out}")


if __name__ == "__main__":
    main()
