import sys
sys.path.insert(0, "/home/sifat/hdd/projects/test/Sable-exp")
from engine.skills.parser import SkillParser

html = (
    "<!doctype html><html><body><h1>hi</h1>"
    "<canvas id=c></canvas>"
    "<script>console.log(1)</" + "script></body></html>"
)
full = '<render_html title="Test Sim">' + html + "</render_html>"

p = SkillParser()
events = []
for i in range(0, len(full), 60):
    events.extend(list(p.feed(full[i : i + 60])))

print("total events:", len(events))
for e in events:
    print(" ", e.get("type"), {k: v for k, v in e.items() if k != "text"},
          ("text_len=%d" % len(e.get("text", ""))) if "text" in e else "")
