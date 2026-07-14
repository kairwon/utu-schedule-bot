"""
UTU 排课系统 — 后端
===================
手机/电脑浏览器打开 /page 直接操作
电脑 Excel 导入 → 自动同步 → 手机也能用
"""

import json, os, time
from datetime import datetime
from pathlib import Path

from flask import Flask, request, jsonify
from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv(override=True)

app = Flask(__name__)

@app.after_request
def add_cors_headers(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    resp.headers["Access-Control-Allow-Methods"] = "GET,POST,OPTIONS"
    return resp

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
anthropic = Anthropic(api_key=ANTHROPIC_API_KEY, base_url="https://api.anthropic.com") if ANTHROPIC_API_KEY else None

DATA_DIR      = Path(__file__).parent / "data"
SCHEDULE_FILE = DATA_DIR / "schedule.json"

TIME_SLOTS = ["09:30-11:30", "12:30-14:30", "14:30-16:30", "16:30-18:40"]
ROOM_LIST  = [f"Room {i}" for i in range(1, 31) if i not in (9, 11)]
ENGLISH_SUBS = {"vocabulary","1000","2000","4000","grammar","ielts","phonics","esl"}
VOCAB_LEVELS = {"1000","2000","4000"}

def load_json(path, default=None):
    if default is None: default = {}
    if path.exists():
        try: return json.loads(path.read_text(encoding="utf-8"))
        except: return default
    return default

def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

def get_schedule():
    return load_json(SCHEDULE_FILE, {"students": [], "teachers": [], "slots": {}, "attendance": {}, "excluded_teachers": {}})

def save_schedule(data):
    save_json(SCHEDULE_FILE, data)

# ──────────────────────── 排课算法 ────────────────────────

def is_match(teacher_sub: str, student_sub: str) -> bool:
    ts, ss = teacher_sub.strip().lower(), student_sub.strip().lower()
    if ts == ss: return True
    if ts == "vocabulary" and ss in VOCAB_LEVELS: return True
    if ss == "vocabulary" and ts in VOCAB_LEVELS: return True
    return False

def is_english_subject(subj: str) -> bool:
    return subj.strip().lower() in ENGLISH_SUBS

def get_group_key(subject: str, grade: int) -> str:
    s = subject.strip().lower()
    if s in VOCAB_LEVELS: return s
    if is_english_subject(s): return s
    return f"{s}_{grade}"

def schedule_slot(students, teachers, time_text, max_class_student=4, excluded_teachers=None):
    if excluded_teachers is None: excluded_teachers = set()
    teachers = [t for t in teachers if t['name'] not in excluded_teachers]
    room_idx = [0]
    def get_room(): r = ROOM_LIST[room_idx[0] % len(ROOM_LIST)]; room_idx[0] += 1; return r

    result = []
    all_students = []
    for s in students:
        prefer_tea = s.get("preferTea", "") or ""
        subs = [x.strip() for x in (s["Subject"]+"").split("/") if x.strip()]
        all_students.append({
            "name": s["Student"], "grade": int(s.get("Grade",0)), "subjects": subs,
            "stu_type": (s.get("Type","group") or "group").strip(), "preferTea": prefer_tea,
            "placed": False, "matchedTeacher": None, "matchedSubject": None,
        })

    lv_rank_desc = {"high":0, "mid":1, "low":2}
    lv_rank_asc  = {"high":0, "mid":1, "low":2}
    all_teachers = [{"name": t["name"], "subjects": t.get("subjects",[]),
                      "level": t.get("level","mid"), "busy": False} for t in teachers]
    all_teachers.sort(key=lambda t: (lv_rank_desc.get(t["level"],1), t["name"]))

    def get_candidates(stu):
        candidates = []
        for tea in all_teachers:
            for sub in stu["subjects"]:
                for ts in tea["subjects"]:
                    ts_sub = ts.get("subject",ts) if isinstance(ts,dict) else ts
                    max_grade = ts.get("maxGrade",99) if isinstance(ts,dict) else 99
                    if is_match(ts_sub, sub) and stu["grade"] <= max_grade:
                        candidates.append({"teacher": tea, "subject": sub}); break
        candidates.sort(key=lambda c: lv_rank_asc.get(c["teacher"]["level"],1))
        return candidates

    p1_stus = [s for s in all_students if s["stu_type"]=="1v1" and s["preferTea"]]
    p2_stus = [s for s in all_students if s["stu_type"]!="1v1" and s["preferTea"]]
    p3_stus = [s for s in all_students if s["stu_type"]=="1v1" and not s["preferTea"]]
    p4_stus = [s for s in all_students if s["stu_type"]!="1v1" and not s["preferTea"]]

    reserved_teachers = set()
    locked_teachers = set()
    match_map = {}

    # P1: 1v1 + 指定老师
    p1_by_tea = {}
    for stu in p1_stus:
        tea = next((t for t in all_teachers if t["name"]==stu["preferTea"]), None)
        if not tea: continue
        ok_sub = None
        for sub in stu["subjects"]:
            for ts in tea["subjects"]:
                ts_sub = ts.get("subject",ts) if isinstance(ts,dict) else ts
                max_grade = ts.get("maxGrade",99) if isinstance(ts,dict) else 99
                if is_match(ts_sub,sub) and stu["grade"]<=max_grade: ok_sub=sub; break
            if ok_sub: break
        if not ok_sub: continue
        key = tea["name"]
        if key not in p1_by_tea: p1_by_tea[key] = {"tea":tea, "pairs":[]}
        p1_by_tea[key]["pairs"].append((stu,ok_sub))
        reserved_teachers.add(tea["name"])
    for tea_name, entry in p1_by_tea.items():
        by_sub = {}
        for stu, sub in entry["pairs"]: by_sub.setdefault(sub,[]).append(stu)
        for sub, stus in by_sub.items():
            names = "、".join(s["name"] for s in stus)
            result.append({"时段":time_text,"教室":get_room(),"老师":tea_name,"科目":sub,
                "人数":len(stus),"type":"1v1","grade":stus[0]["grade"],"学生名单":names})
            for s in stus: s["placed"]=True; s["matchedTeacher"]=entry["tea"]; s["matchedSubject"]=sub
        entry["tea"]["busy"] = True

    # P2: 班课 + 指定老师
    p2_by_tea_sub = {}
    for stu in p2_stus:
        tea = next((t for t in all_teachers if t["name"]==stu["preferTea"]), None)
        if not tea: continue
        ok_sub = None
        for sub in stu["subjects"]:
            for ts in tea["subjects"]:
                ts_sub = ts.get("subject",ts) if isinstance(ts,dict) else ts
                max_grade = ts.get("maxGrade",99) if isinstance(ts,dict) else 99
                if is_match(ts_sub,sub) and stu["grade"]<=max_grade: ok_sub=sub; break
            if ok_sub: break
        if not ok_sub: continue
        key = f"{tea['name']}||{ok_sub}"
        if key not in p2_by_tea_sub: p2_by_tea_sub[key] = {"tea":tea,"sub":ok_sub,"stus":[]}
        p2_by_tea_sub[key]["stus"].append(stu)
        reserved_teachers.add(tea["name"])
    for entry in p2_by_tea_sub.values():
        tea, sub, stus = entry["tea"], entry["sub"], entry["stus"]
        tea["busy"] = True
        for i in range(0, len(stus), max_class_student):
            batch = stus[i:i+max_class_student]
            names = "、".join(s["name"] for s in batch)
            result.append({"时段":time_text,"教室":get_room(),"老师":tea["name"],"科目":sub,
                "人数":len(batch),"type":"group","grade":batch[0]["grade"],"学生名单":names})
            for s in batch: s["placed"]=True; s["matchedTeacher"]=tea; s["matchedSubject"]=sub

    # P3: 1v1 无指定 → 匈牙利
    def augment_1v1(stu, visited):
        for c in get_candidates(stu):
            tea = c["teacher"]
            if tea["name"] in visited or tea["name"] in reserved_teachers: continue
            visited.add(tea["name"])
            if tea["name"] not in match_map: match_map[tea["name"]] = (stu, c["subject"]); return True
            if augment_1v1(match_map[tea["name"]][0], visited): match_map[tea["name"]] = (stu, c["subject"]); return True
        return False
    p3_stus.sort(key=lambda s: len(get_candidates(s)))
    for stu in p3_stus: augment_1v1(stu, set())
    for tea_name, (stu, sub) in match_map.items():
        tea = next(t for t in all_teachers if t["name"]==tea_name)
        stu["matchedTeacher"]=tea; stu["matchedSubject"]=sub; tea["busy"]=True; locked_teachers.add(tea_name)
    for stu in p3_stus:
        if stu.get("matchedTeacher"):
            result.append({"时段":time_text,"教室":get_room(),"老师":stu["matchedTeacher"]["name"],
                "科目":stu["matchedSubject"],"人数":1,"type":"1v1","grade":stu["grade"],"学生名单":stu["name"]})
            stu["placed"]=True
    match_map.clear()

    # P4: 班课 无指定 → 匈牙利
    def augment(stu, visited):
        for c in get_candidates(stu):
            tea = c["teacher"]
            if tea["name"] in visited or tea["name"] in reserved_teachers or tea["name"] in locked_teachers or tea["busy"]: continue
            visited.add(tea["name"])
            if tea["name"] not in match_map or augment(match_map[tea["name"]][0], visited):
                match_map[tea["name"]] = (stu, c["subject"]); return True
        return False
    p4_stus.sort(key=lambda s: len(get_candidates(s)))
    for stu in p4_stus: augment(stu, set())
    for tea_name, (stu, sub) in match_map.items():
        tea = next(t for t in all_teachers if t["name"]==tea_name)
        stu["matchedTeacher"]=tea; stu["matchedSubject"]=sub; tea["busy"]=True
    for stu in p4_stus:
        if stu.get("matchedTeacher") and not stu["placed"]:
            result.append({"时段":time_text,"教室":get_room(),"老师":stu["matchedTeacher"]["name"],
                "科目":stu["matchedSubject"],"人数":1,"type":"group","grade":stu["grade"],"学生名单":stu["name"]})
            stu["placed"]=True

    # 未排班课 → 合班 → 找空闲老师 → 最后手段
    unmatched = [s for s in all_students if not s["placed"] and s["stu_type"]!="1v1"]
    for stu in unmatched:
        for sub in stu["subjects"]:
            key = get_group_key(sub, stu["grade"])
            open_classes = [c for c in result if c.get("type")!="1v1"
                and get_group_key(c["科目"],c["grade"])==key
                and len(c["学生名单"].split("、"))<max_class_student
                and stu["name"] not in c["学生名单"].split("、")]
            open_classes.sort(key=lambda c: c["人数"])
            if open_classes:
                cls = open_classes[0]
                names = cls["学生名单"].split("、")+[stu["name"]]
                cls["学生名单"]="、".join(names); cls["人数"]=len(names); stu["placed"]=True; break

    still_failed = [s for s in all_students if not s["placed"] and s["stu_type"]!="1v1"]
    for stu in still_failed:
        merged = False
        for sub in stu["subjects"]:
            key = get_group_key(sub, stu["grade"])
            open_classes = [c for c in result if c.get("type")!="1v1"
                and get_group_key(c["科目"],c["grade"])==key
                and len(c["学生名单"].split("、"))<max_class_student
                and stu["name"] not in c["学生名单"].split("、")]
            if open_classes:
                open_classes.sort(key=lambda c: c["人数"])
                cls=open_classes[0]
                names=cls["学生名单"].split("、")+[stu["name"]]
                cls["学生名单"]="、".join(names); cls["人数"]=len(names); stu["placed"]=True; merged=True; break
        if merged: continue
        for sub in stu["subjects"]:
            free_tea = next((t for t in all_teachers
                if not t["busy"] and t["name"] not in reserved_teachers and t["name"] not in locked_teachers
                and any(is_match(ts.get("subject",ts) if isinstance(ts,dict) else ts, sub)
                        and stu["grade"]<=(ts.get("maxGrade",99) if isinstance(ts,dict) else 99)
                        for ts in t["subjects"])), None)
            if free_tea:
                free_tea["busy"]=True
                result.append({"时段":time_text,"教室":get_room(),"老师":free_tea["name"],
                    "科目":sub,"人数":1,"type":"group","grade":stu["grade"],"学生名单":stu["name"]})
                stu["placed"]=True; break
        if not stu["placed"]:
            for sub in stu["subjects"]:
                is_eng = is_english_subject(sub)
                open_classes = [c for c in result if c.get("type")!="1v1"
                    and is_match(c["科目"],sub) and (is_eng or c["grade"]==stu["grade"])
                    and len(c["学生名单"].split("、"))<max_class_student
                    and stu["name"] not in c["学生名单"].split("、")]
                if open_classes:
                    open_classes.sort(key=lambda c: c["人数"])
                    cls=open_classes[0]
                    names=cls["学生名单"].split("、")+[stu["name"]]
                    cls["学生名单"]="、".join(names); cls["人数"]=len(names); stu["placed"]=True; break

    failed_1v1 = [s for s in all_students if not s["placed"] and s["stu_type"]=="1v1"]
    for stu in failed_1v1:
        for sub in stu["subjects"]:
            free_tea = next((t for t in all_teachers
                if not t["busy"] and t["name"] not in reserved_teachers and t["name"] not in locked_teachers
                and any(is_match(ts.get("subject",ts) if isinstance(ts,dict) else ts, sub)
                        and stu["grade"]<=(ts.get("maxGrade",99) if isinstance(ts,dict) else 99)
                        for ts in t["subjects"])), None)
            if free_tea:
                free_tea["busy"]=True
                result.append({"时段":time_text,"教室":get_room(),"老师":free_tea["name"],
                    "科目":sub,"人数":1,"type":"1v1","grade":stu["grade"],"学生名单":stu["name"]})
                stu["placed"]=True; break

    all_failed = [s for s in all_students if not s["placed"]]
    idle_teachers = [t["name"] for t in all_teachers if not t["busy"]]
    return {"result":result, "failCount":len(all_failed),
        "failNames":[f"{s['name']}（{'/'.join(s['subjects'])}，G{s['grade']}）" for s in all_failed],
        "idleTeachers":idle_teachers, "placedCount":sum(1 for s in all_students if s["placed"])}

# ──────────────────────── 操作执行 ────────────────────────

def format_student_list(students):
    return "\n".join(f"  {i+1}. {s['Student']} | G{s['Grade']} | {s['Subject']} | {s.get('Type','group')}" for i,s in enumerate(students))

def format_attendance(attendance, students, excluded):
    today = datetime.now().strftime("%Y-%m-%d")
    day_att = attendance.get(today, {})
    day_exc = excluded.get(today, [])
    lines = [f"📋 {today} 选课："]
    for slot in TIME_SLOTS:
        names = day_att.get(slot, [])
        lines.append(f"  [{slot}] {len(names)}人" + (f": {', '.join(names)}" if names else " (未选)"))
    if day_exc: lines.append(f"🚫 排除: {', '.join(day_exc)}")
    return "\n".join(lines)

def format_schedule_result(result_data, time_text):
    lines = [f"📋 [{time_text}] 排课结果："]
    for c in result_data["result"]:
        lines.append(f"  {c['教室']} | {c['老师']} | {c['科目']} | {c['学生名单']} | {c['type']}")
    if result_data["failNames"]: lines.append(f"\n⚠️ 未排入：{', '.join(result_data['failNames'])}")
    if result_data["idleTeachers"]: lines.append(f"📋 空闲老师：{', '.join(result_data['idleTeachers'])}")
    return "\n".join(lines)

def _parse_names(text, students):
    known = {s['Student']: s['Student'] for s in students}
    known_lower = {k.lower(): k for k in known}
    parts = text.replace("，",",").replace("、",",").replace("\n",",").split(",")
    parts = [p.strip() for p in parts if p.strip()]
    result = []
    for p in parts:
        try:
            idx = int(p)-1
            if 0 <= idx < len(students): result.append(students[idx]['Student']); continue
        except: pass
        if p in known: result.append(p); continue
        match = known_lower.get(p.lower())
        if match: result.append(match); continue
        matches = [v for k,v in known_lower.items() if p.lower() in k]
        if len(matches)==1: result.append(matches[0])
    seen = set()
    return [x for x in result if not (x in seen or seen.add(x))]

def execute_operation(schedule_data, op):
    action = op.get("action","")
    slots = schedule_data.setdefault("slots",{})
    students = schedule_data.get("students",[])
    teachers = schedule_data.get("teachers",[])
    attendance = schedule_data.setdefault("attendance",{})
    excluded = schedule_data.setdefault("excluded_teachers",{})
    today = datetime.now().strftime("%Y-%m-%d")

    if action == "list_students":
        return f"📋 学生（{len(students)}人）：\n\n"+format_student_list(students) if students else "📭 暂无学生", schedule_data

    if action == "show_attendance":
        return format_attendance(attendance, students, excluded), schedule_data

    if action == "select_students":
        slot_time = op.get("time","")
        names_str = op.get("student_names","")
        date = op.get("date",today)
        if slot_time not in TIME_SLOTS: return f"⚠️ 时段可选: {' / '.join(TIME_SLOTS)}", schedule_data
        if names_str in ("全部","所有人","all","所有"):
            selected = [s['Student'] for s in students]
        else:
            selected = _parse_names(names_str, students)
        if not selected: return "⚠️ 未识别到学生", schedule_data
        attendance.setdefault(date,{})[slot_time] = selected
        return f"✅ [{slot_time}] {len(selected)}人: {', '.join(selected)}\n\n"+format_attendance(attendance,students,excluded), schedule_data

    if action == "clear_slot":
        slot_time = op.get("time","")
        date = op.get("date",today)
        if date in attendance and slot_time in attendance[date]:
            del attendance[date][slot_time]
            return f"✅ 已清空 [{slot_time}]\n\n"+format_attendance(attendance,students,excluded), schedule_data
        return f"⚠️ [{slot_time}] 无学生", schedule_data

    if action == "exclude_teacher":
        name = op.get("teacher_name","").strip()
        date = op.get("date",today)
        if not name: return "⚠️ 请提供老师名", schedule_data
        exc = excluded.setdefault(date,[])
        if name not in exc: exc.append(name); return f"✅ 已排除 {name}\n\n"+format_attendance(attendance,students,excluded), schedule_data
        return f"⚠️ {name} 已排除", schedule_data

    if action == "include_teacher":
        name = op.get("teacher_name","").strip()
        date = op.get("date",today)
        exc = excluded.get(date,[])
        if name in exc: exc.remove(name); return f"✅ 已恢复 {name}\n\n"+format_attendance(attendance,students,excluded), schedule_data
        return f"⚠️ {name} 未排除", schedule_data

    if action == "auto_schedule":
        time_slot = op.get("time","")
        date = op.get("date",today)
        subject_filter = op.get("subject","")
        if time_slot and time_slot not in TIME_SLOTS: return f"⚠️ 时段: {' / '.join(TIME_SLOTS)}", schedule_data
        day_att = attendance.get(date,{})
        day_exc = excluded.get(date,[])
        slots_to_do = [time_slot] if time_slot else [s for s in TIME_SLOTS if day_att.get(s)]
        if not any(day_att.get(s) for s in slots_to_do): return "⚠️ 还没选学生", schedule_data
        if not teachers: return "⚠️ 无老师", schedule_data
        all_results = []
        for slot in slots_to_do:
            names = day_att.get(slot,[])
            if not names: continue
            slot_students = [s for s in students if s['Student'] in names]
            if subject_filter:
                slot_students = [s for s in slot_students if subject_filter.lower() in [x.strip().lower() for x in (s.get("Subject","")+"").split("/")]]
            if not slot_students: all_results.append(f"[{slot}] 无符合条件的学生"); continue
            r = schedule_slot(slot_students, teachers, slot, excluded_teachers=set(day_exc))
            slots.setdefault(date,{})[slot] = {"classes":r["result"],"failCount":r["failCount"],"idleTeachers":r["idleTeachers"],"placedCount":r["placedCount"]}
            all_results.append(format_schedule_result(r, slot))
        return f"🚀 {date} 排课完成！\n\n"+"\n\n".join(all_results), schedule_data

    if action == "add_student":
        name = op.get("student_name","").strip()
        grade = int(op.get("grade",0))
        subject = op.get("subject","").strip()
        stu_type = (op.get("student_type","group") or "group").strip()
        if not name or not grade or not subject: return "⚠️ 格式：姓名, G5, 数学/物理, 1v1或group", schedule_data
        if any(s['Student']==name for s in students): return f"⚠️ {name} 已存在", schedule_data
        students.append({"Student":name,"Grade":int(grade),"Subject":subject,"Type":stu_type if stu_type in ("1v1","group") else "group"})
        return f"✅ 已添加: {name} | G{grade} | {subject} | {stu_type}", schedule_data

    if action == "remove_student":
        name = op.get("student_name","").strip()
        for i,s in enumerate(students):
            if s['Student']==name:
                del students[i]
                for d in attendance:
                    for slot in attendance[d]:
                        if name in attendance[d][slot]: attendance[d][slot].remove(name)
                for d in slots:
                    for t in list(slots[d].keys()):
                        for c in list(slots[d][t].get("classes",[])):
                            names_in = [x.strip() for x in c.get("学生名单","").split("、")]
                            if name in names_in:
                                new_names = [x for x in names_in if x!=name]
                                if new_names: c["学生名单"]="、".join(new_names)
                                else: slots[d][t]["classes"].remove(c)
                        if not slots[d][t].get("classes"): del slots[d][t]
                    if not slots[d]: del slots[d]
                return f"✅ 已删除 {name}", schedule_data
        return f"⚠️ 未找到 {name}", schedule_data

    if action == "query_student":
        name = op.get("student_name","")
        found = []
        for d,dd in slots.items():
            for tk,slot in dd.items():
                for c in slot.get("classes",[]):
                    if name in [s.strip() for s in c.get("学生名单","").split("、")]:
                        found.append({**c,"date":d,"time":tk})
        if not found: return f"📭 {name} 无排课", schedule_data
        lines = [f"📋 {name}:"]
        for f in found: lines.append(f"  {f['date']} {f['time']} | {f.get('科目','?')} | {f.get('老师','?')} | {f.get('教室','?')}")
        return "\n".join(lines), schedule_data

    if action == "query_day":
        date = op.get("date",today)
        if date not in slots:
            if date in attendance: return format_attendance(attendance,students,excluded), schedule_data
            return f"📭 {date} 无排课", schedule_data
        lines = [f"📋 {date}:"]
        for tk in sorted(slots[date].keys()):
            for c in slots[date][tk].get("classes",[]):
                lines.append(f"  [{tk}] {c.get('科目','?')} | {c.get('老师','?')} | {c.get('学生名单','?')} | {c.get('教室','?')}")
        return "\n".join(lines), schedule_data

    if action == "remove_class":
        student, date, time_key, subject = op.get("student_name",""), op.get("date",today), op.get("time",""), op.get("subject","")
        if date in slots and time_key in slots[date]:
            kept = []
            for c in slots[date][time_key].get("classes",[]):
                names = [s.strip() for s in c.get("学生名单","").split("、")]
                if student in names:
                    if subject and c.get("科目")!=subject: kept.append(c); continue
                    new_names = [s for s in names if s!=student]
                    if new_names: c["学生名单"]="、".join(new_names); kept.append(c)
                else: kept.append(c)
            slots[date][time_key]["classes"] = kept
            if not kept:
                del slots[date][time_key]
                if not slots[date]: del slots[date]
            return f"✅ 已取消 {student} {date} {time_key} {subject or '课'}", schedule_data
        return "⚠️ 未找到", schedule_data

    if action == "add_class":
        student, date, time_key, subject, teacher = op.get("student_name",""), op.get("date",today), op.get("time",""), op.get("subject",""), op.get("teacher_name","")
        if not all([student,date,time_key,subject]): return "⚠️ 信息不足", schedule_data
        slots.setdefault(date,{}).setdefault(time_key,{"classes":[]})
        for c in slots[date][time_key]["classes"]:
            if student in [s.strip() for s in c.get("学生名单","").split("、")]: return f"⚠️ {student} 已有课", schedule_data
        used = {c.get("教室") for c in slots[date][time_key]["classes"]}
        room = next((f"Room {i}" for i in range(1,31) if f"Room {i}" not in used),"Room 1")
        slots[date][time_key]["classes"].append({"学生名单":student,"老师":teacher or "待分配","科目":subject,"教室":room,"时段":time_key})
        return f"✅ {student} | {subject} | {date} {time_key} | {teacher or '待分配'} | {room}", schedule_data

    if action == "chat":
        return op.get("message","好的"), schedule_data

    return f"⚠️ 不支持: {action}", schedule_data

# ──────────────────────── AI ────────────────────────

SYSTEM_PROMPT = """你是 UTU 排课系统 AI 助手。学生/老师名多用英文。

四个时段: 09:30-11:30 / 12:30-14:30 / 14:30-16:30 / 16:30-18:40

操作:
- 选学生: "9:30-11:30: David, Yufei" (select_students)
- 全部: "9:30-11:30: 全部"
- 自动排课: "自动排课" or "排课" (auto_schedule)
- 排某时段: "排 9:30-11:30"
- 查看考勤: "查看考勤" (show_attendance)
- 排除老师: "Tere 今天不排" (exclude_teacher)
- 恢复老师: "让 Tere 回来" (include_teacher)
- 添加学生: "添加学生: Zhang, G5, Math, 1v1" (add_student)
- 删除学生: "删除 Zhang" (remove_student)
- 查看课表: "查看课表" or "今天课表" (query_day)
- 学生列表: "有哪些学生" (list_students)
- 取消课程: "取消 David 今天的课" (remove_class)"""

TOOL_SCHEMA = {
    "name": "schedule_operation",
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["list_students","show_attendance","select_students","clear_slot","exclude_teacher","include_teacher","auto_schedule","add_student","remove_student","query_student","query_teacher","query_day","add_class","remove_class","move_class","chat"]},
            "student_name": {"type": "string"}, "student_names": {"type": "string"}, "teacher_name": {"type": "string"},
            "subject": {"type": "string"}, "student_type": {"type": "string"}, "grade": {"type": "integer"},
            "date": {"type": "string"}, "time": {"type": "string"},
            "from_date": {"type": "string"}, "from_time": {"type": "string"}, "to_date": {"type": "string"}, "to_time": {"type": "string"},
            "message": {"type": "string"},
        }, "required": ["action"]
    }
}

def parse_command(user_msg, schedule_data):
    if not anthropic: return {"action":"chat","message":"🤖 未配置 AI"}

    students = schedule_data.get("students",[])
    teachers = schedule_data.get("teachers",[])
    slots = schedule_data.get("slots",{})
    attendance = schedule_data.get("attendance",{})
    today = datetime.now().strftime("%Y-%m-%d")

    ctx = [f"日期: {today}"]
    ctx.append(f"学生 {len(students)} 人: " + ", ".join(s['Student'] for s in students[:20]))
    if len(students) > 20: ctx.append(f"...共{len(students)}人")
    ctx.append(f"老师 {len(teachers)} 人: " + ", ".join(t['name'] for t in teachers[:10]))
    day_att = attendance.get(today,{})
    for slot in TIME_SLOTS:
        names = day_att.get(slot,[])
        ctx.append(f"[{slot}] {'、'.join(names) if names else '(未选)'}")
    ctx.append(f"已排课: {len(slots)} 天")

    try:
        resp = anthropic.messages.create(
            model="claude-sonnet-5", max_tokens=1024, system=SYSTEM_PROMPT,
            messages=[{"role":"user","content":"\n".join(ctx)+"\n\n用户: "+user_msg}],
            tools=[TOOL_SCHEMA])
        for block in resp.content:
            if block.type == "tool_use": return block.input
        text = "".join(block.text for block in resp.content if block.type=="text")
        return {"action":"chat","message":text.strip() if text else "好的"}
    except Exception as e:
        return {"action":"chat","message":f"⚠️ AI 出错: {str(e)[:200]}"}

# ──────────────────────── 路由 ────────────────────────

@app.route("/page")
def serve_page():
    return app.send_static_file("schedule.html")

@app.route("/api/schedule", methods=["GET"])
def api_get():
    return jsonify(get_schedule())

@app.route("/api/schedule", methods=["POST"])
def api_save():
    data = request.get_json(force=True)
    existing = get_schedule()
    data["attendance"] = existing.get("attendance",{})
    data["excluded_teachers"] = existing.get("excluded_teachers",{})
    save_schedule(data)
    return jsonify({"ok":True})

@app.route("/api/command", methods=["POST"])
def api_command():
    body = request.get_json(force=True)
    msg = body.get("message","").strip()
    if not msg: return jsonify({"error":"empty"}),400
    sd = get_schedule()
    op = parse_command(msg, sd)
    reply, updated = execute_operation(sd, op)
    save_schedule(updated)
    return jsonify({"reply":reply, "operation":op, "schedule":updated})

@app.route("/", methods=["GET"])
def health():
    sd = get_schedule()
    return jsonify({"status":"ok","students":len(sd.get("students",[])),"teachers":len(sd.get("teachers",[])),"slots":len(sd.get("slots",{}))})

if __name__ == "__main__":
    port = int(os.environ.get("PORT",8080))
    app.run(host="0.0.0.0", port=port, debug=False)
