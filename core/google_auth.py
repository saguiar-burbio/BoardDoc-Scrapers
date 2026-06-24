# ─────────────────────────────────────────────────────────────────────────────
# src/google_auth.py
# ─────────────────────────────────────────────────────────────────────────────

import os
import pickle
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# Define required scopes for Drive storage and Sheet logging
SCOPES = [
    'https://www.googleapis.com/auth/drive',
    'https://www.googleapis.com/auth/spreadsheets'
]

def get_authenticated_services(credentials_path: str, token_path: str = 'token.pkl'):
    """
    Authenticates Google Drive and Google Sheets services using a local token cache 
    or running a local OAuth server if credentials need initialization.

    Args:
        credentials_path: Path to your client_secret JSON credential file.
        token_path: Path to the cached credentials pickle file.

    Returns:
        tuple: (drive_service, sheets_service) API client instances.
    """
    creds = None

    # Load existing token from disk if it exists
    if os.path.exists(token_path):
        with open(token_path, 'rb') as token:
            creds = pickle.load(token)

    # Refresh the token if expired or generate a new one via local OAuth server
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(credentials_path, SCOPES)
            creds = flow.run_local_server(port=0)
            
        # Cache the refreshed/new credentials for future runs
        with open(token_path, 'wb') as token:
            pickle.dump(creds, token)

    # Initialize Google API clients
    drive_service = build('drive', 'v3', credentials=creds)
    sheets_service = build('sheets', 'v4', credentials=creds)
    
    return drive_service, sheets_service