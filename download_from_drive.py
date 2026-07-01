"""
download_from_drive.py
Télécharge les 40 prochains zips non traités depuis Google Drive.
Lit    : progress.json (pour savoir où on en est)
Écrit  : les zips dans le dossier courant
"""

import json
import os
import requests

CLIENT_ID     = os.getenv("GOOGLE_OAUTH_CLIENT_ID")
CLIENT_SECRET = os.getenv("GOOGLE_OAUTH_CLIENT_SECRET")
REFRESH_TOKEN = os.getenv("GOOGLE_OAUTH_REFRESH_TOKEN")
FOLDER_ID     = os.getenv("GDRIVE_FOLDER_ID")

PROGRESS_FILE = "progress.json"
BATCH_SIZE    = 40


def get_access_token():
    resp = requests.post("https://oauth2.googleapis.com/token", data={
        "client_id":     CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "refresh_token": REFRESH_TOKEN,
        "grant_type":    "refresh_token",
    })
    token = resp.json().get("access_token")
    if not token:
        raise RuntimeError(f"Impossible d'obtenir l'access token : {resp.text}")
    return token


def lister_zips(token):
    """Liste TOUS les zips du dossier Drive cible, triés par nom."""
    headers = {"Authorization": f"Bearer {token}"}
    resultats = []
    page_token = None

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


def charger_progress():
    if os.path.exists(PROGRESS_FILE):
        with open(PROGRESS_FILE, encoding="utf-8") as f:
            return set(json.load(f).get("traites", []))
    return set()


def main():
    token   = get_access_token()
    traites = charger_progress()
    tous    = lister_zips(token)

    restants = [f for f in tous if f["name"] not in traites]
    batch    = restants[:BATCH_SIZE]

    print(f"📦 Drive : {len(tous)} zips total | {len(traites)} traités | {len(restants)} restants")
    print(f"   → Téléchargement du prochain batch : {len(batch)} zips\n")

    if not batch:
        print("✅ Tous les zips ont été traités. Rien à télécharger.")
        return

    headers = {"Authorization": f"Bearer {token}"}
    for f in batch:
        print(f"  ⬇️  {f['name']}", end=" ... ", flush=True)
        resp = requests.get(
            f"https://www.googleapis.com/drive/v3/files/{f['id']}?alt=media",
            headers=headers,
            stream=True,
        )
        with open(f["name"], "wb") as out:
            for chunk in resp.iter_content(chunk_size=8192):
                out.write(chunk)
        print("✅")

    print(f"\n{len(batch)} zips téléchargés.")


if __name__ == "__main__":
    main()
