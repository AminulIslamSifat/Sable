import sys
sys.path.insert(0, "/home/sifat/hdd/projects/test/Sable-exp")
from engine.skills.parser import SkillParser

# 1. big render_html body -> expect MULTIPLE html_chunk deltas
big = "<div>" + ("x" * 2000) + "</div>"
full = '<render_html title="Big">' + big + "</render_html>"

p = SkillParser()
ev = []
for i in range(0, len(full), 40):
    ev.extend(list(p.feed(full[i : i + 40])))

kinds = [e.get("type") for e in ev]
chunks = [e for e in ev if e.get("type") == "html_chunk"]
recon = "".join(e["text"] for e in chunks)
print("BIG html -> chunks:", len(chunks),
      "| start:", kinds.count("html_start"),
      "| end:", kinds.count("html_end"))
print("  body reconstructed exactly:", recon == big)
print("  delta total chars:", len(recon), "expected:", len(big))

# 2. render_component regression
comp = '<render_component><card title="Hi"><text>hello</text></card></render_component>'
p2 = SkillParser()
ev2 = []
for i in range(0, len(comp), 30):
    ev2.extend(list(p2.feed(comp[i : i + 30])))
uib = [e for e in ev2 if e.get("type") == "ui_block"]
print("COMPONENT -> ui_block events:", len(uib),
      "| final payload present:", any(e.get("payload") for e in uib))

# 3. xss probe passthrough (JS sanitizer neutralises; parser must not mangle)
xss = '<render_html><svg /onload=alert(1)></svg></render_html>'
p3 = SkillParser()
ev3 = []
for i in range(0, len(xss), 25):
    ev3.extend(list(p3.feed(xss[i : i + 25])))
body = "".join(e.get("text", "") for e in ev3 if e.get("type") == "html_chunk")
print("XSS passthrough intact:", "/onload=alert(1)" in body)
