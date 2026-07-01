"""
dedup_drive.py
Récupère les zips du dossier Drive, les trie par numéro de batch, et supprime
les doublons.
Logique : même numéro de batch = doublon → garde le plus ancien, supprime les autres.
Nom des zips attendu : pages_html_batch00001_20260629_092407.zip (les fichiers
qui ne matchent pas ce pattern sont ignorés pour la suppression et listés à
part dans le rapport).

Mode DRY_RUN (activé par défaut) : le script liste et trie les zips, écrit le
rapport en indiquant CE QUI SERAIT supprimé, mais n'appelle jamais l'API de
suppression. Pour supprimer réellement, passer DRY_RUN=false.

Écrit : rapport_dedup.csv
"""

import csv
import os
import re
from collections import defaultdict

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

# ─── CONFIG ───────────────────────────────────────────────────────────────────

GDRIVE_FOLDER_ID = os.environ.get("GDRIVE_FOLDER_ID")
RAPPORT_CSV      = "rapport_dedup.csv"
DRY_RUN          = os.environ.get("DRY_RUN", "true").strip().lower() not in ("false", "0", "no")

# Pattern des zips à traiter (adapter si le format de nom change)
ZIP_NAME_PATTERN = re.compile(r"^pages_html_batch(\d+)_")

# ─── DRIVE ────────────────────────────────────────────────────────────────────

def get_drive_service():
    creds = Credentials(
        token=None,
        refresh_token=os.environ["GOOGLE_OAUTH_REFRESH_TOKEN"],
        client_id=os.environ["GOOGLE_OAUTH_CLIENT_ID"],
        client_secret=os.environ["GOOGLE_OAUTH_CLIENT_SECRET"],
        token_uri="https://oauth2.googleapis.com/token",
        scopes=["https://www.googleapis.com/auth/drive"],
    )
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def lister_zips(service):
    """Récupère uniquement les .zip du dossier (pagination complète)."""
    resultats = []
    page_token = None
    while True:
        resp = service.files().list(
            q=(
                f"'{GDRIVE_FOLDER_ID}' in parents "
                "and mimeType != 'application/vnd.google-apps.folder' "
                "and name contains '.zip' and trashed=false"
            ),
            fields="nextPageToken, files(id, name, createdTime, size)",
            orderBy="name",
            pageToken=page_token,
            pageSize=1000,
        ).execute()
        resultats.extend(resp.get("files", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return resultats


def supprimer_fichier(service, file_id: str):
    service.files().delete(fileId=file_id).execute()


# ─── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    service = get_drive_service()

    mode = "DRY-RUN (aucune suppression)" if DRY_RUN else "SUPPRESSION RÉELLE"
    print(f"🔧 Mode : {mode}", flush=True)

    print("📋 Récupération des zips sur Drive...", flush=True)
    zips = lister_zips(service)
    print(f"   {len(zips)} zips trouvés\n")

    # Grouper par numéro de batch
    # Nom attendu : pages_html_batch00001_20260629_092407.zip
    par_batch = defaultdict(list)
    hors_pattern = []

    for f in zips:
        match = ZIP_NAME_PATTERN.match(f["name"])
        if match:
            num_batch = match.group(1)
            par_batch[num_batch].append(f)
        else:
            hors_pattern.append(f)

    rapport = []
    total_a_supprimer = 0
    total_supprimes = 0

    # Traiter les batches dans l'ordre numérique (pas alphabétique)
    for num_batch in sorted(par_batch, key=int):
        fichiers = par_batch[num_batch]

        if len(fichiers) == 1:
            # pas de doublon
            rapport.append({
                "batch": num_batch,
                "zip": fichiers[0]["name"],
                "statut": "CONSERVÉ",
            })
            continue

        # trier par date de création (le plus ancien en premier)
        fichiers_tries = sorted(fichiers, key=lambda f: f.get("createdTime", ""))
        a_garder    = fichiers_tries[0]
        a_supprimer = fichiers_tries[1:]
        total_a_supprimer += len(a_supprimer)

        print(f"Batch {num_batch} — {len(fichiers)} copies → garde : {a_garder['name']}")

        rapport.append({
            "batch": num_batch,
            "zip": a_garder["name"],
            "statut": "CONSERVÉ",
        })

        for f in a_supprimer:
            if DRY_RUN:
                print(f"   🔎 [dry-run] serait supprimé : {f['name']}")
                rapport.append({
                    "batch": num_batch,
                    "zip": f["name"],
                    "statut": "À_SUPPRIMER (dry-run)",
                })
                continue

            try:
                supprimer_fichier(service, f["id"])
                print(f"   🗑️  Supprimé : {f['name']}")
                rapport.append({
                    "batch": num_batch,
                    "zip": f["name"],
                    "statut": "SUPPRIMÉ",
                })
                total_supprimes += 1
            except Exception as e:
                print(f"   ❌ Échec suppression {f['name']} : {e}")
                rapport.append({
                    "batch": num_batch,
                    "zip": f["name"],
                    "statut": f"ERREUR: {e}",
                })

    # fichiers hors pattern, listés à part, jamais touchés
    for f in hors_pattern:
        rapport.append({
            "batch": "",
            "zip": f["name"],
            "statut": "IGNORÉ (nom hors pattern)",
        })

    # rapport
    with open(RAPPORT_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["batch", "zip", "statut"])
        w.writeheader()
        w.writerows(rapport)

    print(f"\n─── RÉSUMÉ ───────────────────────────────")
    print(f"Mode           : {mode}")
    print(f"Zips analysés  : {len(zips)}")
    print(f"Batches uniques: {len(par_batch)}")
    print(f"Hors pattern   : {len(hors_pattern)}")
    if DRY_RUN:
        print(f"À supprimer    : {total_a_supprimer} (relancer avec DRY_RUN=false pour exécuter)")
    else:
        print(f"Supprimés      : {total_supprimes}")
    print(f"Rapport        : {RAPPORT_CSV}")


if __name__ == "__main__":
    main()
