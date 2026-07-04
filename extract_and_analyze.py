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

# Chaîne de fallback : Gemini retiré (250 req/jour gratuit, trop faible pour
# notre volume — il était épuisé en quelques minutes). On combine seulement
# Groq (rapide, 1000 RPD / 200K TPD sur gpt-oss-120b) et Cerebras (1M
# tokens/jour, le plus gros volume journalier des deux).
# Les 2 exposent une API compatible OpenAI, donc même format de payload partout.
PROVIDERS = [
    {
        "nom":              "groq",
        "base_url":         "https://api.groq.com/openai/v1/chat/completions",
        "api_key":          os.getenv("GROQ_API_KEY"),
        "model":            os.getenv("GROQ_MODEL", "openai/gpt-oss-120b"),
        "intervalle_s":     2.2,   # ~27 req/min, marge sous les 30 RPM publiés
        "reasoning_effort": "low",    # gpt-oss exige un minimum de reasoning, on le réduit au max
    },
    {
        "nom":              "cerebras",
        "base_url":         "https://api.cerebras.ai/v1/chat/completions",
        "api_key":          os.getenv("CEREBRAS_API_KEY"),
        "model":            os.getenv("CEREBRAS_MODEL", "gpt-oss-120b"),
        "intervalle_s":     12.0,  # ~5 req/min, aligné sur la vraie limite officielle (5 RPM / 30K TPM / 1M TPD)
        "reasoning_effort": "low",
    },
]

# horodatage du dernier appel réussi par provider, pour respecter intervalle_s
_dernier_appel = {p["nom"]: 0.0 for p in PROVIDERS}

# index du provider actuellement utilisé — avance définitivement au suivant
# dès qu'un provider est à sec, pour ne pas re-tester un provider épuisé
# à chaque site (perte de temps).
_provider_idx = 0

PROGRESS_FILE = "progress.json"
OUTPUT_FILE   = "extraction_results.json"
MAX_CHARS     = 30000

# pages les plus susceptibles de contenir email/gérant — traitées en priorité
SLUGS_PRIORITAIRES = [
    "contact", "contactez", "mentions", "equipe", "notre-equipe",
    "notre-agence", "agence", "a-propos", "qui-sommes", "apropos",
    "responsable", "direction", "directeur", "gerant",
    "agents", "agent", "collaborateurs", "membres", "cabinet",
]

def slug_priorite(nom_fichier: str) -> int:
    """Retourne un score de priorité (plus bas = plus important)."""
    nom = nom_fichier.lower()
    for i, slug in enumerate(SLUGS_PRIORITAIRES):
        if slug in nom:
            return i
    return len(SLUGS_PRIORITAIRES)  # page non prioritaire → à la fin

SYSTEM_PROMPT = """Tu es un extracteur de données pour des agences immobilières françaises.
À partir du texte d'un site web, extrais ces informations en JSON uniquement, sans texte autour :
{
  "nom_agence": "nom commercial de l'agence ou vide",
  "email": "adresse email NOMINATIVE ou vide",
  "nom_gerant": "nom d'une personne responsable ou vide",
  "nb_annonces": "nombre d'annonces/biens (chiffre uniquement) ou vide",
  "taille_equipe": "nombre de personnes dans l'équipe (chiffre uniquement) ou vide",
  "crm_detecte": "logiciel CRM détecté (Apimo, Netty, etc.) ou vide"
}

Règle "nom_agence" :
- Cherche le nom commercial dans le titre de page, l'en-tête, le logo (texte alt), le pied de page,
  ou les mentions légales (raison sociale). Prends la forme la plus courte et lisible (pas la raison
  sociale juridique complète type "SARL Dupont Immobilier au capital de...").

Règle "nom_gerant" — IMPORTANT, ne pas se limiter au mot "gérant" :
- Cherche une personne associée à un rôle de responsabilité : gérant, directeur, directrice,
  fondateur, fondatrice, responsable, associé, associée, agent principal, négociateur en charge,
  ou toute personne présentée en premier / mise en avant sur une page équipe.
- Si la page équipe liste plusieurs noms SANS titre explicite, et qu'il n'y a qu'1 à 3 personnes
  dans l'équipe (agence solo/duo), prends le premier nom listé — c'est presque toujours le gérant
  dans une petite structure.
- Si un email nominatif a été trouvé, vérifie si un nom de la page correspond à cet email
  (ex: email "j.martin@..." → cherche "Julien Martin" dans le texte) et utilise ce nom.
- Ne laisse "nom_gerant" vide QUE si aucun nom de personne n'apparaît nulle part dans le texte fourni.

Règle stricte sur "email" :
- Ne renvoie QUE une adresse nominative, du type prenom.nom@domaine, p.nom@domaine, prenom@domaine.
- Si l'adresse trouvée est générique (contact@, info@, contactez@, agence@, hello@, bonjour@, accueil@, direction@ sans nom associé, etc.), laisse "email": "".
- Si plusieurs adresses nominatives existent, choisis celle qui correspond à "nom_gerant" si possible, sinon la première.

Avant de renvoyer une valeur vide pour "nom_gerant" ou "email", relis une seconde fois le texte fourni :
ces informations sont souvent présentes mais dispersées (ex: nom en haut de la page équipe, email
en bas de page ou dans une fiche contact séparée). Ne devine jamais une valeur qui n'est pas dans le texte."""

GENERIQUES = {
    "contact", "contactez", "contactez-nous", "info", "infos", "agence",
    "hello", "bonjour", "accueil", "direction", "administration", "admin",
    "secretariat", "commercial", "location", "vente", "ventes", "gestion",
    "immo", "immobilier", "reception",
}

def est_nominatif(email: str) -> bool:
    if not email or "@" not in email:
        return False
    local = email.split("@")[0].lower().strip()
    if local in GENERIQUES:
        return False
    # une adresse nominative contient typiquement un séparateur (. _ -) ou un prénom+nom collés
    return bool(local) and local not in GENERIQUES

# ─── PROGRESS ─────────────────────────────────────────────────────────────────

def charger_progress():
    """Retourne (zips_traites, sites_traites) — sites_traites permet de
    reprendre EN PLEIN MILIEU d'un zip sans retraiter (et dupliquer) les
    sites déjà analysés avant un arrêt pour quota épuisé."""
    if os.path.exists(PROGRESS_FILE):
        with open(PROGRESS_FILE, encoding="utf-8") as f:
            data = json.load(f)
            return set(data.get("traites", [])), set(data.get("sites_traites", []))
    return set(), set()

def sauver_progress(traites: set, sites_traites: set):
    with open(PROGRESS_FILE, "w", encoding="utf-8") as f:
        json.dump({
            "traites": list(traites),
            "sites_traites": list(sites_traites),
        }, f, ensure_ascii=False)

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

import re

def _extraire_json(raw: str) -> str:
    """Nettoie la sortie du modèle avant parsing : retire le raisonnement
    éventuel (<think>...</think>) et les fences markdown ```json ... ```."""
    texte = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
    texte = re.sub(r"^```(?:json)?\s*|\s*```$", "", texte, flags=re.MULTILINE).strip()
    if not texte.startswith("{"):
        debut, fin = texte.find("{"), texte.rfind("}")
        if debut != -1 and fin != -1:
            texte = texte[debut:fin + 1]
    return texte

def _appel_provider(provider: dict, texte: str) -> tuple[str | None, int | None, str | None]:
    """Un seul appel à un provider donné.
    Retourne (contenu_brut, status_code, erreur_reseau)."""
    attente = provider["intervalle_s"] - (time.time() - _dernier_appel[provider["nom"]])
    if attente > 0:
        time.sleep(attente)
    _dernier_appel[provider["nom"]] = time.time()

    try:
        resp = requests.post(provider["base_url"], headers={
            "Authorization": f"Bearer {provider['api_key']}",
            "Content-Type": "application/json",
        }, json={
            "model": provider["model"],
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user",   "content": f"Voici le texte du site :\n\n{texte[:MAX_CHARS]}"},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0,
            "max_tokens": 2000,
            "reasoning_effort": provider.get("reasoning_effort", "low"),
            **provider.get("extra_params", {}),
        }, timeout=60)
    except Exception as e:
        return None, None, str(e)

    if resp.status_code != 200:
        return None, resp.status_code, resp.text[:200]

    try:
        message = resp.json()["choices"][0]["message"]
    except Exception as e:
        return None, resp.status_code, f"réponse inattendue : {e}"

    contenu = (message.get("content") or "").strip()
    return contenu, resp.status_code, None

def tous_epuises() -> bool:
    """True si on a avancé au-delà du dernier provider disponible —
    plus aucun quota gratuit à utiliser aujourd'hui."""
    return _provider_idx >= len(PROVIDERS)

def analyser(texte: str, max_tentatives: int = 3) -> dict:
    global _provider_idx
    vide = {"nom_agence": "", "email": "", "nom_gerant": "", "nb_annonces": "", "taille_equipe": "", "crm_detecte": ""}

    while _provider_idx < len(PROVIDERS):
        provider = PROVIDERS[_provider_idx]

        if not provider["api_key"]:
            print(f"\n   [{provider['nom']}] pas de clé API configurée, on passe au suivant")
            _provider_idx += 1
            continue

        for tentative in range(1, max_tentatives + 1):
            raw, status, err = _appel_provider(provider, texte)

            # quota épuisé / accès refusé → provider définitivement grillé, on avance
            if status in (429, 402, 403):
                print(f"\n   ⚠️  [{provider['nom']}] {status} — bascule vers le provider suivant")
                _provider_idx += 1
                break

            # erreur réseau ou HTTP transitoire → on retente ce même provider
            if raw is None:
                if tentative < max_tentatives:
                    time.sleep(3 * tentative)
                    continue
                print(f"\n   ⚠️  [{provider['nom']}] échec après {max_tentatives} tentatives : {err}")
                _provider_idx += 1
                break

            if not raw:
                if tentative < max_tentatives:
                    time.sleep(3 * tentative)
                    continue
                return {**vide, "_erreur": f"[{provider['nom']}] réponse vide après plusieurs tentatives"}

            nettoye = _extraire_json(raw)
            try:
                return {**vide, **json.loads(nettoye)}
            except json.JSONDecodeError:
                if tentative < max_tentatives:
                    time.sleep(3 * tentative)
                    continue
                print(f"\n   [debug JSON invalide — {provider['nom']}] brut={raw[:300]!r}")
                return {**vide, "_erreur": f"[{provider['nom']}] JSON invalide après {max_tentatives} tentatives"}
        else:
            # la boucle for s'est terminée sans break → sortie normale, rien à faire ici
            pass

    return {**vide, "_erreur": "tous les providers sont épuisés ou en échec"}

# ─── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    debut_global = time.time()
    traites, sites_traites = charger_progress()
    resultats = charger_resultats()

    zips_locaux    = sorted(glob.glob("*.zip"))
    zips_a_traiter = [z for z in zips_locaux if os.path.basename(z) not in traites]

    print("=" * 60)
    print(f"🚀 DÉMARRAGE EXTRACTION")
    print(f"   Zips locaux     : {len(zips_locaux)}")
    print(f"   Déjà traités    : {len(traites)}")
    print(f"   À traiter       : {len(zips_a_traiter)}")
    providers_dispo = [p["nom"] for p in PROVIDERS if p["api_key"]]
    print(f"   Providers dispo : {', '.join(providers_dispo) or 'AUCUN — vérifie les secrets GitHub'}")
    print(f"   Résultats existants : {len(resultats)}")
    print("=" * 60)

    if not zips_a_traiter:
        print("✅ Rien à traiter — tous les zips ont été traités.")
        return

    nb_sites      = 0
    nb_erreurs    = 0
    nb_emails     = 0
    nb_gerants    = 0
    stop_quota    = False   # True dès que tous les providers sont à sec

    for i, zip_path in enumerate(zips_a_traiter, 1):
        if stop_quota:
            break

        nom_zip    = os.path.basename(zip_path)
        debut_zip  = time.time()
        print(f"\n{'─'*60}")
        print(f"📦 [{i}/{len(zips_a_traiter)}] {nom_zip}")

        # grouper les pages HTML prioritaires par site
        sites: dict[str, list[tuple[int, str]]] = {}
        try:
            with zipfile.ZipFile(zip_path, "r") as z:
                membres = z.infolist()
                nb_lus = 0
                for info in membres:
                    if info.filename.endswith(".html") and not info.filename.startswith("__MACOSX"):
                        priorite = slug_priorite(info.filename)
                        if priorite == len(SLUGS_PRIORITAIRES):
                            continue  # page non prioritaire → ignorée
                        parties = info.filename.split("/")
                        site_id = parties[0] if len(parties) > 1 else "racine"
                        html    = z.read(info.filename).decode("utf-8", errors="ignore")
                        texte   = clean_html(html)
                        sites.setdefault(site_id, []).append((priorite, texte))
                    nb_lus += 1
                    if nb_lus % 200 == 0:
                        print(f"   … lecture en cours : {nb_lus}/{len(membres)} fichiers", flush=True)
            print(f"   → {len(sites)} sites détectés dans ce zip (pages prioritaires uniquement)")
        except Exception as e:
            print(f"   ❌ Erreur lecture zip : {e}")
            continue

        zip_ok = zip_err = 0
        zip_complet = True   # passe à False si on doit stopper en plein milieu

        for site_id, pages in sites.items():
            cle_site = f"{nom_zip}::{site_id}"
            if cle_site in sites_traites:
                continue  # déjà traité lors d'un run précédent (repris après arrêt quota)

            # ── Vérif AVANT l'appel : si plus aucun provider dispo, on arrête
            # immédiatement (pas de nouvel appel API, pas d'erreur inutile) ──
            if tous_epuises():
                print(f"\n   ⏸️  Quota épuisé sur tous les providers — arrêt immédiat.")
                print(f"      Reprise automatique dès que le quota journalier reset")
                print(f"      (le prochain run planifié retombera pile sur ce zip).")
                zip_complet = False
                stop_quota  = True
                break

            nb_sites += 1
            debut_site = time.time()
            print(f"   [{nb_sites}] {site_id[:45]}", end=" ... ", flush=True)

            texte_complet = " ".join(t for _, t in sorted(pages, key=lambda p: p[0]))
            infos = analyser(texte_complet)
            duree = time.time() - debut_site

            resultats.append({
                "zip":           nom_zip,
                "site":          site_id,
                "nom_agence":    infos.get("nom_agence", ""),
                "email":         infos.get("email", ""),
                "nom_gerant":    infos.get("nom_gerant", ""),
                "nb_annonces":   infos.get("nb_annonces", ""),
                "taille_equipe": infos.get("taille_equipe", ""),
                "crm_detecte":   infos.get("crm_detecte", ""),
            })
            sites_traites.add(cle_site)

            if infos.get("_erreur"):
                print(f"⚠️  {infos['_erreur']} ({duree:.1f}s)")
                nb_erreurs += 1
                zip_err    += 1
                # si l'erreur vient d'un épuisement total juste détecté par analyser(),
                # on s'arrête aussi net à la prochaine itération (tous_epuises() le verra)
            else:
                agence = infos.get("nom_agence") or "-"
                email  = infos.get("email")    or "-"
                gerant = infos.get("nom_gerant") or "-"
                print(f"✅ agence={agence}  email={email}  gérant={gerant}  ({duree:.1f}s)")
                zip_ok += 1
                if infos.get("email"):    nb_emails  += 1
                if infos.get("nom_gerant"): nb_gerants += 1

            # sauvegarde incrémentale : si le process est coupé (timeout, kill),
            # on ne reperd jamais plus qu'un seul site
            sauver_progress(traites, sites_traites)
            sauver_resultats(resultats)

        duree_zip = time.time() - debut_zip

        if zip_complet:
            print(f"   ✔ Zip terminé en {duree_zip:.0f}s — {zip_ok} OK | {zip_err} erreurs")
            traites.add(nom_zip)
            # le zip est fini : plus besoin de garder le détail par site pour lui
            sites_traites = {c for c in sites_traites if not c.startswith(f"{nom_zip}::")}
            sauver_progress(traites, sites_traites)
            sauver_resultats(resultats)
            os.remove(zip_path)
        else:
            print(f"   ⏸️  Zip interrompu après {duree_zip:.0f}s — {zip_ok} OK | {zip_err} erreurs "
                  f"({len(sites) - zip_ok - zip_err} sites restants, repris au prochain run)")
            # zip PAS marqué comme traité, PAS supprimé → repris tel quel plus tard

    duree_totale = time.time() - debut_global
    print(f"\n{'='*60}")
    if stop_quota:
        print(f"⏸️  EXTRACTION SUSPENDUE (quota épuisé) — reprise au prochain cycle planifié")
    else:
        print(f"✅ EXTRACTION TERMINÉE")
    print(f"   Durée totale    : {duree_totale/60:.1f} min")
    print(f"   Sites traités   : {nb_sites}")
    print(f"   Emails trouvés  : {nb_emails} ({nb_emails/nb_sites*100:.0f}%)" if nb_sites else "   Sites traités   : 0")
    print(f"   Gérants trouvés : {nb_gerants} ({nb_gerants/nb_sites*100:.0f}%)" if nb_sites else "")
    print(f"   Erreurs API     : {nb_erreurs}")
    print(f"   Total résultats : {len(resultats)}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
        
