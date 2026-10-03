'''Cover-page HTML template for the study Cover Page tab.

Kept apart from the route module so the (large, brace-heavy) CSS never has to
survive an f-string. ``build_cover_html`` is the only public entry point.

The palette and layout mirror the standalone cover we designed by hand:
navy -> royal gradient header, gold rules, Cinzel / Cormorant Garamond / Inter.
Those three families are installed system-wide, so the render never needs the
network.
'''

from __future__ import annotations

import html as _html
import re
import time
from typing import Any

_MAX_FIELD = 160

DEFAULT_MOTTO = 'Heaven\u2019s Light is Our Guide'

_MONTHS = ['January', 'February', 'March', 'April', 'May', 'June',
           'July', 'August', 'September', 'October', 'November', 'December']


# -- helpers ----------------------------------------------------------------


def clean(value: Any, limit: int = _MAX_FIELD) -> str:
    '''Collapse whitespace and clamp length. Never returns None.'''
    text = str(value if value is not None else '')
    text = re.sub(r'\s+', ' ', text).strip()
    return text[:limit]


def _esc(value: Any) -> str:
    return _html.escape(clean(value), quote=True)


def key(value: str) -> str:
    '''Case/space-insensitive identity for a course number or a name.'''
    return re.sub(r'[^a-z0-9]', '', (value or '').lower())


def today() -> str:
    t = time.localtime()
    return '%d %s %d' % (t.tm_mday, _MONTHS[t.tm_mon - 1], t.tm_year)


# -- CSS (plain string: no f-string, so every brace stays literal) ----------

_CSS = '''
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0;
  -webkit-print-color-adjust:exact !important;print-color-adjust:exact !important;
  font-variant-numeric:lining-nums tabular-nums;
  font-feature-settings:'lnum' 1,'tnum' 1;}
@page{size:A4;margin:0;}
html,body{width:210mm;height:297mm;}
body{font-family:'Inter','DejaVu Sans',sans-serif;color:#1A2230;
  background:#fff;position:relative;overflow:hidden;}
.page{position:relative;width:210mm;height:297mm;}
.frame{position:absolute;inset:8mm;border:1.2pt solid #C9A227;pointer-events:none;}
.frame::before{content:'';position:absolute;inset:1.6mm;
  border:.5pt solid rgba(201,162,39,.45);}
.corner{position:absolute;width:9mm;height:9mm;border:2pt solid #C9A227;}
.corner.tl{top:6.4mm;left:6.4mm;border-right:0;border-bottom:0;}
.corner.tr{top:6.4mm;right:6.4mm;border-left:0;border-bottom:0;}
.corner.bl{bottom:6.4mm;left:6.4mm;border-right:0;border-top:0;}
.corner.br{bottom:6.4mm;right:6.4mm;border-left:0;border-top:0;}
.header{position:relative;overflow:hidden;color:#fff;padding:14mm 16mm 12mm;
  background:linear-gradient(135deg,#0A1A2F 0%,#122A4C 55%,#1B3A6B 100%);}
.header::after{content:'';position:absolute;left:0;right:0;bottom:0;height:1.6mm;
  background:linear-gradient(90deg,#C9A227 0%,#E8C766 50%,#C9A227 100%);}
.header .glow{position:absolute;right:-30mm;top:-55mm;width:95mm;height:95mm;
  border-radius:50%;
  background:radial-gradient(circle,rgba(255,255,255,.10) 0%,rgba(255,255,255,0) 68%);}
.header .ring{position:absolute;right:-30mm;bottom:-70mm;width:130mm;height:130mm;
  border-radius:50%;border:1px solid rgba(255,255,255,.10);}
.header .ring2{position:absolute;left:-45mm;top:-45mm;width:100mm;height:100mm;
  border-radius:50%;border:1px solid rgba(201,162,39,.18);}
.motto{text-align:center;font-family:'Cormorant Garamond',serif;font-style:italic;
  font-size:11.5pt;letter-spacing:.16em;color:#E8C766;margin-bottom:6mm;}
.motto .rule{display:block;width:38mm;height:1px;margin:3.5mm auto 0;
  background:linear-gradient(90deg,transparent,#E8C766,transparent);}
.uni{text-align:center;font-family:'Cinzel',serif;font-weight:900;font-size:22pt;
  line-height:1.22;letter-spacing:.02em;text-shadow:0 1px 0 rgba(0,0,0,.25);}
.dept{text-align:center;font-family:'Cormorant Garamond',serif;font-weight:600;
  font-size:13pt;letter-spacing:.06em;color:#DCE4F2;margin-top:3.5mm;}
.body{padding:14mm 16mm 0;}
.course{background:#F5F7FB;border:1px solid #D9DEE8;border-left:4pt solid #C9A227;
  border-radius:2mm;padding:6.5mm 8mm;display:grid;grid-template-columns:1fr 1fr;
  gap:2.4mm 10mm;margin-bottom:11mm;}
.course .k{font-size:8pt;letter-spacing:.14em;text-transform:uppercase;
  color:#5A6577;font-weight:600;}
.course .v{font-size:11.5pt;font-weight:600;color:#1B3A6B;margin-top:.6mm;}
.titleblock{text-align:center;margin:2mm 0 10mm;}
.titleblock .num{font-family:'Cinzel',serif;font-weight:700;font-size:12pt;
  letter-spacing:.22em;color:#C9A227;text-transform:uppercase;}
.titleblock .tsep{width:22mm;height:1.4pt;margin:4mm auto;
  background:linear-gradient(90deg,#C9A227,#E8C766);}
.titleblock .ttl{font-family:'Cormorant Garamond',serif;font-weight:700;
  font-size:24pt;line-height:1.28;color:#0A1A2F;}
.people{display:grid;grid-template-columns:1fr 1fr;border:1px solid #D9DEE8;
  border-radius:2mm;overflow:hidden;}
.col{padding:9mm;background:#fff;}
.col + .col{border-left:1px solid #D9DEE8;background:#F5F7FB;}
.col h4{font-family:'Cinzel',serif;font-size:9pt;font-weight:700;
  letter-spacing:.2em;text-transform:uppercase;color:#1B3A6B;
  padding-bottom:3mm;margin-bottom:4mm;border-bottom:1px solid #C9A227;}
.col .name{font-family:'Cormorant Garamond',serif;font-size:17pt;font-weight:700;
  line-height:1.15;color:#0A1A2F;margin-bottom:2.8mm;}
.col .row{font-size:10pt;color:#1A2230;margin:1.6mm 0;display:flex;
  align-items:baseline;}
.col .row .lbl{flex:0 0 30mm;color:#5A6577;font-weight:500;}
.col .row .val{font-weight:600;}
.ornament{display:flex;align-items:center;justify-content:center;gap:4mm;
  margin-top:15mm;}
.ornament .ln{width:42mm;height:1px;
  background:linear-gradient(90deg,transparent,#C9A227);}
.ornament .ln.r{background:linear-gradient(90deg,#C9A227,transparent);}
.ornament .gem{width:3mm;height:3mm;background:#C9A227;transform:rotate(45deg);
  box-shadow:0 0 0 1.1mm #fff,0 0 0 1.5mm rgba(201,162,39,.5);}
.footer{position:absolute;left:16mm;right:16mm;bottom:14mm;}
.datestrip{display:flex;justify-content:space-between;gap:8mm;border-radius:2mm;
  padding:5.5mm 8mm;color:#fff;border-bottom:1.2mm solid #C9A227;
  background:linear-gradient(135deg,#0A1A2F 0%,#1B3A6B 100%);}
.datestrip .item{text-align:center;flex:1;}
.datestrip .lab{font-size:7.5pt;letter-spacing:.16em;text-transform:uppercase;
  color:#E8C766;font-weight:600;margin-bottom:1.4mm;}
.datestrip .val{font-family:'Cormorant Garamond',serif;font-size:13pt;
  font-weight:600;}
.seal{text-align:center;margin-top:5mm;font-family:'Cormorant Garamond',serif;
  font-style:italic;font-size:9.5pt;color:#5A6577;}
'''


# -- template ---------------------------------------------------------------


def build_cover_html(data: dict[str, Any], teacher_name: str = '') -> str:
    '''Render the composer state to a standalone HTML document.'''
    uni = data.get('university') or {}
    course = data.get('course') or {}
    student = data.get('student') or {}
    teacher = data.get('teacher_profile') or {}
    doc = data.get('doc') or {}
    dates = data.get('dates') or {}

    kind = _esc(doc.get('kind')) or 'Assignment'
    number = _esc(doc.get('number')) or '01'

    parts = [
        '<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">',
        '<title>Cover</title><style>', _CSS, '</style></head><body>',
        '<div class="page">',
        '<div class="frame"></div>',
        '<div class="corner tl"></div><div class="corner tr"></div>',
        '<div class="corner bl"></div><div class="corner br"></div>',
        '<div class="header">',
        '<div class="glow"></div><div class="ring"></div><div class="ring2"></div>',
        '<div class="motto">', _esc(uni.get('motto')) or DEFAULT_MOTTO,
        '<span class="rule"></span></div>',
        '<div class="uni">', _esc(uni.get('name')) or 'University Name', '</div>',
        '<div class="dept">', _esc(uni.get('department')) or 'Department', '</div>',
        '</div>',
        '<div class="body">',
        '<div class="course">',
        '<div><div class="k">Course No.</div><div class="v">',
        _esc(course.get('no')) or '&mdash;', '</div></div>',
        '<div><div class="k">Course Title</div><div class="v">',
        _esc(course.get('title')) or '&mdash;', '</div></div>',
        '</div>',
        '<div class="titleblock">',
        '<div class="num">', kind, ' No. ', number, '</div>',
        '<div class="tsep"></div>',
        '<div class="ttl">', _esc(course.get('title')) or '&mdash;', '</div>',
        '</div>',
        '<div class="people">',
        '<div class="col"><h4>Submitted By</h4>',
        '<div class="name">', _esc(student.get('name')) or '&mdash;', '</div>',
        '<div class="row"><span class="lbl">Roll</span><span class="val">',
        _esc(student.get('roll')) or '&mdash;', '</span></div>',
        '<div class="row"><span class="lbl">Section</span><span class="val">',
        _esc(student.get('section')) or '&mdash;', '</span></div>',
        '<div class="row"><span class="lbl">Session</span><span class="val">',
        _esc(student.get('session')) or '&mdash;', '</span></div>',
        '</div>',
        '<div class="col"><h4>Submitted To</h4>',
        '<div class="name">', _esc(teacher_name) or '&mdash;', '</div>',
        '<div class="row"><span class="lbl">Designation</span><span class="val">',
        _esc(teacher.get('designation')) or '&mdash;', '</span></div>',
        '<div class="row"><span class="lbl">Dept.</span><span class="val">',
        _esc(teacher.get('dept')) or '&mdash;', '</span></div>',
        '</div>',
        '</div>',
        '<div class="ornament"><span class="ln"></span>',
        '<span class="gem"></span><span class="ln r"></span></div>',
        '</div>',
        '<div class="footer">',
        '<div class="datestrip">',
        '<div class="item"><div class="lab">Date of ', kind, '</div>',
        '<div class="val">', _esc(dates.get('given')) or today(), '</div></div>',
        '<div class="item"><div class="lab">Date of Submission</div>',
        '<div class="val">', _esc(dates.get('due')) or today(), '</div></div>',
        '</div>',
        '<div class="seal">&mdash; Submitted in partial fulfillment of the ', 
        'requirements of ', _esc(course.get('no')) or 'the course', ' &mdash;</div>',
        '</div>',
        '</div></body></html>',
    ]
    return ''.join(parts)
