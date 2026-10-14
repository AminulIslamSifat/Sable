"""Regression: bare block render tags (no <render_component> wrapper)."""
import sys, json
sys.path.insert(0, ".")
from engine.skills.parser import SkillParser


def run(txt, chunks=None):
    p = SkillParser()
    ev = []
    if chunks:
        for c in chunks:
            ev += list(p.feed(c))
    else:
        ev += list(p.feed(txt))
    ev += list(p.flush())
    return ev


def kinds(ev):
    return [(e.get("type"), e.get("partial")) for e in ev]


FAIL = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (("  " + detail) if detail and not cond else ""))
    if not cond:
        FAIL.append(name)


# 1. bare flow, sealed
FLOW = '<flow current="2">\n  <flow-item title="Clear the way" text="Move the top {{n-1}} disks."/>\n  <flow-item title="Move the big one" text="Slide it."/>\n</flow>'
ev = run(FLOW)
uis = [e for e in ev if e.get("type") == "ui_block"]
check("bare flow emits ui_block", len(uis) == 1 and not uis[0].get("partial"), str(kinds(ev)))
node = uis[0]["payload"]["components"][0]["data"]["nodes"][0]
check("flow kind preserved", node["kind"] == "flow")
check("current attr preserved", node.get("current") == "2")
check("two flow-items", len(node["children"]) == 2)
check("chip text intact", "{{n-1}}" in node["children"][0]["text"])

# 2. no raw markup leaks as text
texts = "".join(e.get("text", "") for e in ev if e.get("type") == "text")
check("no markup leaked to text", "<flow" not in texts and "flow-item" not in texts, repr(texts[:120]))

# 3. prose before bare block
EV2 = run('Here you go:\n<card title="Hi"><text>hello</text></card>')
check("prose preserved before block", any(e.get("type") == "text" and "Here you go" in e.get("text", "") for e in EV2))
check("card rendered", any(e.get("type") == "ui_block" for e in EV2))

# 4. fenced code stays literal
FENCE = 'Look:\n```\n<flow current="2"><flow-item title="A" text="x"/></flow>\n```'
EV3 = run(FENCE)
check("fenced block NOT rendered", not any(e.get("type") == "ui_block" for e in EV3), str(kinds(EV3)))
ftxt = "".join(e.get("text", "") for e in EV3 if e.get("type") == "text")
check("fenced content preserved as text", "flow-item" in ftxt, repr(ftxt))

# 5. unterminated at EOF -> sealed final, not text
EV4 = run('<flow current="2"><flow-item title="A" text="x"/><flow-item title="B" text="y"/>')
u4 = [e for e in EV4 if e.get("type") == "ui_block"]
check("unterminated bare tag sealed at flush", any(not e.get("partial") for e in u4), str(kinds(EV4)))
t4 = "".join(e.get("text", "") for e in EV4 if e.get("type") == "text")
check("unterminated didn't leak", "flow-item" not in t4, repr(t4))

# 6. streamed across chunks -> partials then seal
p = SkillParser()
chunks = ['<flow current="1">\n', '<flow-item title="One" text="aaa"/>\n', '<flow-item title="Two" text="bbb"/>\n', '</flow>']
EV5 = []
for c in chunks:
    EV5 += list(p.feed(c))
EV5 += list(p.flush())
u5 = [e for e in EV5 if e.get("type") == "ui_block"]
check("chunked stream produces ui_block", len(u5) >= 1, str(kinds(EV5)))
check("chunked stream seals", any(not e.get("partial") for e in u5), str(kinds(EV5)))
final5 = [e for e in u5 if not e.get("partial")][-1]
fn = final5["payload"]["components"][0]["data"]["nodes"][0]
check("chunked final has 2 items", len(fn.get("children", [])) == 2, json.dumps(fn)[:200])

# 7. wrapper still works (no regression)
EV6 = run('<render_component><flow current="2"><flow-item title="A" text="x"/></flow></render_component>')
check("wrapper path still works", any(e.get("type") == "ui_block" and not e.get("partial") for e in EV6), str(kinds(EV6)))

# 8. an inline tag is NOT intercepted as a block
EV7 = run('Use <b>bold</b> and <code>inline</code> here.')
check("inline tags not rendered as blocks", not any(e.get("type") == "ui_block" for e in EV7), str(kinds(EV7)))

# 9. card/box/steps variants
for tag, body in [("steps", '<flow-item title="A" text="x"/>'), ("callout", 'hello'), ("stats", '<stat value="1" label="x"/>')]:
    e = run('<%s>%s</%s>' % (tag, body, tag))
    check("bare <%s> renders" % tag, any(x.get("type") == "ui_block" for x in e), str(kinds(e)))

# 10. bare <svg markup="…"/> — the drawing lives inside the attribute, so the
# outer tag is SELF-CLOSING and the inner `markup="<svg …"` nests a second
# `<svg` in the value. Both used to break: the tag was never intercepted (not
# in the bare set), and the incomplete-tag hold descended INTO the attribute
# value and mistook the inner <svg for the real block. Result: the drawing
# painted mid-stream, then vanished at the final render.
_INNER = ("<svg viewBox='0 0 300 300' xmlns='http://www.w3.org/2000/svg'>"
          "<circle cx='150' cy='150' r='132' fill='none'/>"
          "<text x='150' y='138' fill='#fff'>N</text></svg>")
_SVG = '<svg markup="' + _INNER + '"/>'

EV8 = run('Here:\n\n' + _SVG + '\n\ndone')
u8 = [e for e in EV8 if e.get("type") == "ui_block"]
check("bare svg emits ui_block", any(not e.get("partial") for e in u8), str(kinds(EV8)))
t8 = "".join(e.get("text", "") for e in EV8 if e.get("type") == "text")
check("bare svg didn't leak outer tag", "markup=" not in t8, repr(t8))
check("prose after svg preserved", "done" in t8, repr(t8))
if u8:
    _node = u8[-1]["payload"]["components"][0]["data"]["nodes"][0]
    check("svg node kind", _node.get("kind") == "svg", json.dumps(_node)[:150])
    check("svg markup preserved", ("<svg" in (_node.get("markup") or "")), json.dumps(_node)[:150])

# 11. bare svg split across CHUNKS must still seal once, not leak the half-tag.
p = SkillParser()
_parts = ['Here:\n\n<svg markup="<svg ', "viewBox='0 0 300 300' xmlns='http://www.w3.org/2000/svg'>",
          "<circle cx='150' cy='150' r='132'/></svg>", '"/>\n\nafter']
EV9 = []
for c in _parts:
    EV9 += list(p.feed(c))
EV9 += list(p.flush())
u9 = [e for e in EV9 if e.get("type") == "ui_block"]
check("chunked bare svg seals", any(not e.get("partial") for e in u9), str(kinds(EV9)))
t9 = "".join(e.get("text", "") for e in EV9 if e.get("type") == "text")
check("chunked bare svg no leak", "markup=" not in t9 and "<circle" not in t9, repr(t9))
check("chunked svg keeps trailing prose", "after" in t9, repr(t9))

# 12. a quoted <svg> inside a fence is not a block.
EV10 = run('```\n' + _SVG + '\n```')
check("fenced svg NOT rendered", not any(e.get("type") == "ui_block" for e in EV10), str(kinds(EV10)))

print()
print("FAILURES:", FAIL if FAIL else "none")
sys.exit(1 if FAIL else 0)
