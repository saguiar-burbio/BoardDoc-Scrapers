# ─────────────────────────────────────────────────────────────────────────────
# src/analyzer.py
# ─────────────────────────────────────────────────────────────────────────────

import os
import json
import time
import re
import logging
from typing import Dict, Any

from dotenv import load_dotenv
from google import genai
# Alias to prevent namespace conflict with Python's built-in 'types' module
from google.genai import types

# Relative configurations import
from config.settings import MODEL_NAME

# Setup named logger for consistent output
LOGGER = logging.getLogger("simbli_minutes")

# In-module initialization of the Gemini client to prevent circular imports
load_dotenv()
gemini_key = os.getenv("GEMINI_KEY")
client = genai.Client(api_key=gemini_key)
LOGGER.info("Gemini client successfully initialized inside analyzer.py")

def analyze_pdf_with_gemini(
    pdf_path: str, 
    prompt: str, 
    model_name: str = "gemini-2.0-flash-lite"
) -> Dict[str, Any]:
    """Sends a PDF to Gemini for analysis and parses the JSON response."""
    LOGGER.info(f"  Analysing PDF with Gemini ({model_name})...")
    try:
        with open(pdf_path, "rb") as f:
            pdf_bytes = f.read()
        pdf_part = types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf")
        response = client.models.generate_content(model=model_name, contents=[prompt, pdf_part])
    except Exception as e:
        err_str = str(e)
        LOGGER.error(f"  ❌ Gemini API Call Failed: {e}")
        if "400" in err_str or "INVALID_ARGUMENT" in err_str:
            return {"result": {"error": "INVALID_ARGUMENT"}, "tokens": {}}
        return {"result": {"error": "API_CALL_FAILED"}, "tokens": {}}

    usage = getattr(response, "usage_metadata", None)
    token_info = {
        "input_tokens": getattr(usage, "prompt_token_count", None),
        "output_tokens": getattr(usage, "candidates_token_count", None),
        "total_tokens": getattr(usage, "total_token_count", None),
    }

    try:
        raw = response.text.strip()
        data = _extract_json(raw)
    except (json.JSONDecodeError, AttributeError) as e:
        raw_text = getattr(response, "text", "No response text found")
        LOGGER.warning(f"  ⚠️ Could not parse JSON from Gemini response. Error: {e}")
        data = {"raw_text": raw_text}

    return {"result": data, "tokens": token_info}


def analyze_pdf_with_gemini_with_retry(
    pdf_path: str,
    prompt: str,
    model_name: str = MODEL_NAME,
    max_retries: int = 2,
    base_backoff: float = 2.0,
) -> Dict[str, Any]:
    """Calls analyze_pdf_with_gemini with exponential backoff on failure."""
    last_response: Dict[str, Any] = {}
 
    for attempt in range(1, max_retries + 2):
        if attempt > 1:
            wait_secs = base_backoff ** (attempt - 1)
            LOGGER.warning(f"  ⏳ Gemini retry {attempt - 1}/{max_retries} — waiting {wait_secs:.1f}s...")
            time.sleep(wait_secs)
 
        LOGGER.info(f"  🔁 Gemini call attempt {attempt}/{max_retries + 1}: {pdf_path}")
        last_response = analyze_pdf_with_gemini(pdf_path, prompt, model_name)

        error = last_response.get("result", {}).get("error")
        if error == "INVALID_ARGUMENT":
            LOGGER.error(f"  ❌ Gemini 400 INVALID_ARGUMENT — not retrying: {pdf_path}")
            return last_response
        if error != "API_CALL_FAILED":
            return last_response

    LOGGER.error(f"  ❌ Gemini failed after {max_retries + 1} attempts for: {pdf_path}")
    return last_response

def _extract_json(raw: str) -> Any:
    """Robustly extracts JSON from a string that may or may not be wrapped in markdown code fences."""
    match = re.search(r"```(?:json)?\s*\n?(.*?)```", raw, re.DOTALL)
    if match:
        raw = match.group(1).strip()
    json_match = re.search(r"(\{.*\}|\[.*\])", raw, re.DOTALL)
    if json_match:
        raw = json_match.group(1).strip()
    return json.loads(raw)

