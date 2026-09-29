'''Rebuild engine/data/bangla_en.tsv.gz from the open Bengali datasets.

Sources (MinhasKamal/BengaliDictionary, MIT):
  BengaliDictionary_36.csv   36k  english,bangla
  BengaliDictionary_93.csv   93k  english,bangla  (includes inflections)
  BengaliDictionary.json     17k  english,bangla + bn_syns + example sentences

The result is one headword per line:
  <english lowercased>\t<bangla meaning>|<bangla meaning>|...

Run from the project root:
  uv run python scripts/build_bangla_dict.py
'''
from __future__ import annotations

import csv
import gzip
import io
import json
import re
import sys
import urllib.request
from pathlib import Path

BASE = 'https://raw.githubusercontent.com/MinhasKamal/BengaliDictionary/master/'
CSV_FILES = ('BengaliDictionary_36.csv', 'BengaliDictionary_93.csv')
JSON_FILE = 'BengaliDictionary.json'
OUT = Path(__file__).resolve().parent.parent / 'engine' / 'data' / 'bangla_en.tsv.gz'
MAX_MEANINGS = 8
_WS = re.compile(r'\s+')
# Spreadsheet error cells that leaked into the English column upstream.
_EXCEL_ERROR = re.compile(r'^#(?:name|ref|value|n/a|div/0!|null|num)\??$', re.IGNORECASE)


def is_junk_key(key: str) -> bool:
    '''True for headwords that are not words at all.

    The upstream CSVs were edited in Excel at some point, so a few #NAME? /
    #REF! error cells ended up in the English column and became "headwords".
    '''
    return not key or bool(_EXCEL_ERROR.match(key))


def _fetch(name: str) -> bytes:
    url = BASE + name
    print(f'  fetching {name} …', end='', flush=True)
    with urllib.request.urlopen(url, timeout=180) as res:
        data = res.read()
    print(f' {len(data) / 1e6:.1f} MB')
    return data


def _add(merged: dict, en: str, bn: str, stats: dict) -> None:
    en = (en or '').strip().lower()
    bn = _WS.sub(' ', (bn or '').strip()).strip(',')
    if not en or not bn:
        stats['empty'] += 1
        return
    if is_junk_key(en):
        stats['junk'] += 1
        return
    bucket = merged.setdefault(en, [])
    for part in bn.split(','):
        part = part.strip()
        if part and part not in bucket:
            bucket.append(part)


def main() -> int:
    merged: dict[str, list[str]] = {}
    stats = {'empty': 0, 'junk': 0, 'malformed': 0}

    for name in CSV_FILES:
        raw = _fetch(name)
        rows = 0
        reader = csv.reader(io.StringIO(raw.decode('utf-8-sig', errors='replace')))
        for row in reader:
            if len(row) >= 2:
                _add(merged, row[0], row[1], stats)
                rows += 1
            else:
                # A Bangla-only line (the English column was blank) shows up as
                # a single field. Counted rather than silently skipped.
                stats['malformed'] += 1
        print(f'  {name}: {rows} rows')

    raw = _fetch(JSON_FILE)
    data = json.loads(raw.decode('utf-8-sig', errors='replace'))
    for entry in data:
        if not isinstance(entry, dict):
            stats['malformed'] += 1
            continue
        _add(merged, entry.get('en', ''), entry.get('bn', ''), stats)
        for syn in (entry.get('bn_syns') or []):
            _add(merged, entry.get('en', ''), syn, stats)
    print(f'  {JSON_FILE}: {len(data)} entries')

    final = {k: v[:MAX_MEANINGS] for k, v in merged.items() if v}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(OUT, 'wt', encoding='utf-8') as fh:
        for en in sorted(final):
            fh.write(en + '\t' + '|'.join(final[en]) + '\n')

    print(f'\nwrote {len(final)} headwords -> {OUT}')
    print(f'file size: {OUT.stat().st_size / 1e6:.2f} MB')
    dropped = stats['empty'] + stats['junk'] + stats['malformed']
    print(f'dropped {dropped} rows '
          f'(empty={stats["empty"]} junk={stats["junk"]} malformed={stats["malformed"]})')
    return 0


if __name__ == '__main__':
    sys.exit(main())
