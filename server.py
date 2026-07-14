"""
UTU 排课系统 — AI 指令后端
===========================
电脑网页端：不变，照常用 Excel 导入学生/老师，数据同步到服务器
飞书机器人：手机上选学生、分配时段、增删学生、查询排课

四个时段：
  ① 09:30-11:30
  ② 12:30-14:30
  ③ 14:30-16:30
  ④ 16:30-18:40

环境变量：
  FEISHU_APP_ID / FEISHU_APP_SECRET / ANTHROPIC_API_KEY
"""

import json, os, time
from datetime import datetime
from pathlib import Path

import requests
from flask import Flask, request, jsonify
from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv(override=True)

# ──────────────────────────── 配置 ────────────────────────────
app = Flask(__name__)

@app.after_request
def add_cors_headers(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    resp.headers["Access-Control-Allow-Methods"] = "GET,POST,OPTIONS"
    return resp

@app.route("/api/<path:path>", methods=["OPTIONS"])
def handle_options(path=""):
    return "", 204

DATA_DIR      = Path(__file__).parent / "data"
SCHEDULE_FILE = DATA_DIR / "schedule.json"

FEISHU_APP_ID     = os.environ.get("FEISHU_APP_ID", "")
FEISHU_APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

anthropic = Anthropic(
    api_key=ANTHROPIC_API_KEY,
    base_url="https://api.anthropic.com"
) if ANTHROPIC_API_KEY else None

FEISHU_HOST = "https://open.feishu.cn"

# 四个固定时段
TIME_SLOTS = [
    "09:30-11:30",
    "12:30-14:30",
    "14:30-16:30",
    "16:30-18:40",
]

# ──────────────────────── 数据读写 ────────────────────────
def load_json(path: Path, default=None):
    if default is None: default = {}
    if path.exists():
        try: return json.loads(path.read_text(encoding="utf-8"))
        except: return default
    return default

def save_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

def get_schedule():
    return load_json(SCHEDULE_FILE, {"students": [], "teachers": [], "slots": {}, "attendance": {}})

def save_schedule(data):
    save_json(SCHEDULE_FILE, data)

# ──────────────────────── 飞书 ────────────────────────
_tenant_token = {"token": "", "expires_at": 0}

def get_tenant_token():
    global _tenant_token
    if time.time() < _tenant_token["expires_at"] - 60:
        return _tenant_token["token"]
    resp = requests.post(
        f"{FEISHU_HOST}/open-apis/auth/v3/tenant_access_token/internal",
        json={"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET}, timeout=10)
    data = resp.json()
    if data.get("code") != 0:
        raise Exception(f"飞书 token 失败: {data}")
    _tenant_token["token"] = data["tenant_access_token"]
    _tenant_token["expires_at"] = time.time() + data.get("expire", 3600)
    return _tenant_token["token"]

def send_feishu_msg(open_id: str, text: str):
    token = get_tenant_token()
    resp = requests.post(
        f"{FEISHU_HOST}/open-apis/im/v1/messages?receive_id_type=open_id",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"receive_id": open_id, "msg_type": "text", "content": json.dumps({"text": text})},
        timeout=10)
    result = resp.json()
    if result.get("code") != 0:
        print(f"[飞书] 发送失败: {result}")
    return result

# ──────────────────────── 学生/考勤格式化 ────────────────────────
def format_student_list(students: list) -> str:
    """把学生列表格式化成可读消息"""
    lines = []
    for i, s in enumerate(students):
        lines.append(f"  {i+1}. {s['Student']} | G{s['Grade']} | {s['Subject']} | {s.get('Type','group')}")
    return "\n".join(lines)

def format_attendance(attendance: dict, students: list) -> str:
    """格式化今天的考勤状态"""
    today = datetime.now().strftime("%Y-%m-%d")
    day_att = attendance.get(today, {})

    name_map = {s['Student']: s for s in students}

    lines = [f"📋 {today} 学生考勤："]
    for slot in TIME_SLOTS:
        names = day_att.get(slot, [])
        if names:
            details = []
            for n in names:
                s = name_map.get(n, {})
                details.append(f"{n}({s.get('Subject','?')})")
            lines.append(f"  [{slot}] {' / '.join(details)}")
        else:
            lines.append(f"  [{slot}] （未选）")
    return "\n".join(lines)

# ──────────────────────── 操作执行 ────────────────────────
def execute_operation(schedule_data: dict, op: dict) -> tuple[str, dict]:
    action = op.get("action", "")
    slots = schedule_data.setdefault("slots", {})
    students = schedule_data.get("students", [])
    attendance = schedule_data.setdefault("attendance", {})
    today = datetime.now().strftime("%Y-%m-%d")

    # ── list_students: 列出所有学生 ──
    if action == "list_students":
        if not students:
            return "📭 还没有学生数据。请在电脑网页导入 Excel。", schedule_data
        return f"📋 学生列表（共 {len(students)} 人）：\n\n" + format_student_list(students), schedule_data

    # ── show_attendance: 显示当天考勤 ──
    if action == "show_attendance":
        return format_attendance(attendance, students), schedule_data

    # ── select_students: 为一个时段选学生 ──
    if action == "select_students":
        slot_time = op.get("time", "")
        student_names_str = op.get("student_names", "")
        date = op.get("date", today)

        if slot_time not in TIME_SLOTS:
            return f"⚠️ 无效时段。可选：{' / '.join(TIME_SLOTS)}", schedule_data

        # 解析学生名（支持编号、逗号分隔、中文顿号分隔）
        selected = _parse_student_names(student_names_str, students)

        if not selected:
            return "⚠️ 没有识别到有效学生名。请重新输入，如：David, Yufei, Eva", schedule_data

        attendance.setdefault(date, {})[slot_time] = selected
        return f"✅ {date} [{slot_time}] 已选 {len(selected)} 人：{', '.join(selected)}\n\n" + format_attendance(attendance, students), schedule_data

    # ── clear_slot: 清空某时段 ──
    if action == "clear_slot":
        slot_time = op.get("time", "")
        date = op.get("date", today)
        if date in attendance and slot_time in attendance[date]:
            del attendance[date][slot_time]
            return f"✅ 已清空 {date} [{slot_time}]\n\n" + format_attendance(attendance, students), schedule_data
        return f"⚠️ {date} [{slot_time}] 本来就没有学生", schedule_data

    # ── add_student: 添加新学生 ──
    if action == "add_student":
        name = op.get("student_name", "").strip()
        grade = op.get("grade", 0)
        subject = op.get("subject", "").strip()
        stu_type = op.get("student_type", "group").strip()

        if not name:
            return "⚠️ 请提供学生姓名。格式：添加学生：姓名, G年级, 科目/科目, 1v1或group", schedule_data
        if not grade:
            return "⚠️ 请提供年级（数字）。格式：添加学生：姓名, G5, 数学, group", schedule_data
        if not subject:
            return "⚠️ 请提供科目。格式：添加学生：姓名, G5, 数学/物理, group", schedule_data

        # 查重
        if any(s['Student'] == name for s in students):
            return f"⚠️ 学生「{name}」已存在", schedule_data

        students.append({
            "Student": name,
            "Grade": int(grade),
            "Subject": subject,
            "Type": stu_type if stu_type in ("1v1", "group") else "group"
        })
        return f"✅ 已添加学生：{name} | G{grade} | {subject} | {stu_type}", schedule_data

    # ── remove_student: 删除学生 ──
    if action == "remove_student":
        name = op.get("student_name", "").strip()
        for i, s in enumerate(students):
            if s['Student'] == name:
                del students[i]
                # 同时从考勤中移除
                for d in attendance:
                    for slot in attendance[d]:
                        if name in attendance[d][slot]:
                            attendance[d][slot].remove(name)
                # 从排课中移除
                for d in slots:
                    for t in list(slots[d].keys()):
                        for c in list(slots[d][t].get("classes", [])):
                            names_in = [x.strip() for x in c.get("学生名单", "").split("、")]
                            if name in names_in:
                                new_names = [x for x in names_in if x != name]
                                if new_names:
                                    c["学生名单"] = "、".join(new_names)
                                else:
                                    slots[d][t]["classes"].remove(c)
                        if not slots[d][t].get("classes"):
                            del slots[d][t]
                    if not slots[d]:
                        del slots[d]
                return f"✅ 已删除学生「{name}」（同时从考勤和排课中移除）", schedule_data
        return f"⚠️ 未找到学生「{name}」", schedule_data

    # ── 保留原有操作 ──
    if action == "query_student":
        name = op.get("student_name", "")
        found = _find_student_slots(schedule_data, name)
        if not found:
            return f"📭 {name} 没有任何排课记录", schedule_data
        lines = [f"📋 {name} 的课表："]
        for f in found:
            lines.append(f"  • {f['date']} {f['time']} | {f.get('科目','?')} | {f.get('老师','?')} | {f.get('教室','?')}")
        return "\n".join(lines), schedule_data

    if action == "query_teacher":
        name = op.get("teacher_name", "")
        found = []
        for d, day_data in slots.items():
            for tk, slot in day_data.items():
                for c in slot.get("classes", []):
                    if c.get("老师") == name:
                        found.append({**c, "date": d, "time": tk})
        if not found:
            return f"📭 {name} 没有排课", schedule_data
        lines = [f"📋 {name} 的课表："]
        for f in found:
            lines.append(f"  • {f['date']} {f['time']} | {f.get('科目','?')} | {f.get('学生名单','?')} | {f.get('教室','?')}")
        return "\n".join(lines), schedule_data

    if action == "query_day":
        date = op.get("date", today)
        if date not in slots:
            # 如果没排课但有考勤，显示考勤
            if date in attendance:
                return format_attendance(attendance, students), schedule_data
            return f"📭 {date} 没有排课记录", schedule_data
        day = slots[date]
        lines = [f"📋 {date} 课表："]
        for tk in sorted(day.keys()):
            for c in day[tk].get("classes", []):
                lines.append(f"  • [{tk}] {c.get('科目','?')} | {c.get('老师','?')} | {c.get('学生名单','?')} | {c.get('教室','?')}")
        return "\n".join(lines), schedule_data

    if action == "remove_class":
        student = op.get("student_name", "")
        date = op.get("date", today)
        time_key = op.get("time", "")
        subject = op.get("subject", "")
        if date in slots and time_key in slots[date]:
            classes = slots[date][time_key].get("classes", [])
            kept, removed = [], []
            for c in classes:
                names = [s.strip() for s in c.get("学生名单", "").split("、")]
                if student in names:
                    if subject and c.get("科目") != subject:
                        kept.append(c); continue
                    new_names = [s for s in names if s != student]
                    if new_names:
                        c["学生名单"] = "、".join(new_names)
                        kept.append(c)
                    removed.append(c)
                else:
                    kept.append(c)
            slots[date][time_key]["classes"] = kept
            if not kept:
                del slots[date][time_key]
                if not slots[date]: del slots[date]
            if removed:
                return f"✅ 已取消 {student} 在 {date} {time_key} 的{subject or '课'}", schedule_data
        return f"⚠️ 未找到匹配的课", schedule_data

    if action == "add_class":
        student = op.get("student_name", "")
        date = op.get("date", today)
        time_key = op.get("time", "")
        subject = op.get("subject", "")
        teacher = op.get("teacher_name", "")
        if not all([student, date, time_key, subject]):
            return "⚠️ 信息不足：学生名、日期、时段、科目", schedule_data
        slots.setdefault(date, {}).setdefault(time_key, {"classes": []})
        for c in slots[date][time_key]["classes"]:
            if student in [s.strip() for s in c.get("学生名单", "").split("、")]:
                return f"⚠️ {student} 在 {date} {time_key} 已有课", schedule_data
        used_rooms = {c.get("教室") for c in slots[date][time_key]["classes"]}
        room = next((f"Room {i}" for i in range(1, 31) if f"Room {i}" not in used_rooms), "Room 1")
        slots[date][time_key]["classes"].append({
            "学生名单": student, "老师": teacher or "待分配",
            "科目": subject, "教室": room, "时段": time_key,
        })
        return f"✅ 已添加：{student} | {subject} | {date} {time_key} | {teacher or '待分配'} | {room}", schedule_data

    if action == "move_class":
        student = op.get("student_name", "")
        from_date = op.get("from_date", "")
        from_time = op.get("from_time", "")
        to_date = op.get("to_date", from_date)
        to_time = op.get("to_time", "")
        if not all([student, from_date, from_time, to_time]):
            return "⚠️ 信息不足", schedule_data
        found = None
        if from_date in slots and from_time in slots[from_date]:
            for c in slots[from_date][from_time].get("classes", []):
                if student in [s.strip() for s in c.get("学生名单", "").split("、")]:
                    found = c; break
        if not found:
            return f"⚠️ 未找到 {student} 在 {from_date} {from_time} 的课", schedule_data
        old_names = [s.strip() for s in found.get("学生名单", "").split("、")]
        new_old = [s for s in old_names if s != student]
        if new_old:
            found["学生名单"] = "、".join(new_old)
        else:
            slots[from_date][from_time]["classes"].remove(found)
            if not slots[from_date][from_time]["classes"]:
                del slots[from_date][from_time]
                if not slots[from_date]: del slots[from_date]
        slots.setdefault(to_date, {}).setdefault(to_time, {"classes": []})
        used = {c.get("教室") for c in slots[to_date][to_time]["classes"]}
        room = found.get("教室") if found.get("教室") not in used else next((f"Room {i}" for i in range(1,31) if f"Room {i}" not in used), "Room 1")
        slots[to_date][to_time]["classes"].append({
            "学生名单": student, "老师": found.get("老师", "待分配"),
            "科目": found.get("科目", ""), "教室": room, "时段": to_time,
        })
        return f"✅ 已移动：{student} {found.get('科目','')} | {from_date} {from_time} → {to_date} {to_time}", schedule_data

    if action == "chat":
        return op.get("message", "好的👌"), schedule_data

    return f"⚠️ 不支持的操作: {action}", schedule_data

def _parse_student_names(text: str, students: list) -> list:
    """把用户输入解析为学生名列表（支持编号、逗号、顿号分隔）"""
    known_names = {s['Student'] for s in students}
    result = []

    # 分割：逗号、顿号、空格、中文逗号
    parts = text.replace("，", ",").replace("、", ",").replace("\n", ",").split(",")
    parts = [p.strip() for p in parts if p.strip()]

    for p in parts:
        # 尝试当作编号
        try:
            idx = int(p) - 1
            if 0 <= idx < len(students):
                result.append(students[idx]['Student'])
                continue
        except ValueError:
            pass
        # 尝试直接匹配名字（大小写不敏感）
        for name in known_names:
            if name.lower() == p.lower():
                result.append(name)
                break
        else:
            # 部分匹配
            matches = [n for n in known_names if p.lower() in n.lower()]
            if len(matches) == 1:
                result.append(matches[0])

    # 去重保持顺序
    seen = set()
    return [x for x in result if not (x in seen or seen.add(x))]

def _find_student_slots(schedule_data: dict, name: str) -> list:
    results = []
    for d, day_data in schedule_data.get("slots", {}).items():
        for tk, slot in day_data.items():
            for c in slot.get("classes", []):
                if name in [s.strip() for s in c.get("学生名单", "").split("、")]:
                    results.append({**c, "date": d, "time": tk})
    return results

# ──────────────────────── AI 上下文格式化 ────────────────────────
def build_context(schedule_data: dict) -> str:
    """构建给 AI 看的精简上下文"""
    students = schedule_data.get("students", [])
    teachers = schedule_data.get("teachers", [])
    slots = schedule_data.get("slots", {})
    attendance = schedule_data.get("attendance", {})
    today = datetime.now().strftime("%Y-%m-%d")

    ctx = [f"=== 排课数据 ===\n当前日期: {today}"]
    ctx.append(f"\n【学生 {len(students)} 人】")
    for s in students:
        ctx.append(f"  {s['Student']} | G{s['Grade']} | {s['Subject']} | {s.get('Type','group')}")

    ctx.append(f"\n【老师 {len(teachers)} 人】")
    for t in teachers[:5]:
        subs = ", ".join(f"{x.get('subject','?')}" for x in t.get("subjects", []))
        ctx.append(f"  {t.get('name','?')} | {t.get('level','mid')} | {subs}")

    ctx.append(f"\n【今日考勤】")
    if today in attendance:
        for slot in TIME_SLOTS:
            names = attendance[today].get(slot, [])
            ctx.append(f"  [{slot}] {'、'.join(names) if names else '（未选）'}")
    else:
        ctx.append("  （今天还没选学生）")

    ctx.append(f"\n【已排课日期: {len(slots)} 个】")
    for d in sorted(slots.keys())[-3:]:  # 最近 3 天
        day_data = slots[d]
        for tk in sorted(day_data.keys()):
            for c in day_data[tk].get("classes", []):
                ctx.append(f"  {d} [{tk}] {c.get('科目','?')} | {c.get('老师','?')} | {c.get('学生名单','?')}")

    return "\n".join(ctx)

# ──────────────────────── Claude 解析 ────────────────────────
SYSTEM_PROMPT = """你是 UTU 排课系统的 AI 助手。学生名和老师名通常用英文。

四个时段（固定）：
  ① 09:30-11:30  ② 12:30-14:30  ③ 14:30-16:30  ④ 16:30-18:40

你帮用户做三件事：

━━━ 选学生（考勤）━━━
用户每天选择哪些学生来哪个时段。这是核心功能。
- "今天选学生" / "开始排课" / "选人" → 列出全部学生和四个时段当前状态
- "9:30-11:30: David, Yufei, Eva" → 给该时段选人
- "12:30-14:30: 全部学生" → 全选
- "14:30-16:30: 1,3,5,7" → 支持编号
- "清空 9:30-11:30" → clear_slot
- "查看考勤" → show_attendance

━━━ 学生管理 ━━━
- "添加学生：张三, G5, 数学/物理, 1v1" → add_student
- "删除李四" → remove_student（同时从考勤和排课中移除）
- "有哪些学生" → list_students

━━━ 排课操作 ━━━
- "查看张三的课表" → query_student
- "给今天来的学生排英语课" → 查看考勤后逐个 add_class
- "取消张三今天的课" → remove_class
- "把李四的课调到下午" → move_class

注意：
1. 日期用 YYYY-MM-DD。"今天"=当前日期
2. 时段必须是四个之一
3. 学生名/老师名要和数据里的严格匹配（大小写不敏感但尽量一致）
4. 选学生时如果用户说"全部"、"所有人"，使用全部学生
5. 不确定时反问用户，不要瞎编"""

TOOL_SCHEMA = {
    "name": "schedule_operation",
    "description": "执行排课操作",
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "list_students", "show_attendance", "select_students",
                    "clear_slot", "add_student", "remove_student",
                    "query_student", "query_teacher", "query_day",
                    "add_class", "remove_class", "move_class", "chat"
                ]
            },
            "student_name":   {"type": "string"},
            "student_names":  {"type": "string", "description": "多个学生名，逗号或顿号分隔，支持编号如1,3,5"},
            "teacher_name":   {"type": "string"},
            "subject":        {"type": "string"},
            "student_type":   {"type": "string", "description": "1v1 或 group"},
            "grade":          {"type": "integer"},
            "date":           {"type": "string", "description": "YYYY-MM-DD"},
            "time":           {"type": "string", "description": f"时段: {' / '.join(TIME_SLOTS)}"},
            "from_date":      {"type": "string"},
            "from_time":      {"type": "string"},
            "to_date":        {"type": "string"},
            "to_time":        {"type": "string"},
            "message":        {"type": "string"},
        },
        "required": ["action"]
    }
}

def parse_command(user_msg: str, schedule_data: dict) -> dict:
    if not anthropic:
        return {"action": "chat", "message": "🤖 服务器未配置 Anthropic API Key"}

    context = build_context(schedule_data)

    try:
        resp = anthropic.messages.create(
            model="claude-sonnet-5",
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": f"{context}\n\n用户: {user_msg}"}],
            tools=[TOOL_SCHEMA],
        )
        for block in resp.content:
            if block.type == "tool_use":
                return block.input
        text = "".join(block.text for block in resp.content if block.type == "text")
        return {"action": "chat", "message": text.strip() if text else "好的👌"}
    except Exception as e:
        print(f"[Claude] 错误: {e}")
        return {"action": "chat", "message": f"⚠️ AI 出错: {str(e)[:200]}"}

# ──────────────────────── API 路由（不变，网页端照常用） ────────────────────────

@app.route("/api/schedule", methods=["GET"])
def api_get_schedule():
    return jsonify(get_schedule())

@app.route("/api/schedule", methods=["POST"])
def api_save_schedule():
    data = request.get_json(force=True)
    # 保留已有的 attendance 数据（网页不会传这个字段）
    existing = get_schedule()
    data["attendance"] = existing.get("attendance", {})
    save_schedule(data)
    return jsonify({"ok": True})

@app.route("/api/command", methods=["POST"])
def api_command():
    body = request.get_json(force=True)
    user_msg = body.get("message", "").strip()
    if not user_msg:
        return jsonify({"error": "message 为空"}), 400
    sd = get_schedule()
    op = parse_command(user_msg, sd)
    reply, updated = execute_operation(sd, op)
    save_schedule(updated)
    return jsonify({"reply": reply, "operation": op, "schedule": updated})

# ──────────────────────── 飞书 Webhook ────────────────────────

@app.route("/feishu/webhook", methods=["POST"])
def feishu_webhook():
    body = request.get_json(force=True)

    if body.get("type") == "url_verification":
        return jsonify({"challenge": body.get("challenge", "")})

    event = body.get("event", {})
    if not event: return jsonify({"ok": True})

    msg = event.get("message", {})
    if msg.get("message_type") != "text":
        return jsonify({"ok": True})

    try:
        user_msg = json.loads(msg.get("content", "{}")).get("text", "").strip()
    except:
        return jsonify({"ok": True})

    if not user_msg: return jsonify({"ok": True})

    # 去掉群聊 @ 前缀
    if user_msg.startswith("@"):
        parts = user_msg.split(" ", 1)
        user_msg = parts[1] if len(parts) > 1 else ""

    sender_id = event.get("sender", {}).get("sender_id", {}).get("open_id", "")
    if not sender_id: return jsonify({"ok": True})

    print(f"[飞书] {sender_id[:8]}... 说: {user_msg}")

    schedule_data = get_schedule()
    op = parse_command(user_msg, schedule_data)
    reply, updated = execute_operation(schedule_data, op)
    save_schedule(updated)

    # 飞书消息最长 5000 字符，超了截断
    if len(reply) > 4500:
        reply = reply[:4500] + "\n\n...（内容过长已截断）"

    send_feishu_msg(sender_id, reply)
    return jsonify({"ok": True})

# ──────────────────────── 健康检查 ────────────────────────

@app.route("/", methods=["GET"])
def health():
    sd = get_schedule()
    return jsonify({
        "status": "ok",
        "service": "UTU 排课系统 AI 助手",
        "feishu_configured": bool(FEISHU_APP_ID and FEISHU_APP_SECRET),
        "claude_configured": bool(ANTHROPIC_API_KEY),
        "students": len(sd.get("students", [])),
        "teachers": len(sd.get("teachers", [])),
        "slots": len(sd.get("slots", {})),
    })

# ──────────────────────── 启动 ────────────────────────

if __name__ == "__main__":
    print("🚀 UTU 排课 AI 助手启动中...")
    print(f"{'✅' if FEISHU_APP_ID else '⚠️'} 飞书: {'已配置' if FEISHU_APP_ID else '未配置'}")
    print(f"{'✅' if ANTHROPIC_API_KEY else '⚠️'} Claude: {'已配置' if ANTHROPIC_API_KEY else '未配置'}")
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)
