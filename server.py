"""
UTU 排课系统 — AI 指令后端
===========================
功能：
  1. 接收飞书机器人消息
  2. 用 Claude API 把自然语言解析为课表操作
  3. 执行操作，修改课表数据
  4. 给网页前端提供数据 API
  5. 支持 ngrok / Render 部署

飞书机器人需要的事件订阅：
  - im.message.receive_v1 （接收私聊消息）

环境变量（部署时设置）：
  FEISHU_APP_ID       飞书应用 App ID
  FEISHU_APP_SECRET   飞书应用 App Secret
  ANTHROPIC_API_KEY   Anthropic API Key（调用 Claude）
"""

import json
import os
import time
import hashlib
import hmac
from datetime import datetime
from pathlib import Path
from functools import wraps

import requests
from flask import Flask, request, jsonify
from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv(override=True)  # 加载 .env 文件中的环境变量（覆盖已有值）

# ──────────────────────────── 配置 ────────────────────────────
app = Flask(__name__)

# CORS — 允许网页从任何域名访问 API
@app.after_request
def add_cors_headers(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    resp.headers["Access-Control-Allow-Methods"] = "GET,POST,OPTIONS"
    return resp

@app.route("/api/<path:path>", methods=["OPTIONS"])
@app.route("/api/<path:path>/", methods=["OPTIONS"])
def handle_options(path=""):
    return "", 204

DATA_DIR     = Path(__file__).parent / "data"
SCHEDULE_FILE = DATA_DIR / "schedule.json"
STUDENTS_FILE = DATA_DIR / "students.json"
TEACHERS_FILE = DATA_DIR / "teachers.json"

FEISHU_APP_ID     = os.environ.get("FEISHU_APP_ID", "")
FEISHU_APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

anthropic = Anthropic(
    api_key=ANTHROPIC_API_KEY,
    base_url="https://api.anthropic.com"  # 强制使用 Anthropic 官方 API，不受环境变量影响
) if ANTHROPIC_API_KEY else None

# 飞书 API 地址
FEISHU_HOST = "https://open.feishu.cn"

# ──────────────────────── 数据读写 ────────────────────────
def load_json(path: Path, default=None):
    if default is None:
        default = {}
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, IOError):
            return default
    return default

def save_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

def get_schedule():
    return load_json(SCHEDULE_FILE, {"students": [], "teachers": [], "slots": {}})

def save_schedule(data):
    save_json(SCHEDULE_FILE, data)

# ──────────────────────── 飞书 Token ────────────────────────
_tenant_token = {"token": "", "expires_at": 0}

def get_tenant_token():
    """获取飞书 tenant_access_token，自动缓存"""
    global _tenant_token
    if time.time() < _tenant_token["expires_at"] - 60:
        return _tenant_token["token"]

    resp = requests.post(
        f"{FEISHU_HOST}/open-apis/auth/v3/tenant_access_token/internal",
        json={"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET},
        timeout=10,
    )
    data = resp.json()
    if data.get("code") != 0:
        raise Exception(f"获取飞书 token 失败: {data}")

    _tenant_token["token"] = data["tenant_access_token"]
    _tenant_token["expires_at"] = time.time() + data.get("expire", 3600)
    return _tenant_token["token"]

def send_feishu_msg(open_id: str, text: str):
    """通过飞书 API 给用户发消息"""
    token = get_tenant_token()
    content = json.dumps({"text": text})
    resp = requests.post(
        f"{FEISHU_HOST}/open-apis/im/v1/messages?receive_id_type=open_id",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        json={"receive_id": open_id, "msg_type": "text", "content": content},
        timeout=10,
    )
    result = resp.json()
    if result.get("code") != 0:
        print(f"[飞书] 发送消息失败: {result}")
    return result


# ──────────────────────── 课表操作 ────────────────────────
def format_schedule_summary(schedule_data: dict) -> str:
    """把课表数据转成给 AI 看的摘要"""
    students = schedule_data.get("students", [])
    teachers = schedule_data.get("teachers", [])
    slots = schedule_data.get("slots", {})

    lines = ["=== 当前课表数据 ==="]
    lines.append(f"\n学生列表 ({len(students)} 人):")
    for s in students[:50]:  # 最多列 50 个
        lines.append(f"  - {s.get('Student','?')} | G{s.get('Grade','?')} | {s.get('Subject','?')} | {s.get('Type','group')}")
    if len(students) > 50:
        lines.append(f"  ... 还有 {len(students)-50} 人")

    lines.append(f"\n老师列表 ({len(teachers)} 人):")
    for t in teachers:
        subs = ", ".join(f"{x.get('subject','?')}(G≤{x.get('maxGrade','?')})" for x in t.get("subjects", []))
        lines.append(f"  - {t.get('name','?')} | {t.get('level','mid')} | {subs}")

    lines.append(f"\n已排课时段 ({len(slots)} 个日期):")
    for date in sorted(slots.keys()):
        day_slots = slots[date]
        lines.append(f"\n  📅 {date}")
        for time_key in sorted(day_slots.keys()):
            classes = day_slots[time_key].get("classes", [])
            for c in classes:
                lines.append(f"    [{time_key}] {c.get('科目','?')} | 老师:{c.get('老师','?')} | 学生:{c.get('学生名单','?')} | {c.get('教室','?')}")

    lines.append("\n=== 数据结束 ===")
    return "\n".join(lines)

def find_student_slots(schedule_data: dict, student_name: str) -> list:
    """查找某学生的所有排课"""
    results = []
    for date, day_data in schedule_data.get("slots", {}).items():
        for time_key, slot in day_data.items():
            for c in slot.get("classes", []):
                students_in_class = [s.strip() for s in c.get("学生名单", "").split("、")]
                if student_name in students_in_class:
                    results.append({**c, "date": date, "time": time_key})
    return results

def execute_operation(schedule_data: dict, op: dict) -> tuple[str, dict]:
    """
    执行课表操作，返回 (消息, 更新后的schedule_data)
    op 格式由 Claude 返回
    """
    action = op.get("action", "")
    slots = schedule_data.setdefault("slots", {})

    # ── query_student ──
    if action == "query_student":
        name = op.get("student_name", "")
        found = find_student_slots(schedule_data, name)
        if not found:
            return f"📭 {name} 目前没有任何排课记录", schedule_data
        lines = [f"📋 **{name} 的课表**："]
        for f in found:
            lines.append(f"  • {f['date']} {f['time']} | {f.get('科目','?')} | 老师:{f.get('老师','?')} | {f.get('教室','?')}")
        return "\n".join(lines), schedule_data

    # ── query_teacher ──
    if action == "query_teacher":
        name = op.get("teacher_name", "")
        found = []
        for date, day_data in slots.items():
            for time_key, slot in day_data.items():
                for c in slot.get("classes", []):
                    if c.get("老师") == name:
                        found.append({**c, "date": date, "time": time_key})
        if not found:
            return f"📭 {name} 目前没有排课记录", schedule_data
        lines = [f"📋 **{name} 的课表**："]
        for f in found:
            lines.append(f"  • {f['date']} {f['time']} | {f.get('科目','?')} | 学生:{f.get('学生名单','?')} | {f.get('教室','?')}")
        return "\n".join(lines), schedule_data

    # ── query_day ──
    if action == "query_day":
        date = op.get("date", "")
        if date not in slots:
            return f"📭 {date} 没有排课记录", schedule_data
        day = slots[date]
        lines = [f"📋 **{date} 课表**："]
        for time_key in sorted(day.keys()):
            for c in day[time_key].get("classes", []):
                lines.append(f"  • [{time_key}] {c.get('科目','?')} | {c.get('老师','?')} | {c.get('学生名单','?')} | {c.get('教室','?')}")
        return "\n".join(lines), schedule_data

    # ── remove_class ──
    if action == "remove_class":
        student = op.get("student_name", "")
        date = op.get("date", "")
        time = op.get("time", "")
        subject = op.get("subject", "")

        if date and date in slots and time and time in slots[date]:
            classes = slots[date][time].get("classes", [])
            removed = []
            kept = []
            for c in classes:
                students_in_class = [s.strip() for s in c.get("学生名单", "").split("、")]
                # 匹配：学生名在班里，且（指定了科目则科目也匹配，没指定则移除学生）
                if student in students_in_class:
                    if subject and c.get("科目") != subject:
                        kept.append(c)
                        continue
                    new_students = [s for s in students_in_class if s != student]
                    if new_students:
                        c["学生名单"] = "、".join(new_students)
                        kept.append(c)
                    # 班里没学生了就删除这个班
                    removed.append(c)
                else:
                    kept.append(c)

            slots[date][time]["classes"] = kept
            if not kept:
                del slots[date][time]
                if not slots[date]:
                    del slots[date]

            if removed:
                return f"✅ 已取消 {student} 在 {date} {time} 的{subject or '课'}", schedule_data
            return f"⚠️ 未找到 {student} 在 {date} {time} 的{subject or '课'}", schedule_data
        return f"⚠️ 未找到对应时段 {date} {time}", schedule_data

    # ── add_class ──
    if action == "add_class":
        student = op.get("student_name", "")
        date = op.get("date", "")
        time = op.get("time", "")
        subject = op.get("subject", "")
        teacher = op.get("teacher_name", "")

        if not all([student, date, time, subject]):
            return "⚠️ 信息不足，请提供：学生名、日期、时段、科目", schedule_data

        slots.setdefault(date, {}).setdefault(time, {"classes": []})

        # 检查学生是否已经在该时段有课
        for c in slots[date][time]["classes"]:
            students_in_class = [s.strip() for s in c.get("学生名单", "").split("、")]
            if student in students_in_class:
                return f"⚠️ {student} 在 {date} {time} 已经有课了（{c.get('科目','?')}）", schedule_data

        # 找一间空闲教室
        used_rooms = {c.get("教室") for c in slots[date][time]["classes"] if c.get("教室")}
        room = "Room 1"
        for i in range(1, 31):
            r = f"Room {i}"
            if r not in used_rooms:
                room = r
                break

        new_class = {
            "学生名单": student,
            "老师": teacher or "待分配",
            "科目": subject,
            "教室": room,
            "时段": time,
        }
        slots[date][time]["classes"].append(new_class)
        return f"✅ 已添加：{student} | {subject} | {date} {time} | 老师:{teacher or '待分配'} | {room}", schedule_data

    # ── move_class ──
    if action == "move_class":
        student = op.get("student_name", "")
        from_date = op.get("from_date", "")
        from_time = op.get("from_time", "")
        to_date = op.get("to_date", from_date)  # 如果没指定目标日期，同一天
        to_time = op.get("to_time", "")

        if not all([student, from_date, from_time, to_time]):
            return "⚠️ 信息不足，请提供：学生名、原日期/时段、目标时段", schedule_data

        # 找到原来的课
        found_cls = None
        if from_date in slots and from_time in slots[from_date]:
            for c in slots[from_date][from_time].get("classes", []):
                students_in_class = [s.strip() for s in c.get("学生名单", "").split("、")]
                if student in students_in_class:
                    found_cls = c
                    break

        if not found_cls:
            return f"⚠️ 未找到 {student} 在 {from_date} {from_time} 的课", schedule_data

        # 从原时段移除该学生
        old_students = [s.strip() for s in found_cls.get("学生名单", "").split("、")]
        new_old_students = [s for s in old_students if s != student]
        if new_old_students:
            found_cls["学生名单"] = "、".join(new_old_students)
        else:
            slots[from_date][from_time]["classes"].remove(found_cls)
            if not slots[from_date][from_time]["classes"]:
                del slots[from_date][from_time]
                if not slots[from_date]:
                    del slots[from_date]

        # 添加到目标时段
        slots.setdefault(to_date, {}).setdefault(to_time, {"classes": []})

        used_rooms = {c.get("教室") for c in slots[to_date][to_time]["classes"] if c.get("教室")}
        room = found_cls.get("教室", "Room 1")
        if room in used_rooms:
            for i in range(1, 31):
                r = f"Room {i}"
                if r not in used_rooms:
                    room = r
                    break

        new_class = {
            "学生名单": student,
            "老师": found_cls.get("老师", "待分配"),
            "科目": found_cls.get("科目", ""),
            "教室": room,
            "时段": to_time,
        }
        slots[to_date][to_time]["classes"].append(new_class)

        return f"✅ 已移动：{student} {found_cls.get('科目','')} | {from_date} {from_time} → {to_date} {to_time}", schedule_data

    # ── chat ──
    if action == "chat":
        return op.get("message", "好的👌"), schedule_data

    # ── unknown ──
    return f"⚠️ 不支持的操作: {action}", schedule_data


# ──────────────────────── Claude 解析 ────────────────────────
SYSTEM_PROMPT = """你是一个排课助手的 AI。用户会用自然语言给你发指令来管理课表。

数据中有以下字段（注：学生名和老师名通常用英文名）：
- 学生: Student(姓名), Grade(年级), Subject(科目，多科目用/分隔), Type(group=班课 或 1v1=一对一)
- 老师: name(姓名), level(低/中/高), subjects(可教科目列表，含maxGrade最大年级)
- 排课记录: 日期(YYYY-MM-DD), 时段(如"09:00-10:30"), 科目, 老师, 学生名单(用"、"分隔), 教室

你需要把用户指令解析为操作。用 schedule_operation 工具返回。

常见指令示例：
- "查看张三的课表" → query_student
- "王老师今天有什么课" → query_teacher
- "周三有哪些课" → query_day（需要知道具体日期）
- "把张三的数学课从周一调到周三下午" → move_class
- "在周三下午给张三加一节物理课，王老师教" → add_class
- "取消李四周五的英语课" → remove_class
- "今天有多少节课" → 可以用 query_day

注意：
1. 日期用 YYYY-MM-DD 格式。如果用户说"今天"、"周三"，根据当前日期推算。
2. 时段尽量匹配系统已有的时段。
3. 如果不确定某个字段，可以留空让用户确认。
4. 如果用户只是聊天（你好、谢谢等），用 action="chat" 回复。"""

TOOL_SCHEMA = {
    "name": "schedule_operation",
    "description": "对课表执行操作",
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["query_student", "query_teacher", "query_day", "move_class", "add_class", "remove_class", "chat"]
            },
            "student_name": {"type": "string", "description": "学生姓名"},
            "teacher_name": {"type": "string", "description": "老师姓名"},
            "subject": {"type": "string", "description": "科目"},
            "date": {"type": "string", "description": "日期 YYYY-MM-DD"},
            "time": {"type": "string", "description": "时段，如 09:00-10:30"},
            "from_date": {"type": "string", "description": "原日期"},
            "from_time": {"type": "string", "description": "原时段"},
            "to_date": {"type": "string", "description": "目标日期"},
            "to_time": {"type": "string", "description": "目标时段"},
            "message": {"type": "string", "description": "对用户的回复（非操作类对话时使用）"}
        },
        "required": ["action"]
    }
}

def parse_command(user_msg: str, schedule_data: dict) -> dict:
    """用 Claude API 把自然语言解析为课表操作"""
    if not anthropic:
        # 没有 API key 时返回 chat，提示用户配置
        return {"action": "chat", "message": "🤖 AI 指令功能需要配置 Anthropic API Key。请在环境变量中设置 ANTHROPIC_API_KEY。"}

    context = format_schedule_summary(schedule_data)
    today = datetime.now().strftime("%Y-%m-%d")

    user_content = f"当前日期: {today}\n\n{context}\n\n用户指令: {user_msg}"

    try:
        resp = anthropic.messages.create(
            model="claude-sonnet-5",
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_content}],
            tools=[TOOL_SCHEMA],
        )

        # 提取 tool_use 结果
        for block in resp.content:
            if block.type == "tool_use":
                return block.input

        # 没有 tool_use，返回文本回复
        text = "".join(block.text for block in resp.content if block.type == "text")
        return {"action": "chat", "message": text.strip() if text else "好的👌"}

    except Exception as e:
        print(f"[Claude] 解析失败: {e}")
        return {"action": "chat", "message": f"⚠️ AI 解析出错，请重试。错误: {str(e)[:100]}"}


# ──────────────────────── API 路由 ────────────────────────

@app.route("/api/schedule", methods=["GET"])
def api_get_schedule():
    """网页获取完整课表数据"""
    return jsonify(get_schedule())

@app.route("/api/schedule", methods=["POST"])
def api_save_schedule():
    """网页上传课表数据（Excel 导入后同步到服务器）"""
    data = request.get_json(force=True)
    save_schedule(data)
    return jsonify({"ok": True, "msg": "课表已同步到服务器"})

@app.route("/api/command", methods=["POST"])
def api_command():
    """网页端 AI 指令接口（和飞书用同一套逻辑）"""
    body = request.get_json(force=True)
    user_msg = body.get("message", "").strip()
    if not user_msg:
        return jsonify({"error": "message 为空"}), 400

    schedule_data = get_schedule()
    op = parse_command(user_msg, schedule_data)
    reply, updated_data = execute_operation(schedule_data, op)
    save_schedule(updated_data)
    return jsonify({"reply": reply, "operation": op, "schedule": updated_data})


# ──────────────────────── 飞书 Webhook ────────────────────────

@app.route("/feishu/webhook", methods=["POST"])
def feishu_webhook():
    """接收飞书事件回调"""
    body = request.get_json(force=True)
    print(f"[飞书] 收到事件: {json.dumps(body, ensure_ascii=False)[:500]}")

    # ── URL 验证（配置事件订阅时飞书会发 challenge） ──
    if body.get("type") == "url_verification":
        challenge = body.get("challenge", "")
        print(f"[飞书] URL 验证 challenge={challenge}")
        return jsonify({"challenge": challenge})

    # ── 处理消息事件 ──
    event = body.get("event", {})
    if not event:
        return jsonify({"ok": True})

    msg_type = event.get("message", {}).get("message_type", "")
    chat_type = event.get("message", {}).get("chat_type", "")

    # 只处理私聊文本消息
    if msg_type != "text":
        return jsonify({"ok": True})
    if chat_type != "p2p":
        # 群聊消息也接受，但需要 @机器人
        pass

    # 提取消息内容
    content_str = event.get("message", {}).get("content", "{}")
    try:
        content = json.loads(content_str)
    except json.JSONDecodeError:
        content = {}
    user_msg = content.get("text", "").strip()
    if not user_msg:
        return jsonify({"ok": True})

    # 群聊中可能包含 @机器人 前缀，去掉
    if "@" in user_msg and user_msg.startswith("@"):
        parts = user_msg.split(" ", 1)
        user_msg = parts[1] if len(parts) > 1 else ""

    if not user_msg:
        return jsonify({"ok": True})

    # 获取发送者 open_id
    sender_id = event.get("sender", {}).get("sender_id", {}).get("open_id", "")
    if not sender_id:
        return jsonify({"ok": True})

    # ── 处理指令 ──
    print(f"[飞书] 用户 {sender_id} 说: {user_msg}")
    schedule_data = get_schedule()
    op = parse_command(user_msg, schedule_data)
    reply, updated_data = execute_operation(schedule_data, op)
    save_schedule(updated_data)

    # 回复用户
    send_feishu_msg(sender_id, reply)

    return jsonify({"ok": True})


# ──────────────────────── 健康检查 ────────────────────────
@app.route("/", methods=["GET"])
def health():
    return jsonify({
        "status": "ok",
        "service": "UTU 排课系统 AI 助手",
        "feishu_configured": bool(FEISHU_APP_ID and FEISHU_APP_SECRET),
        "claude_configured": bool(ANTHROPIC_API_KEY),
        "students": len(get_schedule().get("students", [])),
        "teachers": len(get_schedule().get("teachers", [])),
        "slots": len(get_schedule().get("slots", {})),
        "endpoints": {
            "webhook": "/feishu/webhook",
            "api_schedule": "/api/schedule",
            "api_command": "/api/command",
        }
    })


# ──────────────────────── 启动 ────────────────────────
if __name__ == "__main__":
    print("🚀 UTU 排课 AI 助手启动中...")

    if not FEISHU_APP_ID:
        print("⚠️  FEISHU_APP_ID 未设置（飞书机器人不可用）")
    else:
        print(f"✅ 飞书 App: {FEISHU_APP_ID}")

    if not ANTHROPIC_API_KEY:
        print("⚠️  ANTHROPIC_API_KEY 未设置（AI 指令解析不可用）")
    else:
        print("✅ Claude API 已配置")

    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=True)
