"""
extract_and_analyze.py
Traite les zips téléchargés localement, extrait les infos via Fireworks AI.
Lit    : *.zip dans le dossier courant + progress.json
Écrit  : extraction_results.json (append) + progress.json (mise à jour)
"""

import glob
import json
import os
import time
import zipfile

import requests
from bs4 import BeautifulSoup

# ─── CONFIG ───────────────────────────────────────────────────────────────────

API_KEY       = os.getenv("FIREWORKS_API_KEY")
MODEL         = "accounts/fireworks/models/deepseek-v4-flash"
BASE_URL      = "https://api.fireworks.ai/inference/v1/chat/completions"
PROGRESS_FILE = "progress.json"
OUTPUT_FILE   = "extraction_results.json"
MAX_CHARS     = 20000

SYSTEM_PROMPT = """Tu es un extracteur de données pour des agences immobilières françaises.
À partir du texte d'un site web, extrais ces informations en JSON uniquement, sans texte autour :
{
  "email": "adresse email ou vide",
  "nom_gerant": "nom du gérant/directeur/responsable ou vide",
  "nb_annonces": "nombre d'annonces/biens (chiffre uniquement) ou vide",
  "taille_equipe": "nombre de personnes dans l'équipe (chiffre uniquement) ou vide",
  "crm_detecte": "logiciel CRM détecté (Apimo, Netty, etc.) ou vide"
}
Si une info est absente, laisse "". Ne devine pas."""

# ─── PROGRESS ─────────────────────────────────────────────────────────────────

def charger_progress():
    if os.path.exists(PROGRESS_FILE):
        with open(PROGRESS_FILE, encoding="utf-8") as f:
            return set(json.load(f).get("traites", []))
    return set()

def sauver_progress(traites: set):
    with open(PROGRESS_FILE, "w", encoding="utf-8") as f:
        json.dump({"traites": list(traites)}, f, ensure_ascii=False)

# ─── RÉSULTATS ────────────────────────────────────────────────────────────────

def charger_resultats():
    if os.path.exists(OUTPUT_FILE):
        with open(OUTPUT_FILE, encoding="utf-8") as f:
            return json.load(f)
    return []

def sauver_resultats(resultats: list):
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(resultats, f, ensure_ascii=False, indent=2)

# ─── HTML ─────────────────────────────────────────────────────────────────────

def clean_html(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header"]):
        tag.decompose()
    return " ".join(soup.get_text(separator=" ", strip=True).split())

# ─── LLM ──────────────────────────────────────────────────────────────────────

def analyser(texte: str) -> dict:
    vide = {"email": "", "nom_gerant": "", "nb_annonces": "", "taille_equipe": "", "crm_detecte": ""}
    try:
        resp = requests.post(BASE_URL, headers={
            "Authorization": f"Bearer {API_KEY}",
            "Content-Type": "application/json",
        }, json={
            "model": MODEL,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user",   "content": f"Voici le texte du site :\n\n{texte[:MAX_CHARS]}"},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0,
            "max_tokens": 200,
        }, timeout=30)
        resp.raise_for_status()
        raw = resp.json()["choices"][0]["message"]["content"]
        return {**vide, **json.loads(raw)}
    except Exception as e:
        return {**vide, "_erreur": str(e)}

# ─── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    debut_global = time.time()
    traites   = charger_progress()
    resultats = charger_resultats()

    zips_locaux    = sorted(glob.glob("*.zip"))
    zips_a_traiter = [z for z in zips_locaux if os.path.basename(z) not in traites]

    print("=" * 60)
    print(f"🚀 DÉMARRAGE EXTRACTION")
    print(f"   Zips locaux     : {len(zips_locaux)}")
    print(f"   Déjà traités    : {len(traites)}")
    print(f"   À traiter       : {len(zips_a_traiter)}")
    print(f"   Résultats existants : {len(resultats)}")
    print("=" * 60)

    if not zips_a_traiter:
        print("✅ Rien à traiter — tous les zips ont été traités.")
        return

    nb_sites      = 0
    nb_erreurs    = 0
    nb_emails     = 0
    nb_gerants    = 0

    for i, zip_path in enumerate(zips_a_traiter, 1):
        nom_zip    = os.path.basename(zip_path)
        debut_zip  = time.time()
        print(f"\n{'─'*60}")
        print(f"📦 [{i}/{len(zips_a_traiter)}] {nom_zip}")

        # grouper les pages HTML par site
        sites: dict[str, str] = {}
        try:
            with zipfile.ZipFile(zip_path, "r") as z:
                for info in z.infolist():
                    if info.filename.endswith(".html") and not info.filename.startswith("__MACOSX"):
                        parties = info.filename.split("/")
                        site_id = parties[0] if len(parties) > 1 else "racine"
                        html    = z.read(info.filename).decode("utf-8", errors="ignore")
                        texte   = clean_html(html)
                        sites[site_id] = sites.get(site_id, "") + " " + texte
            print(f"   → {len(sites)} sites détectés dans ce zip")
        except Exception as e:
            print(f"   ❌ Erreur lecture zip : {e}")
            continue

        zip_ok = zip_err = 0
        for site_id, texte_complet in sites.items():
            nb_sites += 1
            debut_site = time.time()
            print(f"   [{nb_sites}] {site_id[:45]}", end=" ... ", flush=True)

            infos = analyser(texte_complet)
            duree = time.time() - debut_site

            resultats.append({
                "zip":           nom_zip,
                "site":          site_id,
                "email":         infos.get("email", ""),
                "nom_gerant":    infos.get("nom_gerant", ""),
                "nb_annonces":   infos.get("nb_annonces", ""),
                "taille_equipe": infos.get("taille_equipe", ""),
                "crm_detecte":   infos.get("crm_detecte", ""),
            })

            if infos.get("_erreur"):
                print(f"⚠️  {infos['_erreur']} ({duree:.1f}s)")
                nb_erreurs += 1
                zip_err    += 1
            else:
                email  = infos.get("email")    or "-"
                gerant = infos.get("nom_gerant") or "-"
                print(f"✅ email={email}  gérant={gerant}  ({duree:.1f}s)")
                zip_ok += 1
                if infos.get("email"):    nb_emails  += 1
                if infos.get("nom_gerant"): nb_gerants += 1

        duree_zip = time.time() - debut_zip
        print(f"   ✔ Zip terminé en {duree_zip:.0f}s — {zip_ok} OK | {zip_err} erreurs")

        traites.add(nom_zip)
        sauver_progress(traites)
        sauver_resultats(resultats)
        os.remove(zip_path)

    duree_totale = time.time() - debut_global
    print(f"\n{'='*60}")
    print(f"✅ EXTRACTION TERMINÉE")
    print(f"   Durée totale    : {duree_totale/60:.1f} min")
    print(f"   Zips traités    : {len(zips_a_traiter)}")
    print(f"   Sites traités   : {nb_sites}")
    print(f"   Emails trouvés  : {nb_emails} ({nb_emails/nb_sites*100:.0f}%)" if nb_sites else "   Sites traités   : 0")
    print(f"   Gérants trouvés : {nb_gerants} ({nb_gerants/nb_sites*100:.0f}%)" if nb_sites else "")
    print(f"   Erreurs API     : {nb_erreurs}")
    print(f"   Total résultats : {len(resultats)}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
