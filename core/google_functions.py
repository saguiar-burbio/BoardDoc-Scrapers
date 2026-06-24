# ─────────────────────────────────────────────────────────────────────────────
# core/google_functions.py
# ─────────────────────────────────────────────────────────────────────────────

"""
Holds Google Drive and Sheets API utility functions, including:
- log_doc_info: Logs processing status/metadata directly to a run log sheet.
- upload_file_to_folder: Uploads a localized PDF out to a designated Google Drive directory.
- move_file: Shifts documents between target storage folders.
- list_files_in_folder: Non-recursive file directory mapper.
- create_folder / create_spreadsheet: Runtime workspace initializers.
- download_file_from_drive: Downloads cloud assets to a localized temporary buffer.
- read_from_sheets: Pulls coordinate inputs out of active Google Sheet pipelines.
"""

import io
import logging
import os
import tempfile
from datetime import datetime
from typing import Any, Dict, List, Optional

import pandas as pd
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload

from core.google_auth import get_authenticated_services

LOGGER = logging.getLogger("simbli_minutes")


# ═════════════════════════════════════════════════════════════════════════════
# 1. SHEETS TELEMETRY WRITERS & READERS
# ═════════════════════════════════════════════════════════════════════════════

def log_doc_info(
    sheets_service,
    spreadsheet_id: str,
    sheet_name: str,
    nces: str,
    district: str,
    title: str,
    status: str,
    message: str,
    url: str = '',
    file_url: str = '',
    meeting_date: any = '',
    document_date: any = '',
    hash: str = '',
    file_name: str = '',
    term: str = 'Minutes',
    paragraph_text: str = '',
    attachment_text: str = '',
    attachment_url: str = '',
    page_count: str = ''
) -> None:
    """Logs document metadata or parsing status to the target run tracking Google Sheet."""
    LOGGER.debug("Attempting to log metadata row to Google Sheets...")
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    if not meeting_date:
        formatted_meeting_date = 'NA'
    else:
        formatted_meeting_date = (
            meeting_date.strftime("%m-%d-%y") if isinstance(meeting_date, datetime) else str(meeting_date)
        )

    if not document_date:
        formatted_document_date = 'NA'
    else:
        formatted_document_date = (
            document_date.strftime("%m-%d-%y") if isinstance(document_date, datetime) else str(document_date)
        )

    values = [[
        now,
        str(nces),
        district,
        title,
        term,
        status,
        paragraph_text,
        message,
        formatted_meeting_date,
        formatted_document_date,
        url,
        file_url,
        hash,
        file_name,
        attachment_text,
        attachment_url,
        page_count
    ]]

    try:
        sheets_service.spreadsheets().values().append(
            spreadsheetId=spreadsheet_id,
            range=f"{sheet_name}!A1",
            valueInputOption="USER_ENTERED",
            body={"values": values}
        ).execute()
        LOGGER.info(f"Successfully logged target row to sheet: {title} | {status}")
    except Exception as e:
        LOGGER.error(f"Failed to append logging metadata row to Google Sheet: {e}")


def read_from_sheets(
    sheet_url: str,
    sheet_name: str,
    sheets_service,
    headers_of_interest: list = None
) -> pd.DataFrame:
    """Reads from a Google Sheet worksheet tab and returns a pandas DataFrame."""
    try:
        sheet_id = sheet_url.split("/d/")[1].split("/")[0]

        result = sheets_service.spreadsheets().values().get(
            spreadsheetId=sheet_id,
            range=sheet_name
        ).execute()

        values = result.get('values', [])
        if not values:
            return pd.DataFrame()

        headers = values[0]
        data_rows = values[1:]
        padded_rows = [row + [''] * (len(headers) - len(row)) for row in data_rows]
        df = pd.DataFrame(padded_rows, columns=headers)

        if headers_of_interest:
            filtered_cols = [col for col in headers_of_interest if col in df.columns]
            df = df[filtered_cols]

        return df
    except Exception as e:
        LOGGER.error(f"Failed to parse source spreadsheet data: {e}")
        return pd.DataFrame()


# ═════════════════════════════════════════════════════════════════════════════
# 2. DRIVE OBJECT INGRESS & STORAGE MANAGEMENT
# ═════════════════════════════════════════════════════════════════════════════

def upload_file_to_folder(drive_service, folder_id: str, file_path: str, file_name: str) -> Optional[str]:
    """Uploads a local file to a target parent directory in Google Drive."""
    try:
        file_metadata = {
            'name': file_name,
            'parents': [folder_id]
        }
        media = MediaFileUpload(file_path, resumable=True)
        file = drive_service.files().create(
            body=file_metadata,
            media_body=media,
            fields='id'
        ).execute()
        LOGGER.info(f"Uploaded '{file_name}' with cloud ID: {file['id']}")
        return file['id']
    except Exception as e:
        LOGGER.error(f"Error uploading {file_path} to folder {folder_id}: {e}")
        return None


def download_file_from_drive(file_id: str, filename: str, drive_service) -> Optional[str]:
    """Downloads a Google Drive file to the local temp directory."""
    try:
        request = drive_service.files().get_media(fileId=file_id)
        fh = io.BytesIO()
        downloader = MediaIoBaseDownload(fh, request)

        done = False
        while not done:
            status, done = downloader.next_chunk()
            if status:
                LOGGER.debug(f"Cloud Download progress: {int(status.progress() * 100)}%")

        temp_path = os.path.join(tempfile.gettempdir(), filename)
        with open(temp_path, "wb") as f:
            f.write(fh.getvalue())

        LOGGER.info(f"Downloaded Drive asset ({file_id}) to: {temp_path}")
        return temp_path
    except Exception as e:
        LOGGER.error(f"Failed to fetch cloud file {file_id}: {e}")
        return None


def move_file(drive_service, file_id: str, new_folder_id: str) -> None:
    """Updates the parent folder of a file in Google Drive."""
    try:
        file = drive_service.files().get(fileId=file_id, fields='parents').execute()
        previous_parents = ",".join(file.get('parents', []))
        drive_service.files().update(
            fileId=file_id,
            addParents=new_folder_id,
            removeParents=previous_parents,
            fields='id, parents'
        ).execute()
        LOGGER.info(f"File {file_id} moved to folder: {new_folder_id}")
    except Exception as e:
        LOGGER.error(f"Failed to move file ({file_id}) to folder ({new_folder_id}): {e}")


def list_files_in_folder(drive_service, folder_id: str) -> list:
    """Returns files in a specific Drive folder (non-recursive)."""
    files = []
    page_token = None
    try:
        while True:
            response = drive_service.files().list(
                q=f"'{folder_id}' in parents and trashed = false",
                spaces='drive',
                fields='nextPageToken, files(id, name)',
                pageToken=page_token
            ).execute()

            files.extend(response.get('files', []))
            page_token = response.get('nextPageToken')
            if not page_token:
                break
        return files
    except Exception as e:
        LOGGER.error(f"Failed to scan folder ({folder_id}): {e}")
        return []


# ═════════════════════════════════════════════════════════════════════════════
# 3. WORKSPACE INITIALIZATION & MANAGEMENT
# ═════════════════════════════════════════════════════════════════════════════

def create_folder(drive_service, name: str, parent_id: str) -> Optional[str]:
    """Creates a new directory inside a parent Google Drive container."""
    try:
        file_metadata = {
            'name': name,
            'mimeType': 'application/vnd.google-apps.folder',
            'parents': [parent_id]
        }
        folder = drive_service.files().create(
            body=file_metadata,
            fields='id'
        ).execute()
        LOGGER.info(f"Folder '{name}' created with ID: {folder['id']}")
        return folder['id']
    except Exception as e:
        LOGGER.error(f"Failed to create folder: {e}")
        return None


def create_spreadsheet(sheets_service, drive_service, title: str, parent_id: str) -> Optional[str]:
    """Creates a Google Sheets spreadsheet inside a designated Google Drive folder."""
    try:
        spreadsheet = {'properties': {'title': title}}
        sheet = sheets_service.spreadsheets().create(
            body=spreadsheet,
            fields='spreadsheetId'
        ).execute()
        sheet_id = sheet['spreadsheetId']

        drive_service.files().update(
            fileId=sheet_id,
            addParents=parent_id,
            removeParents='root',
            fields='id, parents'
        ).execute()

        LOGGER.info(f"Spreadsheet '{title}' created with ID: {sheet_id}")
        return sheet_id
    except Exception as e:
        LOGGER.error(f"Failed to create spreadsheet: {e}")
        return None


def rename_file(drive_service, file_id: str, new_name: str) -> None:
    """Renames an existing file in Google Drive."""
    try:
        drive_service.files().update(
            fileId=file_id,
            body={"name": new_name}
        ).execute()
        LOGGER.info(f"Drive asset ({file_id}) renamed to: '{new_name}'")
    except Exception as e:
        LOGGER.error(f"Error renaming file {file_id}: {e}")
