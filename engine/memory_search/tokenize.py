"""Tokenization, stopwords, path stripping, and text preprocessing for memory search."""

from __future__ import annotations

import re
from datetime import datetime

_STOPWORDS = frozenset({
    "the","a","an","is","are","was","were","be","been","being",
    "have","has","had","do","does","did","will","would","could","should",
    "may","might","shall","can","need","dare","to","of","in","for","on","with",
    "at","by","from","as","into","through","during","before","after",
    "above","below","between","out","off","over","under","again","then",
    "once","here","there","when","where","why","how","all","both","each",
    "few","more","most","other","some","such","no","nor","not","only",
    "own","same","so","than","too","very","just","because","but","and",
    "or","if","while","about","what","which","who","whom","this","that",
    "these","those","i","me","my","we","our","you","your","he","him",
    "his","she","her","it","its","they","them","their",
    "up","down","also","now","any","get","got","make","take","see",
    "know","want","let","say","go","come","think","give","use","find",
    "tell","ask","work","seem","feel","try","leave","call","keep",
    "look","looks","looking","looked",
    "put","mean","become","show","run","move","like","thing","way",
    "back","still","new","one","two","first","last","long","great",
    "little","old","right","big","high","small","large","next",
    "early","young","important","public","bad","good","well","done",
    # Casual insults / filler — high frequency in user prompts, zero retrieval value
    "fuck","fucking","fucked","shit","shitty","damn","dumbass","idiot","idiotic",
    "moron","stupid","dumb","wtf","hell","crap","piss","ass","bullshit",
    "motherfucker","motherfucking","dumbfuck","dipshit","asshole","bitch",
    "ok","okay","hey","hi","hello","please","thanks","thank","yeah","nah",
    "seriously","literally","actually","basically","honestly",
    # Vague/filler words with zero retrieval signal
    "actual","something","anything","everything","nothing","someone","anyone",
    "everyone","nobody","somebody","anyway","random","vibing","lol","lmao",
    "help","sure","really","maybe","ever","never","always","much","many",
    "lot","bit","kind","sort","stuff","things","regardless","whatever",
    "somehow","somewhere","everywhere","nowhere","huh","wtf","bruh","yo",
})

# Regex to find file paths in queries (tool calls pass raw paths as content)
_PATH_RE = re.compile(r"[/~][\w./\-]+")
# Directory names with trailing slash (e.g. "includes/", "layouts/", "backup/")
_DIR_RE = re.compile(r"\b\w+/\s")
# Standalone file extensions like .css .json .bak that leak from listings
_EXT_RE = re.compile(r"\b[\w\-]+\.(?:css|json|jsonc|js|ts|py|md|txt|html|bak|toml|yaml|yml|conf|cfg|log|sh|rs|go|c|h|svg|xml)\b")
# Generic path components with zero retrieval value
_PATH_NOISE = frozenset({
    "home", "usr", "local", "bin", "lib", "etc", "var", "tmp", "opt",
    "config", "configs", "src", "lib", "include", "includes", "build",
    "dist", "node_modules", "cache", "data", "backup", "bak", "layouts",
    "projects", "project", "hdd", "ssd", "dotfiles",
})


def _strip_paths(text: str) -> str:
    """Replace file paths with their meaningful last component(s).

    /home/sifat/Projects/odysseus → "odysseus"
    /home/sifat/.config/waybar/style.css → "waybar"
    """
    def _path_replacer(m: re.Match) -> str:
        path = m.group(0)
        parts = [p for p in path.strip("/~").split("/") if p and p != "."]
        if not parts:
            return " "
        last = parts[-1]
        if "." in last:
            last = last.rsplit(".", 1)[0]
            parts[-1] = last
        meaningful = [p for p in parts if p.lower() not in _PATH_NOISE and len(p) > 2]
        if meaningful:
            return " " + " ".join(meaningful[-2:]) + " "
        return " "

    text = _PATH_RE.sub(_path_replacer, text)
    text = _DIR_RE.sub(" ", text)
    text = _EXT_RE.sub(" ", text)
    return text


def _tokenize(text: str) -> set[str]:
    """Split text into lowercase alphanumeric tokens, filtering stopwords."""
    tokens = re.findall(r"[a-z0-9]+", text.lower())
    return {t for t in tokens if t not in _STOPWORDS and len(t) > 1}


def _keyword_score(query_tokens: set[str], entry_tokens: set[str]) -> float:
    """Jaccard-like overlap between query and entry token sets."""
    if not query_tokens:
        return 0.0
    return len(query_tokens & entry_tokens) / len(query_tokens)


def _is_expired(entry: dict) -> bool:
    """Check if an ephemeral entry has passed its expires_at timestamp."""
    expires = str(entry.get("expires_at") or "").strip()
    if not expires:
        return False
    try:
        dt = datetime.fromisoformat(expires)
        if dt.tzinfo is not None:
            dt = dt.replace(tzinfo=None)
        return dt < datetime.now()
    except ValueError:
        return False
#
