# ─────────────────────────────────────────────────────────────────────────────
# core/gcs.py
# Google Cloud Storage bootstrap helpers shared by all crawler entry points.
# ─────────────────────────────────────────────────────────────────────────────

import logging
import os
import tempfile

from google.cloud import storage

LOGGER = logging.getLogger("simbli_minutes")


def download_blob_to_tmp(bucket_name: str, blob_name: str) -> str:
    """Download a GCS object to a temp file and return its local path."""
    LOGGER.debug(f"Downloading gs://{bucket_name}/{blob_name} → /tmp/...")
    gcs_client = storage.Client()
    bucket = gcs_client.bucket(bucket_name)
    blob   = bucket.blob(blob_name)
    fd, tmp_path = tempfile.mkstemp(suffix=os.path.splitext(blob_name)[1])
    os.close(fd)
    blob.download_to_filename(tmp_path)
    LOGGER.debug(f"Downloaded to {tmp_path}")
    return tmp_path


def upload_file_to_gcs(bucket_name: str, destination_blob: str, source_file: str) -> str:
    """Upload a local file to a GCS bucket and return its gs:// URI."""
    LOGGER.debug(f"Uploading '{source_file}' → gs://{bucket_name}/{destination_blob}")
    gcs_client = storage.Client()
    bucket = gcs_client.bucket(bucket_name)
    blob   = bucket.blob(destination_blob)
    blob.upload_from_filename(source_file)
    uri = f"gs://{bucket_name}/{destination_blob}"
    LOGGER.info(f"✅ GCS upload complete: {uri}")
    return uri
