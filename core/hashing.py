# ─────────────────────────────────────────────────────────────────────────────
# src/hashing.py
# ─────────────────────────────────────────────────────────────────────────────

import re
import json
import hashlib
import logging
from typing import Optional

import numpy as np
from datasketch import MinHash

# Configuration imports
from config.settings import MINHASH_NUM_PERM, MINHASH_SHINGLE_SIZE

# Reference the central system named logger
LOGGER = logging.getLogger("simbli_minutes")


# ═════════════════════════════════════════════════════════════════════════════
# 1. BINARY DUPLICATION CHECKING (SHA-256 SYSTEM)
# ═════════════════════════════════════════════════════════════════════════════

def compute_sha256_from_file(filepath: str) -> str:
    """
    Computes the SHA-256 cryptographic signature of a file's raw bytes.
    Runs in bounded memory by reading the target file in chunks.

    Args:
        filepath: Complete system location path to the target document.

    Returns:
        A 64-character hexadecimal representation of the file's hash.
    """
    LOGGER.debug(f"  Computing SHA-256 hash for: {filepath}")
    sha256 = hashlib.sha256()
    try:
        # Read in 64KB blocks to prevent memory crashes on extremely large PDFs
        with open(filepath, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                sha256.update(chunk)
        digest = sha256.hexdigest()
        LOGGER.debug(f"  SHA-256 computed successfully = {digest}")
        return digest
    except OSError as e:
        LOGGER.error(f"  SHA-256 calculation failed for file '{filepath}': {e}")
        raise


# ═════════════════════════════════════════════════════════════════════════════
# 2. LOCALITY-SENSITIVE HASHING (MINHASH & TEXT JACCARD SIMILARITY)
# ═════════════════════════════════════════════════════════════════════════════

def build_minhash(
    text: str,
    num_perm: int = MINHASH_NUM_PERM,
    shingle_size: int = MINHASH_SHINGLE_SIZE
) -> MinHash:
    """
    Generates a datasketch MinHash object of a given text block by tokenizing
    it into character-level n-gram shingles. This allows soft deduplication.

    Args:
        text: The raw, unstructured text string extracted via OCR or digital parse.
        num_perm: The number of random permutation hash functions (higher = better accuracy).
        shingle_size: The character width of each overlapping shingle n-gram.

    Returns:
        An initialized datasketch MinHash object containing the text's fingerprint.
    """
    mh = MinHash(num_perm=num_perm)
    cleaned = re.sub(r"\s+", " ", text.lower()).strip()
    
    if not cleaned:
        LOGGER.warning("  build_minhash: clean text is empty — returning blank MinHash structure.")
        return mh
        
    # Segment text into overlapping token strings (shingles)
    shingles = {cleaned[i:i + shingle_size] for i in range(len(cleaned) - shingle_size + 1)}
    LOGGER.debug(f"  MinHash: extracted {len(shingles)} unique shingles from {len(cleaned)} characters.")
    
    for shingle in shingles:
        mh.update(shingle.encode("utf-8"))
        
    return mh


def serialize_minhash(mh: MinHash) -> str:
    """
    Serializes a datasketch MinHash object's internal state array into a compact,
    database-friendly JSON string.

    Args:
        mh: An active MinHash object instance.

    Returns:
        A serialized JSON array representing the internal hashvalues.
    """
    return json.dumps(mh.hashvalues.tolist())


def deserialize_minhash(serialized: str, num_perm: int = MINHASH_NUM_PERM) -> MinHash:
    """
    Reconstructs an active datasketch MinHash object from its serialized JSON format
    without having to re-shingle the original text payload.

    Args:
        serialized: The JSON array string containing the saved hashvalues.
        num_perm: The target permutation width, must match the initial build state.

    Returns:
        A fully re-hydrated MinHash object ready for Jaccard similarity evaluation.
    """
    mh = MinHash(num_perm=num_perm)
    mh.hashvalues = np.array(json.loads(serialized), dtype=np.uint64)
    return mh


def compute_jaccard(mh_a: MinHash, mh_b: MinHash) -> float:
    """
    Calculates the Jaccard similarity coefficient between two MinHash structures.
    This provides a close mathematical estimation of text document intersection.

    Args:
        mh_a: Fingerprint representation of Document A.
        mh_b: Fingerprint representation of Document B.

    Returns:
        A floating point score between 0.0 (no overlap) and 1.0 (highly identical).
    """
    return mh_a.jaccard(mh_b)