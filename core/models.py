# ─────────────────────────────────────────────────────────────────────────────
# core/models.py
# Shared dataclasses used by all crawler pipelines.
# ─────────────────────────────────────────────────────────────────────────────

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class AttachmentRecord:
    """Tracks the outcome of a single attachment download attempt."""
    index:           int
    name:            str            = ""
    pdf_url:         str            = ""
    downloaded:      str            = "—"
    dupe_check_type: str            = "—"
    passed_dupe:     Optional[bool] = None
    file_name:       str            = ""


@dataclass
class MeetingRecord:
    """Tracks aggregate outcomes for a single board meeting."""
    date:         str
    meeting_type: str
    keyword:      str
    total:        int = 0
    downloaded:   int = 0
    dupes:        int = 0
    errors:       int = 0
    attachments:  List[AttachmentRecord] = field(default_factory=list)
