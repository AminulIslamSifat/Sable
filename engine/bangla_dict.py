'''Offline English -> Bangla dictionary index.

Backed by engine/data/bangla_en.tsv.gz, a merge of the open
MinhasKamal/BengaliDictionary datasets (~106k unique English headwords). The
data file is committed to the repo so a fresh clone needs no download step.
Kept out of the request path at import time: the file is read once, lazily,
on the first lookup, then held as a plain dict for O(1) hits afterwards.

Regenerate the data file with scripts/build_bangla_dict.py.
'''
from __future__ import annotations

import gzip
import logging
import threading
from pathlib import Path

logger = logging.getLogger('sable.study.dict.offline')

_DATA_FILE = Path(__file__).resolve().parent / 'data' / 'bangla_en.tsv.gz'

_INDEX: dict[str, list[str]] | None = None
_LOCK = threading.Lock()
_FAILED = False


def _load() -> dict[str, list[str]]:
    '''Read the TSV into memory. Missing file is not fatal - returns {} so the
    online sources stay the only path.'''
    global _FAILED
    if _FAILED or not _DATA_FILE.exists():
        if not _DATA_FILE.exists():
            logger.info('offline bangla dictionary not found at %s', _DATA_FILE)
        _FAILED = True
        return {}
    index: dict[str, list[str]] = {}
    try:
        with gzip.open(_DATA_FILE, 'rt', encoding='utf-8') as fh:
            for line in fh:
                line = line.rstrip('\n')
                if not line:
                    continue
                key, _, joined = line.partition('\t')
                if not key or not joined:
                    continue
                index[key] = joined.split('|')
    except Exception as exc:
        logger.warning('offline bangla dictionary load failed: %s', exc)
        _FAILED = True
        return {}
    logger.info('offline bangla dictionary ready: %d headwords', len(index))
    return index


def _index() -> dict[str, list[str]]:
    global _INDEX
    if _INDEX is None:
        with _LOCK:
            if _INDEX is None:
                _INDEX = _load()
    return _INDEX


def lookup(word: str) -> list[str]:
    'Bangla meanings for an English headword, or [] when absent.'
    term = (word or '').strip().lower()
    if not term:
        return []
    return _index().get(term, [])


def available() -> bool:
    'True once the data file has loaded with at least one headword.'
    return bool(_index())


def headword_count() -> int:
    return len(_index())
