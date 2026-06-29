"""
scraper_agences_enrichi.py
Version GitHub Actions — pas de sauvegarde HTML sur disque (espace limité)
Lit : agences_independantes.csv
Écrit : agences_scrappees.csv, agences_echecs.csv, progress_scraper.json
"""

import csv
import os
import re
import time
import json
import requests
from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed

# ─── CONFIG ───────────────────────────────────────────────────────────────────

AGENCES_CSV   = "agences_independantes.csv"
PROGRESS_FILE = "progress_scraper.json"
PATH_OK       = "agences_scrappees.csv"
PATH_ECHECS   = "agences_echecs.csv"

THREADS_PAR_SITE  = 10
DELAY_ENTRE_SITES = 0.3
TIMEOUT           = 8
SAVE_EVERY        = 50

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "fr-FR,fr;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

SLUGS = [
    "",
    "mentions-legales",
    "mentions-legales/",
    "mentions-legales.html",
    "mentions-legales.php",
    "mentions_legales",
    "equipe",
    "notre-equipe",
    "nos-agents",
    "a-propos",
    "qui-sommes-nous",
    "notre-agence",
    "agence",
    "annonces",
    "vente",
    "ventes",
    "biens",
    "nos-biens",
    "politique-de-confidentialite",
    "contact",
    "nos-biens-a-vendre",
    "nos-proprietes",
    "nos-offres",
    "equipe-agence",
    "contactez-nous",
    "notre-equipe-de-professionnels",
    "nos-conseillers",
    "nos-experts",
    "liste-des-biens",
    "toutes-nos-annonces",
    "nos-mandats",
]

FIELDNAMES_IN = [
    "title", "address", "phone", "web_site",
    "review_count", "review_rating", "segment",
    "review_1_name", "review_1_note", "review_1_texte", "review_1_date",
    "review_2_name", "review_2_note", "review_2_texte", "review_2_date",
    "review_3_name", "review_3_note", "review_3_texte", "review_3_date",
    "place_id", "cid", "latitude", "longitude", "link"
]

FIELDNAMES_OUT = FIELDNAMES_IN + ["pages_trouvees", "nb_pages", "equipe_info", "responsable_info", "nombre_annonces"]

# ─── PROGRESS ─────────────────────────────────────────────────────────────────

def charger_progress():
    if os.path.exists(PROGRESS_FILE):
        with open(PROGRESS_FILE, encoding="utf-8") as f:
            data = json.load(f)
            return set(data.get("traites", [])), data.get("total_ok", 0), data.get("total_echecs", 0)
    return set(), 0, 0

def sauver_progress(traites: set, total_ok: int, total_echecs: int):
    with open(PROGRESS_FILE, "w", encoding="utf-8") as f:
        json.dump({
            "traites": list(traites),
            "total_ok": total_ok,
            "total_echecs": total_echecs
        }, f)

# ─── UTILS ────────────────────────────────────────────────────────────────────

def normaliser_url(url: str) -> str:
    url = url.strip().rstrip("/")
    if not url.startswith("http"):
        url = "https://" + url
    return url

def tester_slug(args):
    session, base_url, slug = args
    url = f"{base_url}/{slug}" if slug else base_url
    try:
        resp = session.get(url, headers=HEADERS, timeout=TIMEOUT, allow_redirects=True)
        if resp.status_code == 200 and len(resp.text) > 500:
            return slug, resp.text
    except Exception:
        pass
    return slug, None

def extract_info_from_html(html_content: str) -> dict:
    equipe_info = ""
    responsable_info = ""
    nombre_annonces = ""

    team_patterns = [
        r"(notre équipe|l'équipe|nos agents|nos conseillers|nos experts)\s*:\s*([\w\s,.-]+)",
        r"(responsable|directeur|gérant)\s*:\s*([\w\s,.-]+)",
        r"(équipe|contact|à propos|qui sommes nous)[^>]*>\s*([\w\s,.-]+(?:<br\s*\/?>[\w\s,.-]+)*)",
    ]
    for pattern in team_patterns:
        match = re.search(pattern, html_content, re.IGNORECASE | re.DOTALL)
        if match:
            equipe_info = match.group(2).strip()
            if any(k in match.group(1).lower() for k in ["responsable", "directeur", "gérant"]):
                responsable_info = equipe_info
            break

    listings_patterns = [
        r"(\d+)\s*(?:annonces|biens|propriétés|offres)\s*(?:disponibles|à vendre)?",
        r"(\d+)\s*mandats",
    ]
    for pattern in listings_patterns:
        match = re.search(pattern, html_content, re.IGNORECASE)
        if match:
            nombre_annonces = match.group(1)
            break

    return {
        "equipe_info": equipe_info,
        "responsable_info": responsable_info,
        "nombre_annonces": nombre_annonces,
    }

def scraper_site(base_url: str) -> dict:
    base_url = normaliser_url(base_url)
    session = requests.Session()
    args = [(session, base_url, slug) for slug in SLUGS]
    resultats = {}
    with ThreadPoolExecutor(max_workers=THREADS_PAR_SITE) as executor:
        futures = {executor.submit(tester_slug, a): a for a in args}
        for future in as_completed(futures):
            slug, html = future.result()
            if html:
                resultats[slug] = html
    return resultats

# ─── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    traites, cumul_ok, cumul_echecs = charger_progress()
    reprise = len(traites) > 0

    if reprise:
        print(f"▶️  Reprise — {len(traites)} agences déjà traitées ({cumul_ok} ✅ | {cumul_echecs} ❌)")
        print(f"   Reste environ {11000 - len(traites)} agences")
    else:
        print("🚀 Démarrage du scraping...")

    mode = "a" if reprise else "w"

    f_ok     = open(PATH_OK,     mode, newline="", encoding="utf-8")
    f_echecs = open(PATH_ECHECS, mode, newline="", encoding="utf-8")
    w_ok     = csv.DictWriter(f_ok,     fieldnames=FIELDNAMES_OUT)
    w_echecs = csv.DictWriter(f_echecs, fieldnames=FIELDNAMES_IN)

    if not reprise:
        w_ok.writeheader()
        w_echecs.writeheader()

    total  = 0
    ok     = cumul_ok
    echecs = cumul_echecs

    try:
        with open(AGENCES_CSV, encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                place_id = row.get("place_id", "")
                if place_id and place_id in traites:
                    continue

                web_site = row.get("web_site", "").strip()
                if not web_site:
                    if place_id:
                        traites.add(place_id)
                    continue

                total += 1
                titre = row.get("title", "")[:40]
                print(f"[{total}] {titre} → {web_site}", end=" ... ", flush=True)

                pages = scraper_site(web_site)

                if pages:
                    slugs_list = "|".join(pages.keys())

                    all_info = {"equipe_info": "", "responsable_info": "", "nombre_annonces": ""}
                    for slug, html_content in pages.items():
                        extracted = extract_info_from_html(html_content)
                        if extracted["equipe_info"] and not all_info["equipe_info"]:
                            all_info["equipe_info"] = extracted["equipe_info"]
                        if extracted["responsable_info"] and not all_info["responsable_info"]:
                            all_info["responsable_info"] = extracted["responsable_info"]
                        if extracted["nombre_annonces"] and not all_info["nombre_annonces"]:
                            all_info["nombre_annonces"] = extracted["nombre_annonces"]

                    w_ok.writerow({**row, "pages_trouvees": slugs_list, "nb_pages": len(pages), **all_info})
                    ok += 1
                    print(f"✅ {len(pages)} pages")
                else:
                    w_echecs.writerow(row)
                    echecs += 1
                    print("❌")

                if place_id:
                    traites.add(place_id)

                if total % SAVE_EVERY == 0:
                    sauver_progress(traites, ok, echecs)
                    f_ok.flush()
                    f_echecs.flush()
                    print(f"\n💾 [{total}] Cumul: {len(traites)} traitées | ✅ {ok} | ❌ {echecs}\n")

                time.sleep(DELAY_ENTRE_SITES)

    except Exception as e:
        print(f"\n⚠️  Erreur : {e}")

    finally:
        sauver_progress(traites, ok, echecs)
        f_ok.close()
        f_echecs.close()

    print("\n─── RÉSULTAT SESSION ───")
    print(f"Traités cette session : {total}")
    print(f"─── CUMUL TOTAL ───")
    print(f"Agences traitées : {len(traites)} / 11000")
    print(f"✅ Scrappées     : {ok}")
    print(f"❌ Échecs        : {echecs}")

if __name__ == "__main__":
    main()
