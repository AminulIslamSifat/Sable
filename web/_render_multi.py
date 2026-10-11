import json, pathlib, sys
sys.path.insert(0, '/home/sifat/hdd/projects/test/Sable-exp')
from engine.skills.handlers.dil_parser import parse
files = [('1. SABLE', '/tmp/block1.dil'), ('2. Chart', '/tmp/block2.dil'), ('3. Feature cards', '/tmp/block3.dil')]
panels = []
for name, path in files:
    mk = pathlib.Path(path).read_text()
    try:
        nodes, state = parse(mk); st = 'OK'
    except Exception as e:
        nodes, state, st = [], {}, 'CRASH: %s' % e
    panels.append({'name': name, 'nodes': nodes, 'state': state, 'status': st})
html = pathlib.Path('/home/sifat/hdd/projects/test/Sable-exp/web/_maria_tpl.html').read_text()
html = html.replace('/*NODES*/', json.dumps(panels))
pathlib.Path('/home/sifat/hdd/projects/test/Sable-exp/web/_maria_multi.html').write_text(html)
for p in panels: print(p['name'], p['status'], 'nodes:', len(p['nodes']))
