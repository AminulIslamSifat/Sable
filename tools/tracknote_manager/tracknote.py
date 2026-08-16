#!/usr/bin/env python3
"""Unified CRUD for notes, schedules, agent_ops in sable.db"""
import argparse, json, sqlite3, sys, uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

def find_db():
    candidates = [
        Path(__file__).resolve().parent.parent.parent / "system" / "sable.db",
        Path.home() / "Projects" / "Sable" / "system" / "sable.db",
    ]
    for p in candidates:
        if p.exists():
            return str(p)
    print("ERROR: sable.db not found", file=sys.stderr); sys.exit(1)

DB = find_db()
TZ = timezone(timedelta(hours=6))

def conn():
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row; return c

def now_iso(): return datetime.now(TZ).isoformat()
def short_id(): return uuid.uuid4().hex[:12]

def calc_next_run(time_str, day, stype):
    if not time_str: return None
    now = datetime.now(TZ)
    parts = time_str.split(":")
    h, m = int(parts[0]), int(parts[1]) if len(parts) > 1 else 0
    candidate = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if stype == "weekly" and day is not None:
        ahead = day - now.weekday()
        if ahead < 0 or (ahead == 0 and candidate <= now): ahead += 7
        candidate += timedelta(days=ahead)
    elif candidate <= now:
        candidate += timedelta(days=1)
    return candidate.isoformat()

def find_row(c, table, id_prefix):
    return c.execute(f"SELECT * FROM {table} WHERE id LIKE ?", (id_prefix + "%",)).fetchone()

# ─── NOTES ───

def notes_list(args):
    c = conn()
    q = "SELECT * FROM notes WHERE archived=0"
    if args.type: q += f" AND note_type='{args.type}'"
    if args.all: q = "SELECT * FROM notes"
    q += " ORDER BY pinned DESC, created_at DESC"
    rows = c.execute(q).fetchall(); c.close()
    if not rows: print("No notes."); return
    for r in rows:
        pin = "📌 " if r["pinned"] else ""
        typ = "☑" if r["note_type"] == "checklist" else "📝"
        items = json.loads(r["items"]) if r["items"] else []
        done = sum(1 for i in items if i.get("done"))
        count = f" ({done}/{len(items)})" if items else ""
        print(f"{pin}{typ} {r['id'][:8]}  {r['title']}{count}")
        if r["content"]:
            print(f"   📄 {r['content']}")
        for i, item in enumerate(items):
            mark = "✓" if item.get("done") else "○"
            print(f"   {mark} [{i}] {item['text']}")

def notes_get(args):
    c = conn(); r = find_row(c, "notes", args.id); c.close()
    if not r: print(f"Not found: {args.id}"); return
    d = dict(r)
    if d.get("items"):
        d["items"] = json.loads(d["items"])
    print(json.dumps(d, indent=2, default=str))

def notes_add(args):
    c = conn(); nid = short_id(); now = now_iso()
    items = args.items if args.items else "[]"
    c.execute("""INSERT INTO notes (id,title,content,note_type,items,due_date,color,label,pinned,archived,created_at,updated_at)
                 VALUES (?,?,?,?,?,?,?,?,0,0,?,?)""",
              (nid, args.title, args.content or "", args.type, items, args.due, args.color, args.label, now, now))
    c.commit(); c.close()
    print(f"Added note: {nid}")

def notes_edit(args):
    c = conn(); r = find_row(c, "notes", args.id)
    if not r: print(f"Not found: {args.id}"); return
    updates = {}
    for f in ["title","content","type","items","due","color","label","pinned","archived"]:
        v = getattr(args, f, None)
        if v is not None:
            col = {"type":"note_type","due":"due_date"}.get(f, f)
            updates[col] = int(v) if f in ("pinned","archived") else v
    if not updates: print("Nothing to update."); return
    updates["updated_at"] = now_iso()
    c.execute(f"UPDATE notes SET {','.join(f'{k}=?' for k in updates)} WHERE id=?", (*updates.values(), r["id"]))
    c.commit(); c.close()
    print(f"Updated {r['id'][:8]}: {', '.join(updates)}")

def notes_toggle(args):
    c = conn(); r = find_row(c, "notes", args.id)
    if not r: print(f"Not found: {args.id}"); return
    items = json.loads(r["items"]) if r["items"] else []
    idx = int(args.index)
    if idx < 0 or idx >= len(items): print(f"Index {idx} out of range (0-{len(items)-1})"); return
    items[idx]["done"] = not items[idx].get("done", False)
    c.execute("UPDATE notes SET items=?, updated_at=? WHERE id=?", (json.dumps(items), now_iso(), r["id"]))
    c.commit(); c.close()
    state = "✓" if items[idx]["done"] else "○"
    print(f"{state} [{idx}] {items[idx]['text']}")

def notes_remove(args):
    c = conn(); r = find_row(c, "notes", args.id)
    if not r: print(f"Not found: {args.id}"); return
    c.execute("DELETE FROM notes WHERE id=?", (r["id"],)); c.commit(); c.close()
    print(f"Removed: {r['id'][:8]} ({r['title']})")

# ─── SCHEDULES ───

def sched_list(args):
    c = conn()
    q = "SELECT * FROM schedules" if args.all else "SELECT * FROM schedules WHERE completed=0"
    q += " ORDER BY created_at DESC"
    rows = c.execute(q).fetchall(); c.close()
    if not rows: print("No schedules."); return
    for r in rows:
        mark = "✓" if r["completed"] else "○"
        t = r["time"] or "—"
        print(f"[{mark}] {r['id'][:8]}  {r['title']}  ({r['schedule_type']} {t})")

def sched_get(args):
    c = conn(); r = find_row(c, "schedules", args.id); c.close()
    if not r: print(f"Not found: {args.id}"); return
    print(json.dumps(dict(r), indent=2, default=str))

def sched_add(args):
    c = conn(); sid = short_id(); now = now_iso()
    c.execute("""INSERT INTO schedules (id,title,schedule_type,time,day_of_week,start_date,end_date,description,completed,created_at,updated_at)
                 VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
              (sid, args.title, args.type, args.time, args.day, args.start, args.end, args.desc or "", 0, now, now))
    c.commit(); c.close()
    print(f"Added schedule: {sid}")

def sched_edit(args):
    c = conn(); r = find_row(c, "schedules", args.id)
    if not r: print(f"Not found: {args.id}"); return
    updates = {}
    for f in ["title","type","time","day","start","end","desc","completed"]:
        v = getattr(args, f, None)
        if v is not None:
            col = {"type":"schedule_type","day":"day_of_week","start":"start_date","end":"end_date","desc":"description"}.get(f, f)
            updates[col] = int(v) if f == "completed" else v
    if not updates: print("Nothing to update."); return
    updates["updated_at"] = now_iso()
    c.execute(f"UPDATE schedules SET {','.join(f'{k}=?' for k in updates)} WHERE id=?", (*updates.values(), r["id"]))
    c.commit(); c.close()
    print(f"Updated {r['id'][:8]}: {', '.join(updates)}")

def sched_toggle(args):
    c = conn(); r = find_row(c, "schedules", args.id)
    if not r: print(f"Not found: {args.id}"); return
    new = 0 if r["completed"] else 1
    c.execute("UPDATE schedules SET completed=?, updated_at=? WHERE id=?", (new, now_iso(), r["id"]))
    c.commit(); c.close()
    print(f"{r['title']} → {'completed' if new else 'active'}")

def sched_remove(args):
    c = conn(); r = find_row(c, "schedules", args.id)
    if not r: print(f"Not found: {args.id}"); return
    c.execute("DELETE FROM schedules WHERE id=?", (r["id"],)); c.commit(); c.close()
    print(f"Removed: {r['id'][:8]} ({r['title']})")

# ─── AGENT OPS ───

def ops_list(args):
    c = conn()
    q = "SELECT * FROM agent_ops" if args.all else "SELECT * FROM agent_ops WHERE enabled=1"
    q += " ORDER BY created_at DESC"
    rows = c.execute(q).fetchall(); c.close()
    if not rows: print("No ops."); return
    for r in rows:
        mark = "✓" if r["enabled"] else "✗"
        print(f"[{mark}] {r['id'][:8]}  {r['name']}  ({r['schedule_type']} {r['schedule_time'] or '—'})  next: {r['next_run'] or '—'}")

def ops_get(args):
    c = conn(); r = find_row(c, "agent_ops", args.id); c.close()
    if not r: print(f"Not found: {args.id}"); return
    print(json.dumps(dict(r), indent=2, default=str))

def ops_add(args):
    c = conn(); oid = short_id(); now = now_iso()
    nr = calc_next_run(args.time, args.day, args.type)
    c.execute("""INSERT INTO agent_ops (id,name,prompt,model,schedule_type,schedule_time,schedule_day,cron_expression,missed_run_policy,enabled,created_at,updated_at)
                 VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
              (oid, args.name, args.prompt, args.model, args.type, args.time, args.day, args.cron, args.policy, int(args.enabled), now, now))
    if nr:
        c.execute("UPDATE agent_ops SET next_run=? WHERE id=?", (nr, oid))
    c.commit(); c.close()
    print(f"Added op: {oid}" + (f"  next: {nr}" if nr else ""))

def ops_edit(args):
    c = conn(); r = find_row(c, "agent_ops", args.id)
    if not r: print(f"Not found: {args.id}"); return
    updates = {}
    for f in ["name","prompt","model","type","time","day","cron","policy","enabled"]:
        v = getattr(args, f, None)
        if v is not None:
            col = {"type":"schedule_type","time":"schedule_time","day":"schedule_day","cron":"cron_expression","policy":"missed_run_policy"}.get(f, f)
            updates[col] = int(v) if f == "enabled" else v
    if not updates: print("Nothing to update."); return
    if "schedule_time" in updates or "schedule_type" in updates:
        st = updates.get("schedule_time", r["schedule_time"])
        stype = updates.get("schedule_type", r["schedule_type"])
        sd = updates.get("schedule_day", r["schedule_day"])
        updates["next_run"] = calc_next_run(st, sd, stype)
    updates["updated_at"] = now_iso()
    c.execute(f"UPDATE agent_ops SET {','.join(f'{k}=?' for k in updates)} WHERE id=?", (*updates.values(), r["id"]))
    c.commit(); c.close()
    print(f"Updated {r['id'][:8]}: {', '.join(updates)}")

def ops_toggle(args):
    c = conn(); r = find_row(c, "agent_ops", args.id)
    if not r: print(f"Not found: {args.id}"); return
    new = 0 if r["enabled"] else 1
    c.execute("UPDATE agent_ops SET enabled=?, updated_at=? WHERE id=?", (new, now_iso(), r["id"]))
    c.commit(); c.close()
    print(f"{r['name']} → {'enabled' if new else 'disabled'}")

def ops_remove(args):
    c = conn(); r = find_row(c, "agent_ops", args.id)
    if not r: print(f"Not found: {args.id}"); return
    c.execute("DELETE FROM agent_ops WHERE id=?", (r["id"],)); c.commit(); c.close()
    print(f"Removed: {r['id'][:8]} ({r['name']})")

# ─── CLI ───

def main():
    p = argparse.ArgumentParser(description="TrackNote Manager")
    sub = p.add_subparsers(dest="section")

    # notes
    np = sub.add_parser("notes"); nsub = np.add_subparsers(dest="cmd")
    s = nsub.add_parser("list"); s.add_argument("--all", action="store_true"); s.add_argument("--type")
    s = nsub.add_parser("get"); s.add_argument("id")
    s = nsub.add_parser("add")
    s.add_argument("--title", required=True); s.add_argument("--content"); s.add_argument("--type", default="note")
    s.add_argument("--items"); s.add_argument("--due"); s.add_argument("--color"); s.add_argument("--label")
    s = nsub.add_parser("edit")
    s.add_argument("id"); s.add_argument("--title"); s.add_argument("--content"); s.add_argument("--type")
    s.add_argument("--items"); s.add_argument("--due"); s.add_argument("--color"); s.add_argument("--label")
    s.add_argument("--pinned", type=int); s.add_argument("--archived", type=int)
    s = nsub.add_parser("remove"); s.add_argument("id")
    s = nsub.add_parser("toggle"); s.add_argument("id"); s.add_argument("index")

    # schedules
    sp = sub.add_parser("schedules"); ssub = sp.add_subparsers(dest="cmd")
    s = ssub.add_parser("list"); s.add_argument("--all", action="store_true")
    s = ssub.add_parser("get"); s.add_argument("id")
    s = ssub.add_parser("add")
    s.add_argument("--title", required=True); s.add_argument("--type", default="daily")
    s.add_argument("--time"); s.add_argument("--day", type=int); s.add_argument("--start"); s.add_argument("--end"); s.add_argument("--desc")
    s = ssub.add_parser("edit")
    s.add_argument("id"); s.add_argument("--title"); s.add_argument("--type"); s.add_argument("--time")
    s.add_argument("--day", type=int); s.add_argument("--start"); s.add_argument("--end"); s.add_argument("--desc"); s.add_argument("--completed", type=int)
    s = ssub.add_parser("remove"); s.add_argument("id")
    s = ssub.add_parser("toggle"); s.add_argument("id")

    # ops
    op = sub.add_parser("ops"); osub = op.add_subparsers(dest="cmd")
    s = osub.add_parser("list"); s.add_argument("--all", action="store_true")
    s = osub.add_parser("get"); s.add_argument("id")
    s = osub.add_parser("add")
    s.add_argument("--name", required=True); s.add_argument("--prompt", required=True)
    s.add_argument("--model", default="qwen3.7-max"); s.add_argument("--type", default="daily")
    s.add_argument("--time"); s.add_argument("--day", type=int); s.add_argument("--cron"); s.add_argument("--policy", default="catch_up"); s.add_argument("--enabled", type=int, default=1)
    s = osub.add_parser("edit")
    s.add_argument("id"); s.add_argument("--name"); s.add_argument("--prompt"); s.add_argument("--model")
    s.add_argument("--type"); s.add_argument("--time"); s.add_argument("--day", type=int); s.add_argument("--cron"); s.add_argument("--policy"); s.add_argument("--enabled", type=int)
    s = osub.add_parser("remove"); s.add_argument("id")
    s = osub.add_parser("toggle"); s.add_argument("id")

    args = p.parse_args()
    if not args.section or not args.cmd:
        p.print_help(); return

    dispatch = {
        "notes": {"list": notes_list, "get": notes_get, "add": notes_add, "edit": notes_edit, "toggle": notes_toggle, "remove": notes_remove},
        "schedules": {"list": sched_list, "get": sched_get, "add": sched_add, "edit": sched_edit, "toggle": sched_toggle, "remove": sched_remove},
        "ops": {"list": ops_list, "get": ops_get, "add": ops_add, "edit": ops_edit, "toggle": ops_toggle, "remove": ops_remove},
    }
    dispatch[args.section][args.cmd](args)

if __name__ == "__main__":
    main()
#
