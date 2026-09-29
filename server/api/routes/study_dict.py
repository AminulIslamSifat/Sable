'''Study-mode dictionary - pure lookup, no AI.

Two keyless sources:
  English  freedictionaryapi.com (Wiktionary-derived JSON), falling back to
           the en.wiktionary.org REST definition endpoint.
  Bangla   en.wiktionary.org wikitext translation tables ({{t|bn|...}}).

Every lookup is cached on disk under LIBRARY_DICT_DIR, so re-selecting the
same word costs zero network. A result is cached whenever at least one source
answered - including a genuine "no entry" - so a total network failure is
never cached and the next attempt retries.
'''
from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path

import httpx
from fastapi import APIRouter, HTTPException, Query

from engine import bangla_dict
from engine.config import LIBRARY_DICT_DIR

router = APIRouter()
logger = logging.getLogger('sable.study.dict')

_FDAPI = 'https://freedictionaryapi.com/api/v1/entries/en/{word}'
_WIKTIONARY_REST = 'https://en.wiktionary.org/api/rest_v1/page/definition/{word}'
_WIKTIONARY_API = 'https://en.wiktionary.org/w/api.php'
_UA = 'SableStudy/2.0 (dictionary lookup)'
_TIMEOUT = 8.0
_MAX_ENTRIES = 5
_MAX_DEFS = 6
_MAX_BN = 8
_MAX_LEMMA_TRIES = 3
# Bump when the lookup pipeline changes so stale entries are refetched.
# v3 retired entries written before transient-failure caching was fixed.
# v4 added the offline Bangla index as the primary Bangla source.
_CACHE_VERSION = 4

_WORD_RE = re.compile(r"[A-Za-z][A-Za-z'-]*")
_BN_RE = re.compile(r'[{][{]t[+]?[^}]*?[|]bn[|]([^}|]+)')
_TAG_RE = re.compile(r'<[^>]+>')


def _clean(text: str) -> str:
    'Strip wiki HTML and collapse runs of whitespace.'
    return re.sub(r'\s+', ' ', _TAG_RE.sub('', text or '')).strip()


def _normalize(raw: str) -> str:
    'First word of a possibly multi-word selection, lowercased and bare.'
    match = _WORD_RE.search(raw or '')
    return (match.group(0) if match else '').strip().lower()


def _cache_path(word: str) -> Path:
    return LIBRARY_DICT_DIR / (word + '.json')


def _read_cache(word: str) -> dict | None:
    path = _cache_path(word)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    # A version bump retires entries written by an older lookup pipeline, so
    # a coverage fix takes effect without the user clearing the cache by hand.
    if data.get('v') != _CACHE_VERSION:
        return None
    return data


def _write_cache(word: str, payload: dict) -> None:
    LIBRARY_DICT_DIR.mkdir(parents=True, exist_ok=True)
    target = _cache_path(word)
    tmp = target.with_suffix('.json.tmp')
    record = dict(payload)
    record['v'] = _CACHE_VERSION
    tmp.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(target)


def _first_ipa(entry: dict) -> str:
    for pron in (entry.get('pronunciations') or []):
        if isinstance(pron, dict) and pron.get('type') == 'ipa' and pron.get('text'):
            return str(pron['text']).strip()
    return ''


def _flatten_sense(sense: dict, depth: int = 0) -> list:
    'One fdapi sense -> definition dicts, recursing one level into subsenses.'
    out = []
    text = _clean(str(sense.get('definition') or ''))
    if text:
        examples = sense.get('examples') or []
        example = _clean(str(examples[0])) if examples else ''
        out.append({'text': text, 'example': example})
    if depth < 1:
        for sub in (sense.get('subsenses') or []):
            if isinstance(sub, dict):
                out.extend(_flatten_sense(sub, depth + 1))
    return out


def _shape_fdapi(data: dict) -> tuple:
    'freedictionaryapi.com JSON -> (entries, phonetic).'
    entries = []
    phonetic = ''
    for entry in (data.get('entries') or []):
        if not isinstance(entry, dict):
            continue
        if not phonetic:
            phonetic = _first_ipa(entry)
        defs = []
        for sense in (entry.get('senses') or []):
            if isinstance(sense, dict):
                defs.extend(_flatten_sense(sense))
                if len(defs) >= _MAX_DEFS:
                    break
        if defs:
            entries.append({
                'pos': str(entry.get('partOfSpeech') or '').strip(),
                'definitions': defs[:_MAX_DEFS],
            })
        if len(entries) >= _MAX_ENTRIES:
            break
    return entries, phonetic


async def _english_from_wiktionary(client: httpx.AsyncClient, word: str) -> tuple:
    'Fallback English source: the en.wiktionary REST definition endpoint.'
    try:
        res = await client.get(
            _WIKTIONARY_REST.format(word=word),
            headers={'User-Agent': _UA},
        )
        if res.status_code != 200:
            return [], ''
        data = res.json()
    except Exception as exc:
        logger.debug('wiktionary definition failed for %s: %s', word, exc)
        return [], ''
    entries = []
    for block in (data.get('en') or []):
        if not isinstance(block, dict):
            continue
        defs = []
        for item in (block.get('definitions') or []):
            if not isinstance(item, dict):
                continue
            text = _clean(str(item.get('definition') or ''))
            if not text:
                continue
            parsed = item.get('parsedExamples') or []
            example = ''
            if parsed and isinstance(parsed[0], dict):
                example = _clean(str(parsed[0].get('example') or ''))
            defs.append({'text': text, 'example': example})
            if len(defs) >= _MAX_DEFS:
                break
        if defs:
            entries.append({
                'pos': str(block.get('partOfSpeech') or '').strip(),
                'definitions': defs,
            })
        if len(entries) >= _MAX_ENTRIES:
            break
    return entries, ''


async def _fetch_english(client: httpx.AsyncClient, word: str) -> tuple:
    'Try the primary English source, then the Wiktionary fallback.'
    try:
        res = await client.get(_FDAPI.format(word=word))
        if res.status_code == 200:
            entries, phonetic = _shape_fdapi(res.json())
            if entries:
                return entries, phonetic, 'freedictionaryapi.com'
    except Exception as exc:
        logger.debug('fdapi failed for %s: %s', word, exc)
    entries, phonetic = await _english_from_wiktionary(client, word)
    return entries, phonetic, 'en.wiktionary.org' if entries else ''


def _lemma_candidates(word: str) -> list:
    '''Crude singular / stem forms to try when an inflected page has no table.

    Wiktionary files translations under the lemma, so "processes" carries no
    {{t|bn|...}} while "process" does. These rewrites cover the common English
    plural and verb-ending patterns; a wrong guess simply costs one request
    that yields nothing.
    '''
    out = []

    def add(candidate: str) -> None:
        if len(candidate) >= 2 and candidate != word and candidate not in out:
            out.append(candidate)

    if word.endswith('ies') and len(word) > 4:
        add(word[:-3] + 'y')                      # studies -> study
    if word.endswith(('ses', 'xes', 'zes', 'ches', 'shes')) and len(word) > 4:
        add(word[:-2])                            # processes -> process
    # Skip -ss so glass/bus/class are left alone.
    if word.endswith('s') and not word.endswith('ss') and len(word) > 3:
        add(word[:-1])                            # grammars -> grammar
    if word.endswith('ing') and len(word) > 5:
        add(word[:-3])                            # running -> runn
        add(word[:-3] + 'e')                      # writing -> write
    if word.endswith('ed') and len(word) > 4:
        add(word[:-2])                            # walked -> walk
        add(word[:-1])                            # studied -> studie
    return out[:_MAX_LEMMA_TRIES]


async def _bangla_page(client: httpx.AsyncClient, page: str) -> list | None:
    '''Extract the {{t|bn|...}} values from one Wiktionary page.

    Returns [] for a page that genuinely has no Bangla table, but None when the
    request itself failed. The caller must not treat those the same: an empty
    list is a fact worth caching, a failed request is not.
    '''
    try:
        res = await client.get(
            _WIKTIONARY_API,
            params={
                'action': 'parse', 'page': page, 'prop': 'wikitext',
                'format': 'json', 'formatversion': '2',
            },
            headers={'User-Agent': _UA},
        )
        if res.status_code != 200:
            logger.debug('wiktionary %s -> HTTP %s', page, res.status_code)
            return None
        wikitext = (((res.json() or {}).get('parse') or {}).get('wikitext')) or ''
    except Exception as exc:
        logger.debug('wiktionary bangla failed for %s: %s', page, exc)
        return None
    seen = set()
    out = []
    for raw in _BN_RE.findall(wikitext):
        value = _clean(raw)
        if value and value not in seen:
            seen.add(value)
            out.append(value)
        if len(out) >= _MAX_BN:
            break
    return out


async def _fetch_bangla(client: httpx.AsyncClient, word: str) -> tuple:
    '''Bangla meanings, retrying the lemma when an inflected form has no table.

    Returns (meanings, errored). `errored` is True when a request failed rather
    than the word legitimately lacking a Bangla entry - that distinction is what
    stops a rate-limited blip being cached as a permanent "no translation".
    '''
    # Offline index covers ~106k headwords with no network at all. Try it first
    # so the common case is instant and immune to rate limiting.
    offline = bangla_dict.lookup(word)
    if offline:
        return offline, False

    direct = await _bangla_page(client, word)
    if direct:
        return direct, False

    candidates = list(_lemma_candidates(word))
    # Wiktionary files translations under the lemma too, and the offline index
    # is keyed the same way - so check the lemma forms against it before
    # spending any more network requests.
    for candidate in candidates:
        offline = bangla_dict.lookup(candidate)
        if offline:
            logger.debug('offline bangla lemma hit: %s -> %s', word, offline[0])
            return offline, False

    if not candidates:
        # A failed lookup and a genuine miss are both "no lemma to try", but
        # only the genuine miss is safe to cache.
        return [], direct is None
    # Fire the guesses together and take the first non-empty in priority order,
    # so the slowest single request sets the latency instead of their sum.
    results = await asyncio.gather(
        *(_bangla_page(client, candidate) for candidate in candidates),
        return_exceptions=True,
    )
    for result in results:
        if isinstance(result, list) and result:
            logger.debug('bangla lemma hit: %s -> %s', word, result[0])
            return result, False
    # Nothing hit. It only counts as a real miss if at least one request
    # actually completed and returned a page.
    errored = all(r is None or isinstance(r, BaseException) for r in results)
    return [], errored


@router.get('/api/study/dictionary')
async def study_dictionary(word: str = Query('', max_length=120)) -> dict:
    'Pure dictionary lookup for a selected word - no model, no tokens.'
    term = _normalize(word)
    if not term:
        raise HTTPException(status_code=400, detail='No word to look up')

    cached = _read_cache(term)
    if cached is not None:
        cached.pop('v', None)
        cached['cached'] = True
        return cached

    async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as client:
        (entries, phonetic, source), (bangla, bn_errored) = await asyncio.gather(
            _fetch_english(client, term),
            _fetch_bangla(client, term),
        )

    payload = {
        'word': term,
        'found': bool(entries or bangla),
        'phonetic': phonetic,
        'bangla': bangla,
        'entries': entries,
        'source': source,
        'offline': bangla_dict.available(),
        'cached': False,
    }
    # Never persist a result whose Bangla side failed to load - a network blip
    # would otherwise be frozen on disk as "this word has no translation".
    if not bn_errored and (source or bangla):
        try:
            _write_cache(term, payload)
        except Exception as exc:
            logger.warning('dictionary cache write failed for %s: %s', term, exc)
    return payload
