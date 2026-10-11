import json, pathlib, sys
sys.path.insert(0, '/home/sifat/hdd/projects/test/Sable-exp')
from engine.skills.handlers.dil_parser import parse

markup = pathlib.Path('/tmp/sable_block.dil').read_text()
nodes, state = parse(markup)
print("nodes:", len(nodes))

tpl = pathlib.Path('/home/sifat/hdd/projects/test/Sable-exp/web/_maria_tpl.html').read_text()
out = tpl.replace('/*NODES*/', json.dumps(nodes))
pathlib.Path('/home/sifat/hdd/projects/test/Sable-exp/web/_maria_ref.html').write_text(out)
print("wrote")