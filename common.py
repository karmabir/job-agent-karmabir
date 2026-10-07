import json
import os
import re
import sqlite3
import threading
from pathlib import Path

import yaml
from anthropic import Anthropic
from dotenv import load_dotenv

ROOT = Path(__file__).parent
DB_PATH = ROOT / "data" / "jobs.db"
load_dotenv(ROOT / ".env")

FAST_MODEL = "claude-haiku-4-5-20251001"
SONNET = "claude-sonnet-5"
WRITE_MODEL = FAST_MODEL if os.environ.get("TAILOR_CHEAP") == "1" else SONNET
# Approximate USD per million tokens (input, output); check console pricing.
PRICES = {FAST_MODEL: (1.0, 5.0), SONNET: (3.0, 15.0)}

_client = None
_usage = {"cost": 0.0, "in": 0, "out": 0}
_lock = threading.Lock()


def client() -> Anthropic:
    global _client
    if _client is None:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise SystemExit("ANTHROPIC_API_KEY missing; put it in job-agent/.env")
        _client = Anthropic()
    return _client


def ask_json(model: str, system: str, user: str, max_tokens: int = 2000, retries: int = 2) -> dict:
    for attempt in range(retries + 1):
        try:
            return _ask_json(model, system, user, max_tokens)
        except (ValueError, json.JSONDecodeError):
            if attempt == retries:
                raise


def _ask_json(model: str, system: str, user: str, max_tokens: int) -> dict:
    extra = {"thinking": {"type": "disabled"}} if model == SONNET else {}
    resp = client().messages.create(
        model=model, max_tokens=max_tokens, system=system,
        messages=[{"role": "user", "content": user}], **extra,
    )
    pin, pout = PRICES[model]
    with _lock:
        _usage["in"] += resp.usage.input_tokens
        _usage["out"] += resp.usage.output_tokens
        _usage["cost"] += resp.usage.input_tokens * pin / 1e6 + resp.usage.output_tokens * pout / 1e6
    text = "".join(b.text for b in resp.content if b.type == "text")
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError(f"no JSON in response: {text[:200]}")
    return json.loads(m.group(0))


def usage_report() -> str:
    return f"tokens in={_usage['in']} out={_usage['out']} est. cost=${_usage['cost']:.3f}"


def load_profile() -> dict:
    return yaml.safe_load((ROOT / "profile.yaml").read_text())


def profile_text() -> str:
    return (ROOT / "profile.yaml").read_text()


def db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(exist_ok=True)
    return sqlite3.connect(DB_PATH)
