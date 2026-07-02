"""
scan_pages.py
Lit les zips depuis Google Drive et recense toutes les pages HTML trouvées.
N'ouvre PAS le HTML — juste la liste des fichiers dans chaque zip (ultra rapide).

Écrit :
  - scan_pages.csv        : une ligne par page (zip, site, page)
  - scan_slugs.csv        : classement des slugs par fréquence
"""

import csv
import io
import json
import os
import time
import zipfile
from collections import Counter

import requests

# ─── CONFIG ───────────────────────────────────────────────────────────────────

CLIENT_ID     = os.getenv("GOOGLE_OAUTH_CLIENT_ID")
CLIENT_SECRET = os.getenv("GOOGLE_OAUTH_CLIENT_SECRET")
REFRESH_TOKEN = os.getenv("GOOGLE_OAUTH_REFRESH_TOKEN")
FOLDER_ID     = os.getenv("GDRIVE_FOLDER_ID")

OUTPUT_PAGES  = "scan_pages.csv"
OUTPUT_SLUGS  = "scan_slugs.csv"

# ─── AUTH ─────────────────────────────────────────────────────────────────────

def get_access_token():
    resp = requests.post("https://oauth2.googleapis.com/token", data={
        "client_id":     CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "refresh_token": REFRESH_TOKEN,
        "grant_type":    "refresh_token",
    })
    token = resp.json().get("access_token")
    if not token:
        raise RuntimeError(f"Token impossible : {resp.text}")
    return token

# ─── DRIVE ────────────────────────────────────────────────────────────────────

def lister_zips(token):
    headers = {"Authorization": f"Bearer {token}"}
    resultats, page_token = [], None
    while True:
        params = {
            "q": f"'{FOLDER_ID}' in parents and name contains '.zip' and trashed=false",
            "fields": "nextPageToken, files(id, name)",
            "pageSize": 1000,
        }
        if page_token:
            params["pageToken"] = page_token
        resp = requests.get(
            "https://www.googleapis.com/drive/v3/files",
            headers=headers, params=params
        )
        data = resp.json()
        resultats.extend(data.get("files", []))
        page_token = data.get("nextPageToken")
        if not page_token:
            break
    return sorted(resultats, key=lambda f: f["name"])


def telecharger_zip(token, file_id) -> bytes:
    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.get(
        f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media",
        headers=headers, stream=True
    )
    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code} — {resp.text[:200]}")
    return resp.content

# ─── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    token = get_access_token()
    token_time = time.time()
    zips  = lister_zips(token)

    print(f"📋 {len(zips)} zips trouvés sur Drive\n")

    compteur_slugs = Counter()
    nb_sites       = 0
    nb_pages       = 0

    with open(OUTPUT_PAGES, "w", newline="", encoding="utf-8") as f_pages:
        writer = csv.DictWriter(f_pages, fieldnames=["zip", "site", "page"])
        writer.writeheader()

        for i, fichier in enumerate(zips, 1):
            nom_zip = fichier["name"]
            print(f"[{i}/{len(zips)}] {nom_zip}", end=" ... ", flush=True)

            # rafraîchir le token avant qu'il expire (durée de vie ~1h)
            if time.time() - token_time > 45 * 60:
                token = get_access_token()
                token_time = time.time()

            try:
                contenu = telecharger_zip(token, fichier["id"])
                with zipfile.ZipFile(io.BytesIO(contenu)) as zf:
                    # lister les fichiers sans extraire le contenu
                    noms = [n for n in zf.namelist() if n.endswith(".html") and not n.startswith("__MACOSX")]

                    # grouper par site (dossier de 1er niveau)
                    sites_dans_zip = {}
                    for chemin in noms:
                        parties = chemin.split("/")
                        if len(parties) >= 2:
                            site_id  = parties[0]
                            nom_page = parties[-1].replace(".html", "")  # ex: "contact", "index"
                            if site_id not in sites_dans_zip:
                                sites_dans_zip[site_id] = []
                            sites_dans_zip[site_id].append(nom_page)

                    for site_id, pages in sites_dans_zip.items():
                        nb_sites += 1
                        for page in pages:
                            nb_pages += 1
                            compteur_slugs[page] += 1
                            writer.writerow({
                                "zip":  nom_zip,
                                "site": site_id,
                                "page": page,
                            })

                    print(f"✅ {len(sites_dans_zip)} sites | {len(noms)} pages")

            except Exception as e:
                print(f"❌ {e}")
                continue

    # ─── CLASSEMENT DES SLUGS ─────────────────────────────────────────────────
    with open(OUTPUT_SLUGS, "w", newline="", encoding="utf-8") as f_slugs:
        writer = csv.DictWriter(f_slugs, fieldnames=["slug", "occurrences", "pct_sites"])
        writer.writeheader()
        for slug, count in compteur_slugs.most_common():
            writer.writerow({
                "slug":        slug,
                "occurrences": count,
                "pct_sites":   f"{count/nb_sites*100:.1f}%" if nb_sites else "0%",
            })

    # ─── RÉSUMÉ TERMINAL ──────────────────────────────────────────────────────
    print(f"\n{'='*50}")
    print(f"✅ SCAN TERMINÉ")
    print(f"   Zips scannés   : {len(zips)}")
    print(f"   Sites uniques  : {nb_sites}")
    print(f"   Pages totales  : {nb_pages}")
    print(f"\n🏆 TOP 20 des pages les plus fréquentes :")
    for slug, count in compteur_slugs.most_common(20):
        barre = "█" * int(count / nb_sites * 40)
        print(f"   {slug:<35} {count:>5} sites ({count/nb_sites*100:.0f}%) {barre}")
    print(f"\n   Résultats : {OUTPUT_PAGES} | {OUTPUT_SLUGS}")
    print(f"{'='*50}")


if __name__ == "__main__":
    main()
  
