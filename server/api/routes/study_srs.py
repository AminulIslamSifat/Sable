'''Study-mode spaced repetition (SRS) - decks, cards, and SM-2 scheduling.

Storage layout (see engine.config):
 ~/sable_output/library/srs/decks.json index of decks
 ~/sable_output/library/srs/cards/<deck>.json cards per deck

A card is anchored to an optional document (doc_id + page) so a review can
jump straight back to the source passage. Scheduling follows a compact SM-2:
each card carries an ease factor, an interval in days, and a due timestamp.
Grades map to SM-2 quality as: again=0, hard=3, good=4, easy=5.

An exclusive flock guards every read-modify-write of a deck card file, and
writes go through a .tmp to replace() dance so a crash never leaves a
half-written deck.
'''
import fcntl
import json
import logging
import re
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

from fastapi import APIRouter, HTTPException

from engine.config import LIBRARY_SRS_DIR

router = APIRouter()
logger = logging.getLogger('sable.study.srs')

_DECKS_VERSION = 1
_MAX_NAME = 120
_MAX_FRONT = 4000
_MAX_BACK = 20000
_MAX_CARDS_PER_DECK = 5000

_GRADES = {'again': 0, 'hard': 3, 'good': 4, 'easy': 5}
_DAY = 86400.0


def _ensure_dirs() -> None:
    (LIBRARY_SRS_DIR / 'cards').mkdir(parents=True, exist_ok=True)


def _index_path() -> Path:
    return LIBRARY_SRS_DIR / 'decks.json'


def _cards_path(deck_id: str) -> Path:
    return LIBRARY_SRS_DIR / 'cards' / (deck_id + '.json')


def _lock_path(deck_id: str) -> Path:
    return LIBRARY_SRS_DIR / 'cards' / (deck_id + '.lock')


def _valid_id(value: str) -> bool:
    'Deck/card ids are uuid4 hex - reject traversal attempts before disk.'
    return bool(re.fullmatch(r'[0-9a-f]{32}', value or ''))


@contextmanager
def _deck_lock(deck_id: str) -> Iterator[None]:
    _ensure_dirs()
    fh = open(_lock_path(deck_id), 'w')
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        finally:
            fh.close()


def _empty_index() -> dict:
    return {'version': _DECKS_VERSION, 'decks': []}


def _read_index() -> dict:
    path = _index_path()
    if not path.exists():
        return _empty_index()
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return _empty_index()
    if not isinstance(data, dict) or not isinstance(data.get('decks'), list):
        return _empty_index()
    data.setdefault('version', _DECKS_VERSION)
    return data


def _write_index(data: dict) -> None:
    _ensure_dirs()
    data['version'] = _DECKS_VERSION
    target = _index_path()
    tmp = target.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(target)


def _find_deck(index: dict, deck_id: str):
    for d in index.get('decks', []):
        if d.get('id') == deck_id:
            return d
    return None


def _read_cards(deck_id: str) -> list:
    path = _cards_path(deck_id)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return []
    if not isinstance(data, dict) or not isinstance(data.get('cards'), list):
        return []
    return data['cards']


def _write_cards(deck_id: str, cards: list) -> None:
    _ensure_dirs()
    target = _cards_path(deck_id)
    tmp = target.with_suffix('.json.tmp')
    payload = {'version': _DECKS_VERSION, 'cards': cards}
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
    tmp.replace(target)


def _update_cards(deck_id: str, mutator: Callable) -> Any:
    'Atomic read-modify-write of a deck cards under an exclusive lock.'
    with _deck_lock(deck_id):
        cards = _read_cards(deck_id)
        result = mutator(cards)
        _write_cards(deck_id, cards)
        return result


def _new_card(front: str, back: str, doc_id: str = '', page: int = 0) -> dict:
    now = time.time()
    return {
        'id': uuid.uuid4().hex,
        'front': front,
        'back': back,
        'doc_id': doc_id,
        'page': page,
        'ease': 2.5,
        'interval': 0.0,
        'reps': 0,
        'lapses': 0,
        'due': now,
        'created': now,
        'reviewed': 0.0,
    }


def _schedule(card: dict, grade: str) -> dict:
    'Apply one SM-2 review to card in place; returns the card.'
    quality = _GRADES.get(grade, 4)
    ease = float(card.get('ease') or 2.5)
    interval = float(card.get('interval') or 0.0)
    reps = int(card.get('reps') or 0)
    if quality < 3:
        reps = 0
        interval = 0.0
        card['lapses'] = int(card.get('lapses') or 0) + 1
        card['due'] = time.time() + 600
    else:
        reps += 1
        if reps == 1:
            interval = 1.0 if quality < 5 else 3.0
        elif reps == 2:
            interval = 3.0 if quality < 5 else 6.0
        else:
            factor = {3: 1.2, 4: ease, 5: ease * 1.15}.get(quality, ease)
            interval = max(1.0, interval * factor)
        card['due'] = time.time() + interval * _DAY
    ease += {0: -0.2, 3: -0.15, 4: 0.0, 5: 0.1}.get(quality, 0.0)
    card['ease'] = min(2.8, max(1.3, round(ease, 3)))
    card['interval'] = round(interval, 3)
    card['reps'] = reps
    card['reviewed'] = time.time()
    return card


def _deck_view(deck: dict, cards: list, now: float) -> dict:
    'Deck record plus live counts.'
    out = dict(deck)
    out['total'] = len(cards)
    out['due'] = sum(1 for c in cards if float(c.get('due') or 0) <= now)
    out['new'] = sum(1 for c in cards if int(c.get('reps') or 0) == 0)
    return out


@router.get('/api/study/srs/decks')
def srs_decks() -> list:
    'Every deck with its live counts (total / due / new).'
    index = _read_index()
    now = time.time()
    out = []
    for deck in index.get('decks', []):
        cards = _read_cards(deck['id'])
        out.append(_deck_view(deck, cards, now))
    out.sort(key=lambda d: -float(d.get('updated') or 0))
    return out


@router.post('/api/study/srs/decks')
def srs_create_deck(payload: dict) -> dict:
    'Create a deck. Body: name, doc_id, description.'
    name = str(payload.get('name') or '').strip()[:_MAX_NAME]
    if not name:
        raise HTTPException(status_code=400, detail='name is required')
    now = time.time()
    deck = {
        'id': uuid.uuid4().hex,
        'name': name,
        'description': str(payload.get('description') or '')[:500],
        'doc_id': str(payload.get('doc_id') or '')[:64],
        'created': now,
        'updated': now,
    }
    with _deck_lock(deck['id']):
        index = _read_index()
        index['decks'].append(deck)
        _write_index(index)
        _write_cards(deck['id'], [])
    return _deck_view(deck, [], now)


@router.patch('/api/study/srs/decks/{deck_id}')
def srs_update_deck(deck_id: str, payload: dict) -> dict:
    if not _valid_id(deck_id):
        raise HTTPException(status_code=400, detail='invalid deck id')
    with _deck_lock(deck_id):
        index = _read_index()
        deck = _find_deck(index, deck_id)
        if not deck:
            raise HTTPException(status_code=404, detail='deck not found')
        if 'name' in payload:
            new_name = str(payload.get('name') or '').strip()[:_MAX_NAME]
            deck['name'] = new_name or deck['name']
        if 'description' in payload:
            deck['description'] = str(payload.get('description') or '')[:500]
        deck['updated'] = time.time()
        _write_index(index)
    return deck


@router.delete('/api/study/srs/decks/{deck_id}')
def srs_delete_deck(deck_id: str) -> dict:
    if not _valid_id(deck_id):
        raise HTTPException(status_code=400, detail='invalid deck id')
    with _deck_lock(deck_id):
        index = _read_index()
        deck = _find_deck(index, deck_id)
        if not deck:
            raise HTTPException(status_code=404, detail='deck not found')
        kept = [d for d in index['decks'] if d.get('id') != deck_id]
        index['decks'] = kept
        _write_index(index)
        try:
            _cards_path(deck_id).unlink(missing_ok=True)
        except Exception:
            pass
    return {'deleted': True, 'id': deck_id}


@router.get('/api/study/srs/decks/{deck_id}/cards')
def srs_deck_cards(deck_id: str, due_only: bool = False) -> list:
    'Cards in a deck. due_only=true returns only what is ready to review.'
    if not _valid_id(deck_id):
        raise HTTPException(status_code=400, detail='invalid deck id')
    cards = _read_cards(deck_id)
    if due_only:
        now = time.time()
        cards = [c for c in cards if float(c.get('due') or 0) <= now]
    cards.sort(key=lambda c: (float(c.get('due') or 0), float(c.get('created') or 0)))
    return cards


@router.post('/api/study/srs/decks/{deck_id}/cards')
def srs_add_card(deck_id: str, payload: dict) -> dict:
    'Add one card. Body: front, back, doc_id, page.'
    if not _valid_id(deck_id):
        raise HTTPException(status_code=400, detail='invalid deck id')
    front = str(payload.get('front') or '').strip()[:_MAX_FRONT]
    back = str(payload.get('back') or '').strip()[:_MAX_BACK]
    if not front or not back:
        raise HTTPException(status_code=400, detail='front and back are required')
    try:
        page = int(payload.get('page') or 0)
    except (TypeError, ValueError):
        page = 0
    card = _new_card(front, back, str(payload.get('doc_id') or '')[:64], max(0, page))

    def add(cards: list) -> dict:
        if len(cards) >= _MAX_CARDS_PER_DECK:
            raise HTTPException(status_code=400, detail='deck is full')
        cards.append(card)
        return card

    return _update_cards(deck_id, add)


@router.post('/api/study/srs/decks/{deck_id}/cards/bulk')
def srs_add_cards_bulk(deck_id: str, payload: dict) -> dict:
    'Add many cards at once.'
    if not _valid_id(deck_id):
        raise HTTPException(status_code=400, detail='invalid deck id')
    raw = payload.get('cards')
    if not isinstance(raw, list) or not raw:
        raise HTTPException(status_code=400, detail='cards array is required')
    new_cards = []
    for item in raw[:_MAX_CARDS_PER_DECK]:
        if not isinstance(item, dict):
            continue
        front = str(item.get('front') or '').strip()[:_MAX_FRONT]
        back = str(item.get('back') or '').strip()[:_MAX_BACK]
        if not front or not back:
            continue
        try:
            page = int(item.get('page') or 0)
        except (TypeError, ValueError):
            page = 0
        doc_id = str(item.get('doc_id') or '')[:64]
        new_cards.append(_new_card(front, back, doc_id, max(0, page)))
    if not new_cards:
        raise HTTPException(status_code=400, detail='no valid cards')

    def add(cards: list) -> int:
        room = _MAX_CARDS_PER_DECK - len(cards)
        cards.extend(new_cards[:room])
        return len(new_cards[:room])

    added = _update_cards(deck_id, add)
    return {'added': added}


@router.patch('/api/study/srs/cards/{deck_id}/{card_id}')
def srs_update_card(deck_id: str, card_id: str, payload: dict) -> dict:
    'Edit a card front/back (does not touch scheduling).'
    if not _valid_id(deck_id) or not _valid_id(card_id):
        raise HTTPException(status_code=400, detail='invalid id')

    def edit(cards: list) -> dict:
        card = next((c for c in cards if c.get('id') == card_id), None)
        if not card:
            raise HTTPException(status_code=404, detail='card not found')
        if 'front' in payload:
            front = str(payload.get('front') or '').strip()[:_MAX_FRONT]
            card['front'] = front or card['front']
        if 'back' in payload:
            back = str(payload.get('back') or '').strip()[:_MAX_BACK]
            card['back'] = back or card['back']
        return card

    return _update_cards(deck_id, edit)


@router.delete('/api/study/srs/cards/{deck_id}/{card_id}')
def srs_delete_card(deck_id: str, card_id: str) -> dict:
    if not _valid_id(deck_id) or not _valid_id(card_id):
        raise HTTPException(status_code=400, detail='invalid id')

    def drop(cards: list) -> dict:
        before = len(cards)
        kept = [c for c in cards if c.get('id') != card_id]
        if len(kept) == before:
            raise HTTPException(status_code=404, detail='card not found')
        cards.clear()
        cards.extend(kept)
        return {'deleted': True, 'id': card_id}

    return _update_cards(deck_id, drop)


@router.post('/api/study/srs/cards/{deck_id}/{card_id}/review')
def srs_review_card(deck_id: str, card_id: str, payload: dict) -> dict:
    'Grade a card.'
    if not _valid_id(deck_id) or not _valid_id(card_id):
        raise HTTPException(status_code=400, detail='invalid id')
    grade = str(payload.get('grade') or 'good').lower()
    if grade not in _GRADES:
        raise HTTPException(status_code=400, detail='invalid grade')

    def review(cards: list) -> dict:
        card = next((c for c in cards if c.get('id') == card_id), None)
        if not card:
            raise HTTPException(status_code=404, detail='card not found')
        return _schedule(card, grade)

    return _update_cards(deck_id, review)


@router.get('/api/study/srs/due')
def srs_due_summary() -> dict:
    'Global due count across every deck.'
    index = _read_index()
    now = time.time()
    total = 0
    per_deck = []
    for deck in index.get('decks', []):
        cards = _read_cards(deck['id'])
        due = sum(1 for c in cards if float(c.get('due') or 0) <= now)
        total += due
        if due:
            per_deck.append({'id': deck['id'], 'name': deck['name'], 'due': due})
    per_deck.sort(key=lambda d: -d['due'])
    return {'due': total, 'decks': per_deck}
