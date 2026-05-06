from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from api.services.config import FEEDBACK_JSONL_PATH


def append_feedback(record: dict) -> None:
    """Append feedback."""
    path = Path(FEEDBACK_JSONL_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = {
        **record,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(line, ensure_ascii=False, default=str) + "\n")
