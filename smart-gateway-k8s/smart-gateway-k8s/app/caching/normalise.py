"""Prompt normalisation and cache-key hashing."""
import hashlib
import json
import re
import unicodedata

_WS = re.compile(r"\s+")


def normalise(prompt: str) -> str:
    """Make trivially different prompts identical: Unicode form, case, whitespace, trailing punctuation."""
    text = unicodedata.normalize("NFKC", prompt).lower()
    text = _WS.sub(" ", text).strip()
    return text.rstrip(" ?!.")


def request_hash(prompt: str, params: dict | None = None, normalise_prompt: bool = True) -> str:
    """Hash the prompt together with any parameters that change the answer (system prompt, temperature...)."""
    text = normalise(prompt) if normalise_prompt else prompt
    payload = json.dumps({"p": text, "params": params or {}}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()