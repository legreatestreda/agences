"""
extraire_infos_drive.py
Télécharge les zips depuis Drive, lit le HTML, extrait les infos clés via Fireworks AI.
Écrit : extraction_resultats.csv
Progress : extraction_progress.json (reprise automatique)
"""

import csv
import io
import json
import os
import re
import time
import zipfile

from bs4 import BeautifulSoup
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from openai import OpenAI

# ─── CONFIG ───────────────────────────────────────────────────────────────────

GDRIVE_FOLDER_ID  = os.environ.get("GDRIVE_FOLDER_ID")
OUTPUT_CSV        = "extraction_resultats.csv"
PROGRESS_FILE     = "extraction_progress.json"

# Fireworks AI
FIREWORKS_API_KEY = os.environ.get("FIREWORKS_API_KEY")
MODEL             = "accounts/fireworks/models/deepseek-v4-flash"

# Limite de caractères envoyés à l'IA par site (réduit les tokens)
MAX_CHARS = 5000
# Pause entre chaque appel IA (évite le rate limit)
DELAY     = 0.3

# ─── CLIENTS ──────────────────────────────────────────────────────────────────

def get_drive_service():
    creds = Credentials(
        token=None,
        refresh_token=os.environ["GOOGLE_OAUTH_REFRESH_TOKEN"],
        client_id=os.environ["GOOGLE_OAUTH_CLIENT_ID"],
        client_secret=os.environ["GOOGLE_OAUTH_CLIENT_SECRET"],
        token_uri="https://oauth2.googleapis.com/token",
        scopes=["https://www.googleapis.com/auth/drive.readonly"],
    )
    return build("drive", "v3", credentials=creds, cache_discovery=False)


llm = OpenAI(
    api_key=FIREWORKS_API_KEY,
    base_url="https://api.fireworks.ai/inference/v1",
)

# ─── PROMPT ───────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """Tu es un extracteur de données pour des agences immobilières françaises.
À partir du texte d'un site web, extrais ces informations en JSON uniquement, sans texte autour :
{
  "email": "adresse email ou vide",
  "nom_gerant": "nom du gérant/directeur/responsable ou vide",
  "nb_annonces": "nombre d'annonces/biens (chiffre uniquement) ou vide",
  "taille_equipe": "nombre de personnes dans l'équipe (chiffre uniquement) ou vide",
  "crm_detecte": "logiciel CRM détecté (Apimo, Netty, etc.) ou vide"
}
Si une info est absente, laisse "". Ne devine pas. Retourne UNIQUEMENT le JSON, rien d'autre."""

# ─── DRIVE ────────────────────────────────────────────────────────────────────

def lister_zips(service):
    resultats = []
    page_token = None
    while True:
        resp = service.files().list(
            q=f"'{GDRIVE_FOLDER_ID}' in parents and name contains '.zip' and trashed=false",
            fields="nextPageToken, files(id, name)",
            pageToken=page_token,
            pageSize=1000,
        ).execute()
        resultats.extend(resp.get("files", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return sorted(resultats, key=lambda f: f["name"])


def telecharger_zip(service, file_id: str) -> bytes:
    request = service.files().get_media(fileId=file_id)
    buf = io.BytesIO()
    dl = MediaIoBaseDownload(buf, request)
    done = False
    while not done:
        _, done = dl.next_chunk()
    return buf.getvalue()

# ─── HTML → TEXTE ─────────────────────────────────────────────────────────────

def html_to_text(html: str) -> str:
    try:
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "meta", "link", "noscript"]):
            tag.decompose()
        return " ".join(soup.get_text(separator=" ").split())
    except Exception:
        return ""

# ─── LLM ──────────────────────────────────────────────────────────────────────

def extraire_via_ia(texte: str) -> dict:
    vide = {"email": "", "nom_gerant": "", "nb_annonces": "", "taille_equipe": "", "crm_detecte": ""}
    try:
        resp = llm.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user",   "content": texte[:MAX_CHARS]},
            ],
            temperature=0,
            max_tokens=200,
        )
        raw = resp.choices[0].message.content.strip()
        raw = re.sub(r"```json|```", "", raw).strip()
        return {**vide, **json.loads(raw)}
    except Exception as e:
        return {**vide, "_erreur": str(e)}

# ─── PROGRESS ─────────────────────────────────────────────────────────────────

def charger_progress() -> set:
    if os.path.exists(PROGRESS_FILE):
        with open(PROGRESS_FILE, encoding="utf-8") as f:
            return set(json.load(f).get("traites", []))
    return set()

def sauver_progress(traites: set):
    with open(PROGRESS_FILE, "w", encoding="utf-8") as f:
        json.dump({"traites": list(traites)}, f)

# ─── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    traites = charger_progress()
    reprise = len(traites) > 0

    drive = get_drive_service()
    zips  = lister_zips(drive)

    zips_restants = [z for z in zips if z["name"] not in traites]
    print(f"{'▶️  Reprise' if reprise else '🚀 Démarrage'} — {len(zips)} zips au total | {len(traites)} déjà traités | {len(zips_restants)} restants\n")

    mode = "a" if reprise else "w"
    with open(OUTPUT_CSV, mode, newline="", encoding="utf-8") as f_out:
        writer = csv.DictWriter(f_out, fieldnames=[
            "zip", "site", "email", "nom_gerant",
            "nb_annonces", "taille_equipe", "crm_detecte",
        ])
        if not reprise:
            writer.writeheader()

        total_sites = 0

        for i, fichier in enumerate(zips_restants, 1):
            nom_zip = fichier["name"]
            print(f"\n📦 [{i}/{len(zips_restants)}] {nom_zip}", flush=True)

            try:
                contenu = telecharger_zip(drive, fichier["id"])
            except Exception as e:
                print(f"   ❌ Téléchargement échoué : {e}")
                continue

            # grouper les pages HTML par site (dossier de 1er niveau)
            sites: dict[str, str] = {}
            try:
                with zipfile.ZipFile(io.BytesIO(contenu)) as zf:
                    for chemin in zf.namelist():
                        parties = chemin.split("/")
                        if len(parties) >= 2 and chemin.endswith(".html") and parties[0]:
                            site_id = parties[0]
                            html    = zf.read(chemin).decode("utf-8", errors="ignore")
                            texte   = html_to_text(html)
                            sites[site_id] = sites.get(site_id, "") + " " + texte
            except Exception as e:
                print(f"   ❌ Lecture zip échouée : {e}")
                continue

            for site_id, texte_complet in sites.items():
                total_sites += 1
                print(f"   [{total_sites}] {site_id[:50]}", end=" ... ", flush=True)

                infos = extraire_via_ia(texte_complet)

                writer.writerow({
                    "zip":          nom_zip,
                    "site":         site_id,
                    "email":        infos.get("email", ""),
                    "nom_gerant":   infos.get("nom_gerant", ""),
                    "nb_annonces":  infos.get("nb_annonces", ""),
                    "taille_equipe":infos.get("taille_equipe", ""),
                    "crm_detecte":  infos.get("crm_detecte", ""),
                })
                f_out.flush()

                if infos.get("_erreur"):
                    print(f"⚠️  {infos['_erreur']}")
                else:
                    email  = infos.get("email")   or "-"
                    gerant = infos.get("nom_gerant") or "-"
                    print(f"✅  email={email}  gérant={gerant}")

                time.sleep(DELAY)

            traites.add(nom_zip)
            sauver_progress(traites)

    print(f"\n─── TERMINÉ ───────────────────────────────")
    print(f"Zips traités  : {len(zips_restants)}")
    print(f"Sites traités : {total_sites}")
    print(f"Résultats     : {OUTPUT_CSV}")


if __name__ == "__main__":
    main()
