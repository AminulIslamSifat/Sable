'''Elegant cover template — cream paper, navy banners, diamond corners.

Every vertical position below was MEASURED off the reference PDF (rendered at
110dpi, band-detected on ink coverage), not eyeballed. The layout is expressed
in millimetres against those targets:

    motto centre      ~23mm        ASSIGNMENT banner   71.9 - 90.3mm
    diamond           ~29.6mm      topic label         ~101mm
    university l1     ~39mm        topic (2 lines)     ~110 - 119mm
    university l2     ~46.6mm      ornament            ~130mm
    department        ~57mm        course block        ~143 - 151mm
    gold rule         ~64.7mm      SUBMITTED banners   177.0 - 186.8mm
                                   names               ~200mm

Palette sampled from the same reference:

    paper  #FDFCF8    navy  #14294D    gold  #B8860B    gold soft  #D7BB75

Two rules that must not be broken:
  1. NO linear-gradients. poppler (Evince/Okular) rasterises those as flat gray.
  2. The topic block is width-capped (max-width) so a long title wraps to two
     lines, which is what gives the middle of the page its intended proportion.
'''

from __future__ import annotations

from typing import Any

from engine.study.cover_html import DEFAULT_MOTTO, _esc, clean

_CSS = '''
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0;
  -webkit-print-color-adjust:exact !important;print-color-adjust:exact !important;
  font-variant-numeric:lining-nums tabular-nums;
  font-feature-settings:'lnum' 1,'tnum' 1;}
@page{size:A4;margin:0;}
html,body{width:210mm;height:297mm;}
body{font-family:'Inter','DejaVu Sans',sans-serif;color:#14294D;
  background:#FDFCF8;overflow:hidden;}
/* overflow:hidden is LOAD-BEARING. The decorative arcs below are positioned to
   bleed past the page edge; without clipping, Chromium widens the document to
   fit them and then scales the entire cover down to fit A4 (measured: a 60mm
   overhang shrank every element to 78%). Do not remove. */
.page{position:relative;width:210mm;height:297mm;background:#FDFCF8;
  overflow:hidden;}

/* Gold frame: a DOUBLE rule (outer + inner line ~2mm apart) with diamond corner
   marks. Measured off the reference at 150dpi - outer line centre at 9.0mm,
   inner at 10.8mm, which is why .frame sits at 9mm and ::before steps in 2mm.
   A single border reads visibly cheaper than the reference; keep both lines. */
.frame{position:absolute;top:9mm;right:9mm;bottom:9mm;left:9mm;
  border:0.9pt solid #B8860B;}
.frame::before{content:'';position:absolute;inset:1.5mm;
  border:0.6pt solid #B8860B;}
/* Diamonds are centred on the frame corner: half of the 4.2mm box back off
   9mm => 6.9mm. (transform:rotate does not grow the layout box.) */
.dia{position:absolute;width:3.4mm;height:3.4mm;background:#B8860B;
  transform:rotate(45deg);}
.dia.tl{top:7.3mm;left:7.3mm;}
.dia.tr{top:7.3mm;right:7.3mm;}
.dia.bl{bottom:7.3mm;left:7.3mm;}
.dia.br{bottom:7.3mm;right:7.3mm;}

/* faint background arcs */
.arc{position:absolute;border-radius:50%;border:0.5pt solid rgba(184,134,11,.10);}
.arc.a{top:-54mm;left:-44mm;width:112mm;height:112mm;}
.arc.b{bottom:-74mm;right:-60mm;width:152mm;height:152mm;}

/* content column, inset from the frame on both sides */
.inner{position:relative;padding:20mm 25mm 0;}

/* -- masthead -- */
.motto{text-align:center;font-family:'Cormorant Garamond',serif;font-style:italic;
  font-size:12pt;color:#B8860B;line-height:1;}
/* Measured: the reference's masthead diamond is 13px tall at 150dpi = 2.2mm
   bounding box, so a ~1.6mm side. Mine was 2.6mm and visibly chunkier. */
.mast-dia{width:1.6mm;height:1.6mm;background:#B8860B;transform:rotate(45deg);
  margin:4.6mm auto 0;}
/* Font size is MEASURED, not chosen: the reference's university line has a
   3.6mm cap height, which for Cinzel (cap ratio ~0.72) is a 14.5pt em. At 21pt
   the masthead dominated the page and pushed everything below it down. */
.uni{text-align:center;font-family:'Cinzel',serif;font-weight:700;font-size:14.5pt;
  line-height:1.45;color:#14294D;margin-top:5.2mm;}
.dept{text-align:center;font-size:8.6pt;font-weight:600;letter-spacing:.22em;
  color:#5A6577;margin-top:5mm;text-transform:uppercase;}
/* 1.4pt, measured: the reference draws this at ~3px @150dpi = 0.51mm. */
.rule1{width:34mm;height:1.4pt;background:#B8860B;margin:4.5mm auto 0;}

/* -- document banner -- */
.banner{margin-top:2mm;background:#14294D;color:#fff;text-align:center;
  padding:5.2mm 0;font-family:'Cinzel',serif;font-weight:700;font-size:20pt;
  letter-spacing:.34em;text-indent:.34em;line-height:1.1;}
/* 1.9pt, measured: ~4px @150dpi = 0.68mm in the reference. */
.banner-rule{height:1.9pt;background:#B8860B;}

/* -- topic -- */
.tlabel{text-align:center;font-size:7.6pt;font-weight:700;letter-spacing:.26em;
  color:#B8860B;margin-top:8mm;text-transform:uppercase;}
/* max-width is what forces the two-line wrap the reference has. Widen it and
   the topic runs to one line, which shortens the whole middle of the page. */
.topic{text-align:center;font-family:'Cormorant Garamond',serif;font-weight:700;
  font-style:italic;font-size:19.5pt;line-height:1.3;color:#14294D;
  max-width:74mm;margin:3.6mm auto 0;}
.orn{display:flex;align-items:center;justify-content:center;gap:3mm;
  margin-top:7mm;}
.orn .ln{width:16mm;height:1pt;background:#D7BB75;}
.orn .gem{width:1.7mm;height:1.7mm;background:#B8860B;transform:rotate(45deg);}

/* -- course row -- */
/* Asymmetric on purpose: the reference puts its divider at ~41% of the content
   width, not 50%, so the course title gets the room it needs to stay on one
   line. An even split wraps it to two and pushes the whole page down. */
.course{display:grid;grid-template-columns:0.66fr 0.6pt 1fr;gap:0 9mm;
  align-items:stretch;margin-top:9mm;}
.course .vline{width:0.6pt;background:#D7BB75;}
.course .cell{text-align:center;}
.course .k{font-size:7.4pt;font-weight:700;letter-spacing:.2em;color:#B8860B;
  text-transform:uppercase;line-height:1;}
.course .v{font-family:'Cormorant Garamond',serif;font-weight:700;font-size:15pt;
  color:#14294D;line-height:1.28;margin-top:3.4mm;}

/* -- people -- */
/* The submitted block is inset ~7mm further than the banner on each side:
   measured 145.8mm wide in the reference against the banner's 159.9mm. */
.people{display:grid;grid-template-columns:1fr 0.6pt 1fr;gap:0 9mm;
  align-items:stretch;margin-top:23mm;padding:0 7mm;}
.people .vline{width:0.6pt;background:#D7BB75;}
.phead{background:#14294D;color:#fff;text-align:center;padding:3.3mm 0;
  font-size:8pt;font-weight:700;letter-spacing:.24em;text-transform:uppercase;
  line-height:1.1;}
.pname{font-family:'Cormorant Garamond',serif;font-weight:700;font-size:16pt;
  color:#14294D;margin:6.4mm 0 0;line-height:1.2;}
.prole{font-family:'Cormorant Garamond',serif;font-style:italic;font-size:11pt;
  color:#5A6577;margin-top:2.2mm;}
.prow{font-size:10pt;color:#14294D;margin-top:2.4mm;line-height:1.35;}
.prow .k{font-weight:700;}
.pblock{font-size:10pt;color:#14294D;margin-top:4mm;line-height:1.45;}

/* -- footer -- */
.foot{position:absolute;left:26mm;right:26mm;bottom:18mm;text-align:center;}
.foot .rule{width:34mm;height:0.6pt;background:#B8860B;margin:0 auto 4.5mm;}
.foot .txt{font-size:8.2pt;font-weight:600;letter-spacing:.2em;color:#5A6577;
  text-transform:uppercase;}
'''


def build_elegant_html(state: dict[str, Any], teacher_name: str = '') -> str:
    uni = state.get('university') or {}
    course = state.get('course') or {}
    student = state.get('student') or {}
    teacher = state.get('teacher_profile') or {}
    doc = state.get('doc') or {}

    kind_raw = clean(doc.get('kind')) or 'Assignment'
    kind = _esc(kind_raw).upper()
    number = _esc(doc.get('number'))
    banner = kind + (('  ' + number) if number and number != '01' else '')

    def p(v: Any, fallback: str = '&mdash;') -> str:
        s = _esc(v)
        return s if s else fallback

    parts = [
        '<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">',
        '<title>Cover</title><style>', _CSS, '</style></head><body>',
        '<div class="page">',
        '<div class="frame"></div>',
        '<div class="arc a"></div><div class="arc b"></div>',
        '<div class="dia tl"></div><div class="dia tr"></div>',
        '<div class="dia bl"></div><div class="dia br"></div>',
        '<div class="inner">',

        '<div class="motto">', _esc(uni.get('motto')) or DEFAULT_MOTTO, '</div>',
        '<div class="mast-dia"></div>',
        '<div class="uni">', p(uni.get('name'), 'University Name'), '</div>',
        '<div class="dept">', p(uni.get('department'), 'Department'), '</div>',
        '<div class="rule1"></div>',

        '<div class="banner">', banner, '</div>',
        '<div class="banner-rule"></div>',

        '<div class="tlabel">', _esc(kind_raw), ' Topic</div>',
        '<div class="topic">',
        p(doc.get('topic') or course.get('title'), 'Topic'), '</div>',
        '<div class="orn"><span class="ln"></span>',
        '<span class="gem"></span><span class="ln"></span></div>',

        '<div class="course">',
        '<div class="cell"><div class="k">Course No</div>',
        '<div class="v">', p(course.get('no')), '</div></div>',
        '<div class="vline"></div>',
        '<div class="cell"><div class="k">Course Title</div>',
        '<div class="v">', p(course.get('title')), '</div></div>',
        '</div>',

        '<div class="people">',
        '<div>',
        '<div class="phead">Submitted By</div>',
        '<div class="pname">', p(student.get('name')), '</div>',
        '<div class="prow"><span class="k">Roll:</span> ',
        p(student.get('roll'), ''), '</div>',
        '<div class="prow"><span class="k">Section:</span> ',
        p(student.get('section'), ''), '</div>',
        '<div class="pblock"><span class="k">Department:</span><br>',
        p(student.get('department') or uni.get('department')), '</div>',
        '<div class="prow" style="margin-top:4mm"><span class="k">Session:</span> ',
        p(student.get('session'), ''), '</div>',
        '</div>',
        '<div class="vline"></div>',
        '<div>',
        '<div class="phead">Submitted To</div>',
        '<div class="pname">', p(teacher_name), '</div>',
        '<div class="prole">', p(teacher.get('designation'), ''), '</div>',
        '<div class="pblock">', p(teacher.get('dept')), '</div>',
        '<div class="pblock">', p(uni.get('name')), '</div>',
        '</div>',
        '</div>',

        '</div>',

        '<div class="foot">',
        '<div class="rule"></div>',
        '<div class="txt">', p(uni.get('department'), 'Department'), '</div>',
        '</div>',

        '</div></body></html>',
    ]
    return ''.join(parts)