"""
scraper_agences_enrichi.py
Version GitHub Actions + sauvegarde Google Drive (par lots zippés)
Lit : agences_independantes.csv
Écrit : agences_scrappees.csv, agences_echecs.csv, progress_scraper.json
Upload : un zip de HTML toutes les SAVE_EVERY agences, vers un dossier Drive

──────────────────────────────────────────────────────────────────────────────
CONFIG NÉCESSAIRE (secrets GitHub Actions) — méthode OAuth utilisateur,
car un compte de service n'a pas de quota de stockage sur un Drive perso :

1. GOOGLE_OAUTH_CLIENT_ID       (Google Cloud Console > Credentials)
2. GOOGLE_OAUTH_CLIENT_SECRET   (idem)
3. GOOGLE_OAUTH_REFRESH_TOKEN   (généré une fois en local via get_refresh_token.py)
4. GDRIVE_FOLDER_ID             (ID du dossier Drive cible, dans son URL)

Dans ton workflow .yml :

    env:
      GOOGLE_OAUTH_CLIENT_ID: ${{ secrets.GOOGLE_OAUTH_CLIENT_ID }}
      GOOGLE_OAUTH_CLIENT_SECRET: ${{ secrets.GOOGLE_OAUTH_CLIENT_SECRET }}
      GOOGLE_OAUTH_REFRESH_TOKEN: ${{ secrets.GOOGLE_OAUTH_REFRESH_TOKEN }}
      GDRIVE_FOLDER_ID: ${{ secrets.GDRIVE_FOLDER_ID }}

Dépendances :
    pip install requests google-api-python-client google-auth
──────────────────────────────────────────────────────────────────────────────
"""

import csv
import io
import json
import os
import re
import shutil
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

# ─── CONFIG ───────────────────────────────────────────────────────────────────

AGENCES_CSV   = "agences_independantes.csv"
PROGRESS_FILE = "progress_scraper.json"
PATH_OK       = "agences_scrappees.csv"
PATH_ECHECS   = "agences_echecs.csv"

LOCAL_HTML_DIR    = "html_pages_tmp"   # vidé après chaque upload
ECHECS_UPLOAD_DIR = "zips_echec_upload"  # zips dont l'upload Drive a échoué, conservés pour retry

THREADS_PAR_SITE  = 10
DELAY_ENTRE_SITES = 0.3
TIMEOUT           = 8
SAVE_EVERY        = 50              # taille d'un lot avant zip + upload Drive

GDRIVE_FOLDER_ID = os.environ.get("GDRIVE_FOLDER_ID")

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

FIELDNAMES_OUT = FIELDNAMES_IN + ["pages_trouvees", "nb_pages"]

# ─── GOOGLE DRIVE ─────────────────────────────────────────────────────────────

def get_drive_service():
    """Construit le client Drive via OAuth utilisateur (refresh token), pas un compte de service —
    nécessaire car les comptes de service n'ont pas de quota de stockage sur un Drive perso."""
    client_id     = os.environ.get("GOOGLE_OAUTH_CLIENT_ID")
    client_secret = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET")
    refresh_token = os.environ.get("GOOGLE_OAUTH_REFRESH_TOKEN")

    if not (client_id and client_secret and refresh_token):
        raise RuntimeError(
            "Variables OAuth manquantes : il faut GOOGLE_OAUTH_CLIENT_ID, "
            "GOOGLE_OAUTH_CLIENT_SECRET et GOOGLE_OAUTH_REFRESH_TOKEN."
        )
    if not GDRIVE_FOLDER_ID:
        raise RuntimeError("GDRIVE_FOLDER_ID manquant dans l'environnement.")

    creds = Credentials(
        token=None,
        refresh_token=refresh_token,
        client_id=client_id,
        client_secret=client_secret,
        token_uri="https://oauth2.googleapis.com/token",
        scopes=["https://www.googleapis.com/auth/drive.file"],
    )
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def zipper_dossier(dossier: str, nom_zip: str) -> str | None:
    """Zippe le contenu de `dossier` vers `nom_zip`. Retourne le chemin, ou None si vide."""
    if not os.path.isdir(dossier) or not os.listdir(dossier):
        return None
    with zipfile.ZipFile(nom_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, _, files in os.walk(dossier):
            for fname in files:
                full_path = os.path.join(root, fname)
                arcname = os.path.relpath(full_path, dossier)
                zf.write(full_path, arcname)
    return nom_zip


def upload_zip_drive(service, chemin_zip: str, max_tentatives: int = 4):
    """Upload un fichier zip vers le dossier Drive cible, avec retry en cas d'erreur réseau."""
    nom_fichier = os.path.basename(chemin_zip)
    file_metadata = {"name": nom_fichier, "parents": [GDRIVE_FOLDER_ID]}

    derniere_erreur = None
    for tentative in range(1, max_tentatives + 1):
        try:
            with open(chemin_zip, "rb") as f:
                media = MediaIoBaseUpload(io.BytesIO(f.read()), mimetype="application/zip", resumable=True)
            service.files().create(body=file_metadata, media_body=media, fields="id").execute()
            return  # succès
        except Exception as e:
            derniere_erreur = e
            if tentative < max_tentatives:
                attente = 5 * tentative  # backoff progressif : 5s, 10s, 15s...
                print(f"(tentative {tentative}/{max_tentatives} échouée, retry dans {attente}s) ", end="", flush=True)
                time.sleep(attente)

    raise derniere_erreur


def flush_batch_vers_drive(service, batch_num: int):
    """Zippe LOCAL_HTML_DIR, upload sur Drive (avec retry), puis vide le dossier local.
    Si l'upload échoue après tous les essais, le zip est déplacé dans ECHECS_UPLOAD_DIR
    pour pouvoir être re-uploadé plus tard, au lieu d'être perdu."""
    if not os.path.isdir(LOCAL_HTML_DIR) or not os.listdir(LOCAL_HTML_DIR):
        return

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    nom_zip = f"pages_html_batch{batch_num:05d}_{timestamp}.zip"

    chemin = zipper_dossier(LOCAL_HTML_DIR, nom_zip)
    if not chemin:
        return

    print(f"\n☁️  Upload {nom_zip} vers Drive...", end=" ", flush=True)
    try:
        upload_zip_drive(service, chemin)
        print("✅")
        os.remove(chemin)
    except Exception as e:
        print(f"❌ échec définitif après plusieurs tentatives : {e}")
        os.makedirs(ECHECS_UPLOAD_DIR, exist_ok=True)
        shutil.move(chemin, os.path.join(ECHECS_UPLOAD_DIR, nom_zip))
        print(f"   → zip conservé dans {ECHECS_UPLOAD_DIR}/ pour retry manuel ultérieur")

    # on vide le dossier local de HTML qu'on vient de traiter, qu'il ait réussi ou non à uploader
    # (le contenu est soit sur Drive, soit sauvegardé dans ECHECS_UPLOAD_DIR en zip)
    shutil.rmtree(LOCAL_HTML_DIR, ignore_errors=True)
    os.makedirs(LOCAL_HTML_DIR, exist_ok=True)


def sauver_pages_localement(identifiant_site: str, pages: dict):
    """Écrit les HTML d'un site sur disque, dans LOCAL_HTML_DIR/<identifiant>/<slug>.html"""
    dossier_site = os.path.join(LOCAL_HTML_DIR, sanitize_filename(identifiant_site))
    os.makedirs(dossier_site, exist_ok=True)
    for slug, html_content in pages.items():
        nom_slug = slug if slug else "index"
        nom_slug = sanitize_filename(nom_slug)
        chemin = os.path.join(dossier_site, f"{nom_slug}.html")
        with open(chemin, "w", encoding="utf-8") as f:
            f.write(html_content)


def sanitize_filename(s: str) -> str:
    s = s.strip("/")
    return re.sub(r"[^a-zA-Z0-9_.-]", "_", s) or "page"

# ─── PROGRESS ─────────────────────────────────────────────────────────────────

def charger_progress():
    if os.path.exists(PROGRESS_FILE):
        with open(PROGRESS_FILE, encoding="utf-8") as f:
            data = json.load(f)
            return (
                set(data.get("traites", [])),
                data.get("total_ok", 0),
                data.get("total_echecs", 0),
                data.get("batch_num", 0),
            )
    return set(), 0, 0, 0

def sauver_progress(traites: set, total_ok: int, total_echecs: int, batch_num: int):
    with open(PROGRESS_FILE, "w", encoding="utf-8") as f:
        json.dump({
            "traites": list(traites),
            "total_ok": total_ok,
            "total_echecs": total_echecs,
            "batch_num": batch_num,
        }, f)

# ─── UTILS SCRAPING ───────────────────────────────────────────────────────────

def normaliser_url(url: str) -> str:
    url = url.strip().rstrip("/")
    if not url.startswith("http"):
        url = "https://" + url
    return url

def tester_slug(args):
    session, base_url, slug = args
    url = f"{base_url}/{slug}" if slug else base_url
    try:
        # timeout = (connexion, lecture) séparés : empêche un site qui répond
        # "au goutte-à-goutte" de contourner le timeout global de requests
        resp = session.get(url, headers=HEADERS, timeout=(TIMEOUT, TIMEOUT), allow_redirects=True, stream=False)
        if resp.status_code == 200 and len(resp.text) > 500:
            return slug, resp.text
    except Exception:
        pass
    return slug, None

def scraper_site(base_url: str) -> dict:
    base_url = normaliser_url(base_url)
    session = requests.Session()
    args = [(session, base_url, slug) for slug in SLUGS]
    resultats = {}
    # plafond de temps total pour TOUT le site (toutes les slugs confondues) :
    # même si requests.timeout est contourné par un site capricieux, on abandonne
    # ce site après TIMEOUT_TOTAL_SITE secondes, point final.
    TIMEOUT_TOTAL_SITE = TIMEOUT * 3
    with ThreadPoolExecutor(max_workers=THREADS_PAR_SITE) as executor:
        futures = {executor.submit(tester_slug, a): a for a in args}
        try:
            for future in as_completed(futures, timeout=TIMEOUT_TOTAL_SITE):
                slug, html = future.result()
                if html:
                    resultats[slug] = html
        except TimeoutError:
            # certains threads sont encore bloqués : on abandonne ce site,
            # les threads restants mourront en arrière-plan sans bloquer la suite
            pass
    return resultats

# ─── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    traites, cumul_ok, cumul_echecs, batch_num = charger_progress()
    reprise = len(traites) > 0

    os.makedirs(LOCAL_HTML_DIR, exist_ok=True)
    drive_service = get_drive_service()

    if reprise:
        print(f"▶️  Reprise — {len(traites)} agences déjà traitées ({cumul_ok} ✅ | {cumul_echecs} ❌) | batch #{batch_num}")
        print(f"   Reste environ {11000 - len(traites)} agences")
    else:
        print("🚀 Démarrage du scraping (avec upload Drive par lots)...")

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

                    # sauvegarde locale des HTML (pour le batch Drive)
                    identifiant = place_id or urlparse(normaliser_url(web_site)).netloc
                    sauver_pages_localement(identifiant, pages)

                    w_ok.writerow({**row, "pages_trouvees": slugs_list, "nb_pages": len(pages)})
                    ok += 1
                    print(f"✅ {len(pages)} pages")
                else:
                    w_echecs.writerow(row)
                    echecs += 1
                    print("❌")

                if place_id:
                    traites.add(place_id)

                if total % SAVE_EVERY == 0:
                    batch_num += 1
                    flush_batch_vers_drive(drive_service, batch_num)
                    sauver_progress(traites, ok, echecs, batch_num)
                    f_ok.flush()
                    f_echecs.flush()
                    print(f"💾 [{total}] Cumul: {len(traites)} traitées | ✅ {ok} | ❌ {echecs}\n")

                time.sleep(DELAY_ENTRE_SITES)

    except Exception as e:
        print(f"\n⚠️  Erreur : {e}")

    finally:
        # upload final du reliquat (même si < SAVE_EVERY)
        batch_num += 1
        flush_batch_vers_drive(drive_service, batch_num)
        sauver_progress(traites, ok, echecs, batch_num)
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
    
