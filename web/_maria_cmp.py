import json, subprocess, sys, pathlib
sys.path.insert(0, '/home/sifat/hdd/projects/test/Sable-exp')
from engine.skills.handlers.dil_parser import parse

BLOCKS = {
 "1. SABLE": '''<box border radius="2xl" padding={4} gap={4}>
  {@body const [tab,setTab] = DIL.useState("Overview")}
  <row align="center" gap={3}>
    <box background="#6D28D9" radius="xl" size="48px" align="center" justify="center"><icon name="orbit" size="2xl" color="#FFFFFF" /></box>
    <box flex="1" gap={1}><title size="xl">SABLE</title><text color="secondary" size="sm">Agent Operations Center \u00b7 Prototype</text></box>
    <badge color="danger">PAUSED</badge>
  </row>
  <grid columns={3} gap={2}>
    <grid-item><box background="surface-secondary" radius="lg" padding={3} gap={1}><title size="xl">00</title><text color="secondary" size="xs">Agents active</text></box></grid-item>
    <grid-item><box background="surface-secondary" radius="lg" padding={3} gap={1}><title size="xl">1</title><text color="secondary" size="xs">Tasks done</text></box></grid-item>
    <grid-item><box background="surface-secondary" radius="lg" padding={3} gap={1}><title size="xl">\u2014</title><text color="secondary" size="xs">Health score</text></box></grid-item>
  </grid>
  <box border radius="lg" padding={3}><row align="center" gap={3}><box flex="1" gap={1}><text weight="600">Planner</text><text color="secondary" size="sm">Breaking down the goal</text></box><box background="surface-tertiary" radius="full" size="9px" /></row></box>
  <box border radius="lg" padding={3}><row align="center" gap={3}><box flex="1" gap={1}><text weight="600">Executor</text><text color="secondary" size="sm">Performing tool actions</text></box><box background="surface-tertiary" radius="full" size="9px" /></row></box>
  <box border radius="lg" padding={3}><row align="center" gap={3}><box flex="1" gap={1}><text weight="600">Verifier</text><text color="secondary" size="sm">Checking output quality</text></box><box background="surface-tertiary" radius="full" size="9px" /></row></box>
  <button block color="success">Resume system</button>
</box>''',
 "3. Feature cards": '''<grid columns={2} gap={3}>
  <grid-item><box border radius="2xl" padding={3} gap={2}><icon name="brain-circuit" size="2xl" color="#A78BFA" /><title size="md">AI Engine</title><text color="secondary" size="sm">Reasoning and tool orchestration.</text><badge color="success">Online</badge></box></grid-item>
  <grid-item><box border radius="2xl" padding={3} gap={2}><icon name="workflow" size="2xl" color="#60A5FA" /><title size="md">Workflows</title><text color="secondary" size="sm">Automate multi-step tasks.</text><badge color="info">12 flows</badge></box></grid-item>
  <grid-item><box border radius="2xl" padding={3} gap={2}><icon name="database" size="2xl" color="#34D399" /><title size="md">Memory</title><text color="secondary" size="sm">Retrieve useful context.</text><badge>Persistent</badge></box></grid-item>
  <grid-item><box border radius="2xl" padding={3} gap={2}><icon name="shield-check" size="2xl" color="#FBBF24" /><title size="md">Guardrails</title><text color="secondary" size="sm">Validate actions and inputs.</text><badge color="success">Protected</badge></box></grid-item>
</grid>''',
}

panels = []
for name, mk in BLOCKS.items():
    try:
        nodes, state = parse(mk)
        ok = "OK"
    except Exception as e:
        nodes, state = [], {}
        ok = f"CRASH: {e}"
    panels.append({"name": name, "nodes": nodes, "state": state, "status": ok})

html = '''<!doctype html><html data-mode="dark" data-theme="default"><head><meta charset="utf-8">
<link rel="stylesheet" href="css/variables.css">
<link rel="stylesheet" href="css/base.css">
<link rel="stylesheet" href="css/chat.css">
<link rel="stylesheet" href="css/components.css">
<link rel="stylesheet" href="css/render-components.css">
<script src="https://unpkg.com/lucide@latest/dist/umd/lucide.js"></script>
<style>body{margin:0;background:#0f0d14;padding:28px;font-family:sans-serif}.panel{margin-bottom:36px}.lbl{color:#b49aff;font-family:monospace;font-size:12px;margin-bottom:10px}.md-content{max-width:820px}</style>
</head><body>
<div id="root"></div>
<script src="js/render-components.js"></script>
<script>
const DATA = ''' + json.dumps(panels) + ''';
const root = document.getElementById("root");
DATA.forEach(p => {
  const panel = document.createElement("div"); panel.className = "panel";
  const lbl = document.createElement("div"); lbl.className = "lbl";
  lbl.textContent = p.name + "  [" + p.status + "]  nodes=" + (p.nodes?p.nodes.length:0);
  panel.appendChild(lbl);
  const c = document.createElement("div"); c.className = "md-content";
  panel.appendChild(c);
  try { window.SableComponents.render({components:[{type:"layout",data:{nodes:p.nodes,state:p.state}}]}, c); }
  catch(e){ c.textContent = "RENDER THREW: " + e.message; }
  root.appendChild(panel);
});
if (window.lucide) lucide.createIcons();
</script></body></html>'''
pathlib.Path('/home/sifat/hdd/projects/test/Sable-exp/web/_maria_cmp.html').write_text(html)
print("wrote harness")
for p in panels:
    print(p['name'], '->', p['status'], 'nodes:', len(p['nodes']))
