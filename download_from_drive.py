import os
import json
import requests

# Configuration OAuth
CLIENT_ID = os.getenv("GOOGLE_OAUTH_CLIENT_ID")
CLIENT_SECRET = os.getenv("GOOGLE_OAUTH_CLIENT_SECRET")
REFRESH_TOKEN = os.getenv("GOOGLE_OAUTH_REFRESH_TOKEN")

def get_access_token():
    url = "https://oauth2.googleapis.com/token"
    data = {
        'client_id': CLIENT_ID,
        'client_secret': CLIENT_SECRET,
        'refresh_token': REFRESH_TOKEN,
        'grant_type': 'refresh_token'
    }
    response = requests.post(url, data=data)
    return response.json().get('access_token')

def download_zip_files():
    access_token = get_access_token()
    if not access_token:
        print("Erreur : Impossible d'obtenir l'access token Google.")
        return

    headers = {"Authorization": f"Bearer {access_token}"}
    
    # Lister les fichiers ZIP sur le Drive
    # On cherche les fichiers .zip qui ne sont pas dans la corbeille
    query = "mimeType='application/zip' and trashed=false"
    url = f"https://www.googleapis.com/drive/v3/files?q={query}"
    
    response = requests.get(url, headers=headers)
    files = response.json().get('files', [])
    
    if not files:
        print("Aucun fichier ZIP trouvé sur Google Drive.")
        return

    for file in files:
        file_id = file['id']
        file_name = file['name']
        print(f"Téléchargement de {file_name}...")
        
        download_url = f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media"
        file_response = requests.get(download_url, headers=headers)
        
        with open(file_name, 'wb') as f:
            f.write(file_response.content)
        print(f"  {file_name} téléchargé avec succès.")

if __name__ == "__main__":
    download_zip_files()
