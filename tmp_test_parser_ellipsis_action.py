from engine.skills.parser import SkillParser

p = SkillParser()
events = []
events.extend(p.feed('<action>[...]</action>'))
events.extend(p.flush())
print(events)
assert not any(e.get('type') == 'parse_error' for e in events), events
print('OK: no parse_error for placeholder action')
