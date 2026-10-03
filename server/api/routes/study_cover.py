'''Study mode - cover-page composer.

Owns one JSON store (~/sable_output/library/cover.json) that remembers every
value the user has typed into the Cover Page tab so the form can offer them
back as dropdowns, plus a PDF renderer for the finished cover.

Storage shape (lists are append-only rosters; singletons overwrite)::

    {
      "version": 1,
      "universities": [{"name","motto","department"}],
      "university":  {"name","motto","department"},      # last used
      "courses": [{"no","title","teachers","last_teacher","last_dates"}],
      "course":  {"no","title"},                         # last used
      "student": {"name","roll","section","session"},
      "teacher_profile": {"designation","dept"},
      "doc": {"kind","number"},
      "dates": {"given","due"}
    }

Design notes
------------
* Courses key on the course number (normalised). Two titles for one number is
  a data bug, not a feature, so the title is corrected in place.
* last_teacher is per-course: that is what makes the teacher auto-load when a
  course is picked, and it is rewritten the moment the user picks a teacher.
* student / teacher_profile / doc / dates are singletons - a cover carries
  exactly one of each, so only the last value survives.
* Every read-modify-write runs under engine.filelock.exclusive_lock so a
  second tab cannot tear the store.
'''

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from engine.config import LIBRARY_COVER_DIR, LIBRARY_COVER_PATH
from engine.filelock import exclusive_lock
from engine.study.cover_html import build_cover_html, clean, key

router = APIRouter()
logger = logging.getLogger('sable.study.cover')

_VERSION = 1
_MAX_ROSTER = 200

_SLOTS = ('student', 'teacher_profile', 'doc', 'dates')


# -- store ------------------------------------------------------------------


def _empty() -> dict[str, Any]:
    return {
        'version': _VERSION,
        'universities': [],
        'university': {'name': '', 'motto': '', 'department': ''},
        'courses': [],
        'course': {'no': '', 'title': ''},
        'student': {'name': '', 'roll': '', 'section': '', 'session': '',
                    'department': ''},
        'teacher_profile': {'designation': '', 'dept': ''},
        'doc': {'kind': 'Assignment', 'number': '01', 'topic': ''},
        'dates': {'given': '', 'due': ''},
        # Which cover template to render. 'classic' is the navy-band design;
        # 'elegant' is the cream/navy-banner design.
        'design': 'classic',
    }


@contextmanager
def _lock() -> Iterator[None]:
    LIBRARY_COVER_DIR.mkdir(parents=True, exist_ok=True)
    with exclusive_lock(LIBRARY_COVER_DIR / '.cover.lock'):
        yield


_DESIGNS = ('classic', 'elegant')


def _normalise(data: dict[str, Any]) -> dict[str, Any]:
    '''Deduplicate every roster and coerce the singletons. Idempotent.'''
    out = _empty()
    design = clean(data.get('design')).lower()
    out['design'] = design if design in _DESIGNS else 'classic'

    seen_uni: set[str] = set()
    for raw in (data.get('universities') or [])[:_MAX_ROSTER]:
        if not isinstance(raw, dict):
            continue
        name = clean(raw.get('name'))
        if not name or key(name) in seen_uni:
            continue
        seen_uni.add(key(name))
        out['universities'].append({
            'name': name,
            'motto': clean(raw.get('motto')),
            'department': clean(raw.get('department')),
        })
    uni = data.get('university') if isinstance(data.get('university'), dict) else {}
    out['university'] = {f: clean(uni.get(f)) for f in out['university']}

    seen_course: set[str] = set()
    for raw in (data.get('courses') or [])[:_MAX_ROSTER]:
        if not isinstance(raw, dict):
            continue
        no = clean(raw.get('no'))
        if not no or key(no) in seen_course:
            continue
        seen_course.add(key(no))
        teachers: list[str] = []
        seen_t: set[str] = set()
        for t in (raw.get('teachers') or [])[:_MAX_ROSTER]:
            nm = clean(t)
            if nm and key(nm) not in seen_t:
                seen_t.add(key(nm))
                teachers.append(nm)
        last = clean(raw.get('last_teacher'))
        if last and key(last) not in seen_t:
            teachers.append(last)
        dates = raw.get('last_dates') if isinstance(raw.get('last_dates'), dict) else {}
        out['courses'].append({
            'no': no,
            'title': clean(raw.get('title')),
            'teachers': teachers,
            'last_teacher': last,
            'last_dates': {'given': clean(dates.get('given')),
                           'due': clean(dates.get('due'))},
        })
    cur = data.get('course') if isinstance(data.get('course'), dict) else {}
    out['course'] = {f: clean(cur.get(f)) for f in out['course']}

    for slot in _SLOTS:
        raw = data.get(slot)
        if isinstance(raw, dict):
            out[slot] = {f: clean(raw.get(f)) for f in out[slot]}
    if not out['doc']['kind']:
        out['doc']['kind'] = 'Assignment'
    if not out['doc']['number']:
        out['doc']['number'] = '01'
    # A student's department defaults to the institution's, so the elegant
    # template has something sensible to print without asking twice.
    if not out['student'].get('department'):
        out['student']['department'] = out['university'].get('department', '')
    return out


def _read() -> dict[str, Any]:
    path = LIBRARY_COVER_PATH
    if not path.exists():
        return _empty()
    try:
        raw = json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return _empty()
    if not isinstance(raw, dict):
        return _empty()
    return _normalise(raw)


def _write(data: dict[str, Any]) -> None:
    LIBRARY_COVER_DIR.mkdir(parents=True, exist_ok=True)
    target = LIBRARY_COVER_PATH
    tmp = target.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(target)


def _merge(cur: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    '''Fold a client save into the store, growing rosters + overwriting singletons.'''
    data = _normalise(cur)

    # institution: remember it, then mark it current
    uni_in = payload.get('university') if isinstance(payload.get('university'), dict) else None
    if uni_in:
        name = clean(uni_in.get('name'))
        motto = clean(uni_in.get('motto'))
        dept = clean(uni_in.get('department'))
        if name:
            rec = next((u for u in data['universities'] if key(u['name']) == key(name)), None)
            if rec is None:
                data['universities'].append({'name': name, 'motto': motto, 'department': dept})
                data['universities'] = data['universities'][-_MAX_ROSTER:]
            else:
                if motto:
                    rec['motto'] = motto
                if dept:
                    rec['department'] = dept
            data['university'] = {'name': name, 'motto': motto, 'department': dept}

    # course: remember it, stash its teacher + dates
    course_in = payload.get('course') if isinstance(payload.get('course'), dict) else None
    if course_in:
        no = clean(course_in.get('no'))
        title = clean(course_in.get('title'))
        if no:
            rec = next((c for c in data['courses'] if key(c['no']) == key(no)), None)
            if rec is None:
                rec = {'no': no, 'title': title, 'teachers': [], 'last_teacher': '',
                       'last_dates': {'given': '', 'due': ''}}
                data['courses'].append(rec)
                data['courses'] = data['courses'][-_MAX_ROSTER:]
            elif title:
                rec['title'] = title
            data['course'] = {'no': no, 'title': title or rec['title']}

            teacher = payload.get('teacher') if isinstance(payload.get('teacher'), dict) else {}
            teacher_name = clean(teacher.get('name'))
            if teacher_name:
                if not any(key(t) == key(teacher_name) for t in rec['teachers']):
                    rec['teachers'].append(teacher_name)
                    rec['teachers'] = rec['teachers'][-_MAX_ROSTER:]
                rec['last_teacher'] = teacher_name

            dates_in = payload.get('dates') if isinstance(payload.get('dates'), dict) else {}
            if dates_in.get('given') or dates_in.get('due'):
                rec['last_dates'] = {
                    'given': clean(dates_in.get('given')) or rec['last_dates'].get('given', ''),
                    'due': clean(dates_in.get('due')) or rec['last_dates'].get('due', ''),
                }

    # design: a straight overwrite, not a roster
    design_in = clean(payload.get('design')).lower()
    if design_in in _DESIGNS:
        data['design'] = design_in

    # singletons: a blank never erases a stored value
    for slot in _SLOTS:
        incoming = payload.get(slot)
        if not isinstance(incoming, dict):
            continue
        for field in data[slot]:
            val = clean(incoming.get(field))
            if val:
                data[slot][field] = val

    return _normalise(data)


def _teacher_for(data: dict[str, Any]) -> str:
    '''The teacher remembered for the currently selected course.'''
    no = key((data.get('course') or {}).get('no', ''))
    if not no:
        return ''
    for rec in data.get('courses') or []:
        if key(rec.get('no', '')) == no:
            return rec.get('last_teacher') or ''
    return ''


# -- routes -----------------------------------------------------------------


@router.get('/api/study/cover')
def cover_get() -> dict[str, Any]:
    '''Full composer state - rosters plus last-used selections.'''
    with _lock():
        return _read()


@router.put('/api/study/cover')
def cover_put(payload: dict[str, Any]) -> dict[str, Any]:
    '''Merge a save into the store and return the merged result.'''
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail='body must be an object')
    with _lock():
        data = _merge(_read(), payload)
        _write(data)
        return data


@router.delete('/api/study/cover/value')
def cover_delete_value(kind: str = '', value: str = '') -> dict[str, Any]:
    '''Forget one roster entry. kind is institution | course | teacher.'''
    kind = (kind or '').strip().lower()
    value = clean(value)
    if not value:
        raise HTTPException(status_code=400, detail='value required')
    with _lock():
        data = _read()
        if kind == 'institution':
            data['universities'] = [u for u in data['universities']
                                    if key(u['name']) != key(value)]
        elif kind == 'course':
            data['courses'] = [c for c in data['courses'] if key(c['no']) != key(value)]
        elif kind == 'teacher':
            for course in data['courses']:
                course['teachers'] = [t for t in course['teachers']
                                      if key(t) != key(value)]
                if key(course.get('last_teacher', '')) == key(value):
                    course['last_teacher'] = course['teachers'][-1] if course['teachers'] else ''
        else:
            raise HTTPException(status_code=400, detail='unknown kind')
        _write(data)
        return data


def _slug(value: str, fallback: str = 'x') -> str:
    s = re.sub(r'[^A-Za-z0-9._-]+', '_', (value or '').strip()).strip('_')
    return s[:40] or fallback


# Single ordered priority list. Whichever candidate is found FIRST is the
# renderer. Rendered as `kind` -> how to drive it:
#
#   'chrome'  - Chromium CLI  (--headless=new --print-to-pdf)
#   'edge'    - Chromium CLI  (same flags, Edge-branded build)
#   'playwright' - Playwright driver; no CLI binary needed
#   'firefox' - detected only; its CLI has no --print-to-pdf, so it is
#               listed last and reported as unsupported rather than used
#
# PRIORITY (highest first): chrome > edge > playwright > firefox
_RENDER_PRIORITY: tuple[tuple[str, tuple[str, ...]], ...] = (
    ('chrome', (
        'google-chrome-stable', 'google-chrome', 'chromium',
        'chromium-browser', 'chrome', 'brave-browser', 'brave', 'vivaldi',
    )),
    ('edge', (
        'microsoft-edge-stable', 'microsoft-edge',
        'microsoft-edge-beta', 'microsoft-edge-dev',
    )),
    ('playwright', ()),
    ('firefox', ('firefox', 'firefox-esr', 'firefox-developer-edition')),
)


def _find_chrome() -> str | None:
    '''First Chromium-family CLI available, or None.

    WHY A BROWSER AT ALL: the cover is an HTML/CSS document. Only a browser
    engine lays out @page, mm units, flexbox, gradients and webfonts the way
    the template expects, so the browser IS the renderer. The only real
    question is WHICH browser - and it should be one that is already on the
    machine, not a second ~150MB Playwright download.
    '''
    for kind, names in _RENDER_PRIORITY:
        if kind not in ('chrome', 'edge'):
            continue
        for name in names:
            found = shutil.which(name)
            if found:
                return found
    return None


def _find_firefox() -> str | None:
    '''Locate Firefox, for a clear "found but cannot print" error message.'''
    for name in ('firefox', 'firefox-esr', 'firefox-developer-edition'):
        found = shutil.which(name)
        if found:
            return found
    return None


def _browser_candidates() -> list[tuple[str, str]]:
    '''Every renderer this machine can offer, in priority order.

    Returns (kind, path) where path is '' for 'playwright' (it has no CLI
    binary - the driver locates the browser itself). Firefox is included so
    it can be reported as "found but unsupported" rather than silently
    dropped. This is the single source of truth for both the render loop and
    any diagnostics.
    '''
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for kind, names in _RENDER_PRIORITY:
        if kind == 'playwright':
            out.append(('playwright', ''))
            continue
        for name in names:
            found = shutil.which(name)
            if found and found not in seen:
                seen.add(found)
                out.append((kind, found))
    return out


def _render_with_chrome(chrome: str, html_path: Path, out_path: Path) -> None:
    '''Print HTML to PDF by shelling out to the system Chrome.

    --user-data-dir is LOAD-BEARING. Without an isolated profile, an already
    running Chrome instance swallows the invocation (opens a tab in the user's
    session and exits) and no PDF is ever written. A throwaway profile also
    keeps the real session, history and cookies untouched.
    '''
    with tempfile.TemporaryDirectory(prefix='sable-cover-') as profile:
        cmd = [
            chrome,
            '--headless=new',
            '--disable-gpu',
            '--no-sandbox',
            '--disable-dev-shm-usage',
            '--hide-scrollbars',
            '--no-pdf-header-footer',
            '--virtual-time-budget=8000',
            '--run-all-compositor-stages-before-draw',
            '--user-data-dir=%s' % profile,
            '--print-to-pdf=%s' % out_path,
            html_path.as_uri(),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        try:
            size = out_path.stat().st_size
        except OSError:
            size = 0
        if size == 0:
            tail = ((proc.stderr or '') + (proc.stdout or ''))[-500:]
            raise RuntimeError('chrome produced no pdf (exit %s): %s'
                               % (proc.returncode, tail.strip()))


def _render_with_playwright(html_path: Path, out_path: Path) -> None:
    '''Fallback renderer.

    Tries Playwright's system-Chrome channel first, so this path still works
    on a machine where the bundled chromium_headless_shell was never
    downloaded - which is exactly the failure this fixes.
    '''
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = None
        errors: list[str] = []
        for opts in ({'channel': 'chrome'}, {'channel': 'chromium'}, {}):
            try:
                browser = p.chromium.launch(**opts)
                break
            except Exception as exc:
                errors.append('%s -> %s' % (opts or 'bundled', exc))
        if browser is None:
            raise RuntimeError('no usable browser: ' + ' | '.join(errors))
        try:
            page = browser.new_page(viewport={'width': 794, 'height': 1123})
            page.goto(html_path.as_uri(), wait_until='load')
            page.wait_for_timeout(400)
            page.pdf(path=str(out_path), format='A4', print_background=True,
                     prefer_css_page_size=True,
                     margin={'top': '0', 'bottom': '0', 'left': '0', 'right': '0'})
        finally:
            browser.close()


def _render_pdf(doc: dict[str, Any], out_path: Path) -> None:
    '''Render the cover HTML to PDF.

    System Chrome first (already installed, no extra download), Playwright as
    a fallback. Either way the HTML template stays the single source of truth.
    '''
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_html = out_path.with_suffix('.html')
    tmp_html.write_text(
        build_cover_html(doc, _teacher_for(doc), doc.get('design') or 'classic'),
        encoding='utf-8',
    )
    try:
        # Walk the priority list: chrome > edge > playwright > firefox.
        # Firefox is detection-only and is skipped here on purpose.
        errors: list[str] = []
        firefox_hint = ''
        for kind, path in _browser_candidates():
            if kind == 'firefox':
                firefox_hint = (' Firefox was found at %s but its CLI only '
                                'supports --screenshot, not --print-to-pdf - '
                                'it cannot make PDFs.' % path)
                continue
            try:
                if kind == 'playwright':
                    _render_with_playwright(tmp_html, out_path)
                    logger.info('cover rendered via playwright (%s)',
                                'after chrome/edge unavailable')
                else:
                    _render_with_chrome(path, tmp_html, out_path)
                    logger.info('cover rendered via %s (%s)', path, kind)
                return
            except Exception as exc:
                logger.warning('%s render failed via %s: %s',
                               kind, path or 'driver', exc)
                errors.append('%s%s -> %s'
                              % (kind, ' ' + path if path else '', exc))

        raise RuntimeError(
            'no browser could render a PDF. Tried: %s.%s'
            % ('; '.join(errors) or 'none found', firefox_hint)
        )
    finally:
        try:
            tmp_html.unlink()
        except OSError:
            pass


@router.post('/api/study/cover/generate')
def cover_generate(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    '''Save the payload, render the cover to PDF, return its download URL.'''
    payload = payload if isinstance(payload, dict) else {}
    with _lock():
        data = _merge(_read(), payload)
        _write(data)

    course = data.get('course') or {}
    student = data.get('student') or {}
    doc = data.get('doc') or {}
    stamp = time.strftime('%Y%m%d-%H%M%S')
    name = '_'.join(p for p in (
        _slug(student.get('roll'), ''),
        _slug(course.get('no')),
        _slug(doc.get('kind')),
        _slug(doc.get('number')),
        _slug(doc.get('design') or 'classic'),
        stamp,
    ) if p) + '.pdf'
    out_path = LIBRARY_COVER_DIR / name

    try:
        _render_pdf(data, out_path)
    except Exception as exc:
        logger.exception('cover render failed')
        raise HTTPException(status_code=500, detail='render failed: %s' % exc) from exc

    return {'ok': True, 'file': str(out_path), 'name': name,
            'url': '/api/study/cover/file/' + name, 'state': data}


@router.get('/api/study/cover/file/{name}')
def cover_file(name: str) -> FileResponse:
    safe = Path(name).name
    if not safe.lower().endswith('.pdf'):
        raise HTTPException(status_code=400, detail='not a pdf')
    path = LIBRARY_COVER_DIR / safe
    if not path.exists():
        raise HTTPException(status_code=404, detail='not found')
    return FileResponse(str(path), media_type='application/pdf', filename=safe)
