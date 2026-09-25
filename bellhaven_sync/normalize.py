"""Normalization for addresses, names and phones so cosmetic differences don't count."""
import re
from difflib import SequenceMatcher

STREET_ABBR = {
    "street": "st", "st.": "st", "avenue": "ave", "av": "ave", "ave.": "ave",
    "road": "rd", "rd.": "rd", "drive": "dr", "dr.": "dr", "lane": "ln", "ln.": "ln",
    "boulevard": "blvd", "blvd.": "blvd", "pike": "pike", "pk": "pike", "pkwy": "parkway",
    "court": "ct", "place": "pl", "circle": "cir", "highway": "hwy", "terrace": "ter",
    "north": "n", "south": "s", "east": "e", "west": "w",
    "northwest": "nw", "northeast": "ne", "southwest": "sw", "southeast": "se",
    "saint": "st",
}

NAME_SYNONYMS = {
    "rehab": "rehabilitation", "&": "and", "centre": "center", "healthcare": "health care",
    "ctr": "center", "snf": "skilled nursing",
}
# Words that carry no identity: operator brand, connectors, generic facility nouns.
NAME_STOPWORDS = {
    "the", "of", "at", "and", "bellhaven", "senior", "living", "care", "center", "health",
    "nursing", "rehabilitation", "community", "communities", "campus", "home",
}


def norm_street(s):
    s = (s or "").lower().replace(",", " ").replace(".", " ")
    toks = [STREET_ABBR.get(t, t) for t in s.split()]
    return " ".join(toks)


def is_po_box(s):
    return bool(re.match(r"\s*p\.?\s*o\.?\s*box\b", s or "", re.I))


def norm_city(s):
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def norm_zip(s):
    return re.sub(r"\D", "", s or "")[:5]


def norm_phone(s):
    d = re.sub(r"\D", "", s or "")
    return d[-10:]


def _name_tokens(s):
    s = (s or "").lower()
    s = re.sub(r"\(.*?\)", " ", s)
    s = s.replace("&", " & ").replace("-", " ")
    toks = []
    for t in re.findall(r"[a-z0-9&]+", s):
        toks.extend(NAME_SYNONYMS.get(t, t).split())
    return toks


def norm_name(s):
    """Full canonical form: two names are 'the same name' if these are equal."""
    return " ".join(t for t in _name_tokens(s) if t not in {"the", "of", "at", "and"})


def core_name_tokens(s):
    """Distinctive tokens only (brand + generic words removed)."""
    return {t for t in _name_tokens(s) if t not in NAME_STOPWORDS}


def name_similarity(a, b):
    """0..1 overlap of distinctive name tokens (typo-tolerant). Falls back to a character
    ratio only when a name has no distinctive tokens at all (e.g. "Bellhaven Senior Living")."""
    ta, tb = core_name_tokens(a), core_name_tokens(b)
    if not ta or not tb:
        return round(SequenceMatcher(None, norm_name(a), norm_name(b)).ratio(), 3)
    hits = sum(1 for t in ta if any(t == u or SequenceMatcher(None, t, u).ratio() >= 0.85 for u in tb))
    return round(hits / (len(ta) + len(tb) - hits), 3)
