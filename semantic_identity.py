"""Narrow, shared identity extractors for election and standings contracts."""
from __future__ import annotations

import re
import unicodedata

_RELATIVE_STANDINGS_RE = re.compile(r"\b(?:finish(?:es|ed|ing)?\s+)?ahead of\b", re.I)
_CHAMPION_OUTCOME_RE = re.compile(
    r"\b(?:win(?:s|ning)?\s+(?:the\s+)?(?:[\w' -]{0,45}\s+)?championship|"
    r"(?:drivers?|league)\s+champion|championship\s+winner)\b", re.I,
)


def relative_standings_scope(text: str) -> bool:
    """Whether a contract explicitly compares one competitor to another."""
    return bool(_RELATIVE_STANDINGS_RE.search(text))


def championship_winner_scope(text: str) -> bool:
    """Whether a contract explicitly settles on winning a championship."""
    return bool(_CHAMPION_OUTCOME_RE.search(text))


_BRAZILIAN_GOVERNOR_REGIONS = (
    "acre", "alagoas", "amapa", "amazonas", "bahia", "ceara", "distrito federal",
    "espirito santo", "goias", "maranhao", "mato grosso", "minas gerais", "para",
    "paraiba", "parana", "pernambuco", "piaui", "rio de janeiro", "rio grande do norte",
    "rio grande do sul", "rondonia", "roraima", "santa catarina", "sao paulo", "sergipe",
    "tocantins",
)


def brazilian_governor_region(text: str) -> str | None:
    """Return a Brazilian subdivision only in explicit governor-election text."""
    low = text.casefold()
    if not re.search(r"\b(?:governor|gubernatorial|governorship)\b", low):
        return None
    folded = (low.replace("á", "a").replace("â", "a").replace("ã", "a")
              .replace("à", "a").replace("ç", "c").replace("é", "e")
              .replace("í", "i").replace("ó", "o").replace("ô", "o").replace("ú", "u"))
    for region in _BRAZILIAN_GOVERNOR_REGIONS:
        if re.search(rf"\b{re.escape(region)}\b", folded):
            return region
    return None


_FEATURED_WORK_RE = re.compile(
    r"\b(?:be\s+)?(?:featured|feature|features)\s+on\s+(?:the\s+)?"
    r"(.+?)(?:\?|$|\bbefore\b|\bafter\b|\bby\b)",
    re.I,
)


def _fold_identity(text: str) -> str:
    folded = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    toks = re.findall(r"[a-z0-9]+", folded.casefold())
    toks = [tok for tok in toks if tok not in {"the", "a", "an"}]
    return " ".join(toks)


def featured_work_identity(text: str) -> str | None:
    """Return the explicit work/project named after "featured on"."""
    match = _FEATURED_WORK_RE.search(text)
    if not match:
        return None
    identity = _fold_identity(match.group(1))
    if not identity or identity in {"album", "project", "song", "track", "record"}:
        return None
    return identity
