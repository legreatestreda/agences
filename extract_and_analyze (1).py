import os
import zipfile
import json
import requests
from bs4 import BeautifulSoup
import glob

# Configuration
API_KEY = os.getenv("FIREWORKS_API_KEY")
MODEL = "accounts/fireworks/models/deepseek-v4-flash"
BASE_URL = "https://api.fireworks.ai/inference/v1/chat/completions"

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

def clean_html(html_content):
    """Nettoie le HTML pour ne garder que le texte utile et réduire les tokens."""
    soup = BeautifulSoup(html_content, 'html.parser')
    
    # Supprimer les balises inutiles
    for tag in soup(['script', 'style', 'nav', 'footer', 'header']):
        tag.decompose()
        
    # Récupérer le texte
    text = soup.get_text(separator=' ', strip=True)
    return text

def analyze_text(text):
    """Envoie le texte à Fireworks AI pour extraction."""
    if not API_KEY:
        print("Erreur : FIREWORKS_API_KEY non configurée.")
        return None

    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json"
    }
    
    # On limite le texte pour éviter de dépasser les limites de contexte, tout en gardant le maximum possible
    # Le modèle supporte 1M de context, mais pour la rapidité et le coût, 15k-20k est souvent suffisant pour une page web.
    truncated_text = text[:20000]

    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"Voici le texte du site :\n\n{truncated_text}"}
        ],
        "response_format": {"type": "json_object"}
    }

    try:
        response = requests.post(BASE_URL, headers=headers, json=payload)
        response.raise_for_status()
        return response.json()['choices'][0]['message']['content']
    except Exception as e:
        print(f"Erreur lors de l'appel API : {e}")
        return None

def process_zips(zip_pattern):
    """Parcourt les fichiers ZIP et traite les HTML à l'intérieur."""
    results = []
    
    # On cherche les ZIP dans le dossier courant
    zip_files = glob.glob(zip_pattern)
    if not zip_files:
        print(f"Aucun fichier correspondant à {zip_pattern} trouvé.")
        return results

    for zip_path in zip_files:
        print(f"Traitement de {zip_path}...")
        try:
            with zipfile.ZipFile(zip_path, 'r') as z:
                for file_info in z.infolist():
                    # On ignore les fichiers cachés et on ne prend que le HTML
                    if file_info.filename.endswith('.html') and not file_info.filename.startswith('__MACOSX'):
                        with z.open(file_info) as f:
                            html_content = f.read().decode('utf-8', errors='ignore')
                            cleaned_text = clean_html(html_content)
                            
                            print(f"  Analyse de {file_info.filename}...")
                            analysis_raw = analyze_text(cleaned_text)
                            
                            if analysis_raw:
                                try:
                                    analysis_json = json.loads(analysis_raw)
                                    results.append({
                                        "source_zip": os.path.basename(zip_path),
                                        "source_file": file_info.filename,
                                        "data": analysis_json
                                    })
                                except json.JSONDecodeError:
                                    print(f"  Erreur de parsing JSON pour {file_info.filename}")
        except Exception as e:
            print(f"Erreur lors de l'ouverture du ZIP {zip_path} : {e}")
    
    return results

if __name__ == "__main__":
    # Recherche tous les fichiers ZIP à la racine
    zip_pattern = "*.zip"
    all_results = process_zips(zip_pattern)
    
    # Sauvegarde des résultats au format JSON
    output_file = 'extraction_results.json'
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(all_results, f, ensure_ascii=False, indent=2)
    
    print(f"\nTerminé. {len(all_results)} fichiers analysés.")
    print(f"Les résultats ont été sauvegardés dans : {output_file}")
