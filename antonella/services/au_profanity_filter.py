"""Multi-language PG-13 profanity and safety filter for Anime Universe City.

Normalizes Unicode (`NFKC`), strips combining diacritics, decodes common leetspeak
substitutions, collapses stretched repeats, and checks both spaced and punctuation-elided
spans across EN, PT-BR, ES, JP (romaji + kana), FR, and DE.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Dict, List, Set, Tuple

_LEET_MAP = str.maketrans({
    "0": "o",
    "1": "i",
    "!": "i",
    "3": "e",
    "4": "a",
    "@": "a",
    "5": "s",
    "$": "s",
    "7": "t",
    "+": "t",
    "8": "b",
    "9": "g",
})

# Severe hate speech / threats / CSAM indicators -> hard block message
_HARD_BLOCK_TERMS: Dict[str, Tuple[str, ...]] = {
    "en": (
        "kys",
        "kill yourself",
        "nigger",
        "nigga",
        "faggot",
        "retard",
        "chink",
        "spic",
        "kike",
        "wetback",
        "tranny",
        "rape",
        "molest",
    ),
    "pt": (
        "se mata",
        "vai morrer",
        "estupro",
        "estuprar",
        "macaco",
        "viadinho",
    ),
    "es": (
        "matate",
        "violar",
        "maricon",
        "sudaca",
    ),
    "de": (
        "kanake",
        "schwuchtel",
        "vergewaltigen",
    ),
    "fr": (
        "negre",
        "bougnoule",
        "violer",
    ),
    "jp": (
        "shine",
        "korosu",
        "しね",
        "ころす",
        "死ね",
        "殺す",
    ),
}

# Censorable PG-13 profanity per language -> masked cleanly with `***`
_PROFANITY_TERMS: Dict[str, Tuple[str, ...]] = {
    "en": (
        "fuck",
        "fucking",
        "fucker",
        "motherfucker",
        "shit",
        "bullshit",
        "bitch",
        "asshole",
        "bastard",
        "dickhead",
        "cock",
        "pussy",
        "cunt",
        "whore",
        "slut",
        "wanker",
        "twat",
        "porn",
        "hentai",
    ),
    "pt": (
        "porra",
        "caralho",
        "puta",
        "putaria",
        "foda",
        "foder",
        "fudido",
        "fodase",
        "merda",
        "bosta",
        "buceta",
        "boceta",
        "arrombado",
        "cuzao",
        "filho da puta",
        "desgraca",
        "corno",
        "vagabunda",
        "pika",
        "rola",
    ),
    "es": (
        "mierda",
        "joder",
        "coño",
        "cono",
        "cabron",
        "pendejo",
        "chingar",
        "chingada",
        "pinche",
        "culero",
        "verga",
        "gilipollas",
        "zorra",
        "marica",
        "hijo de puta",
    ),
    "jp": (
        "kuso",
        "kusoyaro",
        "kusottare",
        "chikusho",
        "teme",
        "fuzakeru",
        "yariman",
        "oppai",
        "manko",
        "chinko",
        "chinchin",
        "ketsunoana",
        "くそ",
        "クソ",
        "ちくしょう",
        "まんこ",
        "ちんこ",
    ),
    "fr": (
        "merde",
        "putain",
        "bordel",
        "salope",
        "connard",
        "connasse",
        "encule",
        "nique",
        "niquer",
        "foutre",
        "bite",
        "couille",
        "batard",
    ),
    "de": (
        "scheisse",
        "scheiße",
        "fick",
        "ficken",
        "fotze",
        "hurensohn",
        "schlampe",
        "wichser",
        "arschloch",
        "miststuck",
        "drecksau",
    ),
}


def _strip_accents(s: str) -> str:
    nfkd = unicodedata.normalize("NFKD", s)
    return "".join(ch for ch in nfkd if not unicodedata.combining(ch))


def _normalize_for_match(text: str) -> Tuple[str, str]:
    """Return `(token_stream, compact_alnum)` after NFKC + accent + leet + stretch normalization."""
    s = unicodedata.normalize("NFKC", text or "").lower()
    s = _strip_accents(s)
    # Translate leetspeak only where digits/symbols act inside words
    leet = s.translate(_LEET_MAP)
    # Collapse 3+ repeated characters (`fuuuuck` -> `fuuck`, `ssshhhiiit` -> `sshiit`) and 2+ for compact check
    collapsed = re.sub(r"(.)\1{2,}", r"\1\1", leet)
    spaced = re.sub(r"[^a-z0-9\u3040-\u30ff\u4e00-\u9faf\s]+", " ", collapsed)
    spaced = re.sub(r"\s+", " ", spaced).strip()
    compact = re.sub(r"[^a-z0-9\u3040-\u30ff\u4e00-\u9faf]+", "", re.sub(r"(.)\1+", r"\1", leet))
    return spaced, compact


def _contains_term(spaced: str, compact: str, term: str) -> bool:
    t_norm = _strip_accents(unicodedata.normalize("NFKC", term)).lower().strip()
    is_cjk = bool(re.search(r"[\u3040-\u30ff\u4e00-\u9faf]", t_norm))
    if is_cjk:
        return t_norm in spaced or t_norm in compact
    if " " in t_norm:
        phrase_re = r"\b" + re.escape(t_norm) + r"\b"
        t_compact = re.sub(r"(.)\1+", r"\1", t_norm.replace(" ", ""))
        return bool(re.search(phrase_re, spaced)) or (len(t_compact) >= 5 and t_compact in compact)

    # Word-boundary check on spaced stream (allow common simple plural/suffix variants)
    if re.search(rf"\b{re.escape(t_norm)}(?:s|es|ed|ing|er)?\b", spaced):
        return True
    # Separated-letter evasion check (`f.u.c.k`, `f u c k`) via compact stream for >=4 char words
    t_collapsed = re.sub(r"(.)\1+", r"\1", t_norm)
    if len(t_collapsed) >= 4 and len(t_norm) >= 4:
        # Check space-split single-letter evasion separately (`f u c k` -> joined singletons)
        singleton_joined = re.sub(r"(?<=\b[a-z])\s+(?=[a-z]\b)", "", spaced)
        if re.search(rf"\b{re.escape(t_norm)}(?:s|es|ing|er)?\b", singleton_joined):
            return True
        # Direct substring on compact only if compact word isn't a harmless substring when long
        if len(t_collapsed) >= 5 and t_collapsed in compact:
            return True
    return False


def _mask_visible_terms(text: str, terms_to_mask: List[str]) -> str:
    """Replace matched profanity spans cleanly with `***` while preserving surrounding punctuation."""
    out = text
    for term in terms_to_mask:
        t = term.strip()
        if not t:
            continue
        if re.search(r"[\u3040-\u30ff\u4e00-\u9faf]", t):
            out = out.replace(t, "***")
            continue
        # Match obfuscated / repeated / leetspeak characters matching each letter of `t`
        char_map = {
            "a": "[a4@áàãâä]",
            "b": "[b8]",
            "e": "[e3éèêë]",
            "i": "[i1!íìîï]",
            "o": "[o0óòõôö]",
            "s": "[s5$]",
            "t": "[t7+]",
            "u": "[uúùûü]",
            "c": "[cç]",
            "n": "[nñ]",
        }
        parts = []
        for ch in _strip_accents(t.lower()):
            if ch == " ":
                parts.append(r"[\s._*-]+")
            else:
                cls = char_map.get(ch, re.escape(ch))
                parts.append(f"{cls}+")
        pattern = r"(?i)\b" + r"[\s._*-]*".join(parts) + r"(?:s|es|ed|ing|er)?\b"
        out = re.sub(pattern, "***", out)
    return out


def filter_pg13_text(text: str, allow_empty: bool = False) -> Dict[str, object]:
    """Evaluate user-submitted text across EN/PT/ES/JP/FR/DE for PG-13 compliance.

    Returns `{clean_text, flagged, blocked, matched_languages, reason}`.
    """
    raw = str(text or "")
    if not raw.strip():
        return {
            "clean_text": "" if allow_empty else "",
            "flagged": False,
            "blocked": False,
            "matched_languages": [],
            "reason": "",
        }

    spaced, compact = _normalize_for_match(raw)

    blocked_langs: Set[str] = set()
    for lang, terms in _HARD_BLOCK_TERMS.items():
        for term in terms:
            if _contains_term(spaced, compact, term):
                blocked_langs.add(lang)
                break

    if blocked_langs:
        return {
            "clean_text": "",
            "flagged": True,
            "blocked": True,
            "matched_languages": sorted(blocked_langs),
            "reason": "Message blocked by PG-13 safety policy.",
        }

    matched_langs: Set[str] = set()
    matched_terms: List[str] = []
    for lang, terms in _PROFANITY_TERMS.items():
        for term in terms:
            if _contains_term(spaced, compact, term):
                matched_langs.add(lang)
                matched_terms.append(term)

    if not matched_terms:
        return {
            "clean_text": raw,
            "flagged": False,
            "blocked": False,
            "matched_languages": [],
            "reason": "",
        }

    clean = _mask_visible_terms(raw, matched_terms)
    # Fallback safeguard if regex replacement missed a sneaky leet/accented variant
    spaced_after, compact_after = _normalize_for_match(clean)
    still_dirty = any(_contains_term(spaced_after, compact_after, t) for t in matched_terms)
    if still_dirty:
        clean = "[filtered for PG-13 community guidelines]"

    return {
        "clean_text": clean,
        "flagged": True,
        "blocked": False,
        "matched_languages": sorted(matched_langs),
        "reason": "Profanity masked to keep Anime Universe City PG-13.",
    }
