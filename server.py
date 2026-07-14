"""
UTU 排课系统 — AI 指令后端
===========================
电脑网页端：不变，照常用 Excel 导入学生/老师，数据同步到服务器
飞书机器人：手机上选学生、分配时段、增删学生、自动排课

四个时段：
  ① 09:30-11:30  ② 12:30-14:30  ③ 14:30-16:30  ④ 16:30-18:40

环境变量：
  FEISHU_APP_ID / FEISHU_APP_SECRET / ANTHROPIC_API_KEY
"""

import json, os, time, random
from datetime import datetime
from pathlib import Path
from copy import deepcopy

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

# 用户会话状态（记住用户在做什么）
user_sessions = {}  # {open_id: {"state": "selecting_slot"|None, "current_slot": "09:30-11:30"}}

TIME_SLOTS = ["09:30-11:30", "12:30-14:30", "14:30-16:30", "16:30-18:40"]
ROOM_LIST  = [f"Room {i}" for i in range(1, 31) if i not in (9, 11)]
ENGLISH_SUBS = {"vocabulary","1000","2000","4000","grammar","ielts","phonics","esl"}
VOCAB_LEVELS = {"1000","2000","4000"}

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
    return load_json(SCHEDULE_FILE, {"students": [], "teachers": [], "slots": {}, "attendance": {}, "excluded_teachers": {}})

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

def send_feishu_card(open_id: str, card_json: dict):
    """发送飞书交互式卡片消息"""
    token = get_tenant_token()
    resp = requests.post(
        f"{FEISHU_HOST}/open-apis/im/v1/messages?receive_id_type=open_id",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"receive_id": open_id, "msg_type": "interactive", "content": json.dumps(card_json, ensure_ascii=False)},
        timeout=10)
    result = resp.json()
    if result.get("code") != 0:
        print(f"[飞书] 卡片发送失败: {result}")
    return result

def build_main_card(attendance: dict, excluded: dict, students: list) -> dict:
    """构建主菜单卡片（带按钮）"""
    today = datetime.now().strftime("%Y-%m-%d")
    day_att = attendance.get(today, {})
    day_exc = excluded.get(today, [])

    # 各时段人数
    counts = {}
    for slot in TIME_SLOTS:
        counts[slot] = len(day_att.get(slot, []))
    total = sum(counts.values())

    status_lines = [f"**{today}**　已选 **{total}/{len(students)}** 人"]
    if day_exc:
        status_lines.append(f"🚫 排除老师：{', '.join(day_exc)}")
    for i, slot in enumerate(TIME_SLOTS):
        names = day_att.get(slot, [])
        status_lines.append(f"  {i+1} [{slot}] {counts[slot]}人" + (f"：{', '.join(names)}" if names else ""))

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "📋 UTU 排课系统"},
            "template": "blue"
        },
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(status_lines)}},
            {"tag": "hr"},
            {"tag": "div", "text": {"tag": "lark_md", "content": "**👇 点击时段选学生**"}},
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "lark_md", "content": f"① 9:30-11:30（{counts[TIME_SLOTS[0]]}人）"},
                        "type": "primary" if counts[TIME_SLOTS[0]] == 0 else "default",
                        "value": json.dumps({"action": "start_select", "slot": TIME_SLOTS[0]})
                    },
                    {
                        "tag": "button",
                        "text": {"tag": "lark_md", "content": f"② 12:30-14:30（{counts[TIME_SLOTS[1]]}人）"},
                        "type": "primary" if counts[TIME_SLOTS[1]] == 0 else "default",
                        "value": json.dumps({"action": "start_select", "slot": TIME_SLOTS[1]})
                    }
                ]
            },
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "lark_md", "content": f"③ 14:30-16:30（{counts[TIME_SLOTS[2]]}人）"},
                        "type": "primary" if counts[TIME_SLOTS[2]] == 0 else "default",
                        "value": json.dumps({"action": "start_select", "slot": TIME_SLOTS[2]})
                    },
                    {
                        "tag": "button",
                        "text": {"tag": "lark_md", "content": f"④ 16:30-18:40（{counts[TIME_SLOTS[3]]}人）"},
                        "type": "primary" if counts[TIME_SLOTS[3]] == 0 else "default",
                        "value": json.dumps({"action": "start_select", "slot": TIME_SLOTS[3]})
                    }
                ]
            },
            {"tag": "hr"},
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "lark_md", "content": "👥 学生列表"},
                        "type": "default",
                        "value": json.dumps({"action": "list_students"})
                    },
                    {
                        "tag": "button",
                        "text": {"tag": "lark_md", "content": "🚀 自动排课"},
                        "type": "primary",
                        "value": json.dumps({"action": "auto_schedule"})
                    },
                    {
                        "tag": "button",
                        "text": {"tag": "lark_md", "content": "📊 查看课表"},
                        "type": "default",
                        "value": json.dumps({"action": "query_day"})
                    },
                    {
                        "tag": "button",
                        "text": {"tag": "lark_md", "content": "🔄 刷新"},
                        "type": "default",
                        "value": json.dumps({"action": "show_main"})
                    }
                ]
            },
            {
                "tag": "note",
                "elements": [{"tag": "plain_text",
                    "content": "💡 点时段按钮选学生 → 回复编号 → 再点「自动排课」。也可直接发文字指令。"}]
            }
        ]
    }


def _show_main_card(sender_id):
    """发送主信息卡片"""
    sd = get_schedule()
    attendance = sd.get("attendance", {})
    excluded = sd.get("excluded_teachers", {})
    students = sd.get("students", [])
    send_feishu_card(sender_id, build_main_card(attendance, excluded, students))

def build_select_card(slot: str, students: list, attendance: dict, excluded: dict) -> dict:
    """构建选学生卡片——40个选项分两排"""
    today = datetime.now().strftime("%Y-%m-%d")
    day_att = attendance.get(today, {})
    already = set(day_att.get(slot, []))

    # 学生选项（带科目信息）
    options = []
    for s in students:
        label = f"{s['Student']} | G{s['Grade']} | {s['Subject']}"
        options.append({
            "value": s["Student"],
            "text": {"tag": "plain_text", "content": label}
        })

    # 预先勾选已有的学生
    initial_selected = [s for s in already if s in {st['Student'] for st in students}]

    elements = [
        {
            "tag": "div",
            "text": {"tag": "lark_md", "content": f"**选择 [{slot}] 的学生**\n选完后点底部「✅ 确认」按钮"}
        },
        {
            "tag": "action",
            "actions": [
                {
                    "tag": "multi_select_static",
                    "placeholder": {"tag": "plain_text", "content": f"选择 {slot} 的学生"},
                    "initial_options": initial_selected[:50],
                    "options": options[:100],
                    "value": {"key": "student_list"}
                }
            ]
        },
        {"tag": "hr"},
        {
            "tag": "action",
            "actions": [
                {
                    "tag": "button",
                    "text": {"tag": "lark_md", "content": "✅ 确认选择"},
                    "type": "primary",
                    "value": json.dumps({"action": "confirm_select", "slot": slot}),
                    "confirm": {
                        "title": {"tag": "plain_text", "content": "确认选择？"},
                        "text": {"tag": "plain_text", "content": f"将更新 [{slot}] 的学生名单"}
                    }
                },
                {
                    "tag": "button",
                    "text": {"tag": "lark_md", "content": "全部学生"},
                    "type": "default",
                    "value": json.dumps({"action": "quick_select", "slot": slot, "mode": "all"})
                },
                {
                    "tag": "button",
                    "text": {"tag": "lark_md", "content": "清空此时段"},
                    "type": "danger",
                    "value": json.dumps({"action": "quick_select", "slot": slot, "mode": "clear"})
                }
            ]
        },
        {
            "tag": "action",
            "actions": [
                {
                    "tag": "button",
                    "text": {"tag": "lark_md", "content": "🔙 返回主菜单"},
                    "type": "default",
                    "value": json.dumps({"action": "show_main"})
                }
            ]
        },
        {
            "tag": "note",
            "elements": [{"tag": "plain_text",
                "content": f"💡 如果多选框不好用，直接发文字：'{slot}: 学生名1, 学生名2' 即可"}]
        }
    ]

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"✏️ 选学生 - {slot}"},
            "template": "green"
        },
        "elements": elements
    }


# ──────────────────────────── 排课算法核心 ────────────────────────────

def is_match(teacher_sub: str, student_sub: str) -> bool:
    """科目匹配逻辑，和网页端一致"""
    ts = teacher_sub.strip().lower()
    ss = student_sub.strip().lower()
    if ts == ss:
        return True
    if ts == "vocabulary" and ss in VOCAB_LEVELS:
        return True
    if ss == "vocabulary" and ts in VOCAB_LEVELS:
        return True
    return False

def is_english_subject(subj: str) -> bool:
    return subj.strip().lower() in ENGLISH_SUBS

def get_group_key(subject: str, grade: int) -> str:
    """组班 key：英语类跨年级，非英语必须同年级；词汇课按等级"""
    s = subject.strip().lower()
    if s in VOCAB_LEVELS:
        return s  # 1000 只和 1000，2000 只和 2000
    if is_english_subject(s):
        return s  # ESL, Grammar 等可跨年级
    return f"{s}_{grade}"  # 数学、物理等必须同年级

def schedule_slot(students: list, teachers: list, time_text: str,
                  max_class_student: int = 4, excluded_teachers: set = None) -> dict:
    """
    核心排课算法（翻译自网页端 JS）

    返回: {
        "result": [{学生名单, 老师, 科目, 教室, 时段, 人数, type, grade}],
        "failNames": [...],
        "idleTeachers": [...],
        "placedCount": int
    }
    """
    if excluded_teachers is None:
        excluded_teachers = set()

    # 过滤掉排除的老师
    teachers = [t for t in teachers if t['name'] not in excluded_teachers]

    room_idx = [0]
    def get_room():
        r = ROOM_LIST[room_idx[0] % len(ROOM_LIST)]
        room_idx[0] += 1
        return r

    result = []
    # 复制学生列表，添加状态字段
    all_students = []
    for s in students:
        prefer_tea = s.get("preferTea", "") or ""
        subs = (s["Subject"] + "").split("/")
        subs = [x.strip() for x in subs if x.strip()]
        all_students.append({
            "name": s["Student"],
            "grade": int(s.get("Grade", 0)),
            "subjects": subs,
            "stu_type": (s.get("Type", "group") or "group").strip(),
            "preferTea": prefer_tea,
            "placed": False,
            "matchedTeacher": None,
            "matchedSubject": None,
        })

    # 获取每个老师的候选学生
    lv_rank_desc = {"high": 0, "mid": 1, "low": 2}
    lv_rank_asc  = {"high": 0, "mid": 1, "low": 2}
    all_teachers = [{"name": t["name"], "subjects": t.get("subjects", []),
                      "level": t.get("level", "mid"), "busy": False} for t in teachers]
    all_teachers.sort(key=lambda t: (lv_rank_desc.get(t["level"], 1), t["name"]))

    def get_candidates(stu):
        candidates = []
        for tea in all_teachers:
            for sub in stu["subjects"]:
                for ts in tea["subjects"]:
                    ts_sub = ts.get("subject", ts) if isinstance(ts, dict) else ts
                    max_grade = ts.get("maxGrade", 99) if isinstance(ts, dict) else 99
                    if is_match(ts_sub, sub) and stu["grade"] <= max_grade:
                        candidates.append({"teacher": tea, "subject": sub})
                        break
        candidates.sort(key=lambda c: lv_rank_asc.get(c["teacher"]["level"], 1))
        return candidates

    # ── 分组 ──
    p1_stus = [s for s in all_students if s["stu_type"] == "1v1" and s["preferTea"]]
    p2_stus = [s for s in all_students if s["stu_type"] != "1v1" and s["preferTea"]]
    p3_stus = [s for s in all_students if s["stu_type"] == "1v1" and not s["preferTea"]]
    p4_stus = [s for s in all_students if s["stu_type"] != "1v1" and not s["preferTea"]]

    reserved_teachers = set()   # P1/P2 预留
    locked_teachers = set()     # P3 1v1 独占
    match_map = {}

    # ── P1: 1v1 + 指定老师 ──
    p1_by_tea = {}
    for stu in p1_stus:
        tea = next((t for t in all_teachers if t["name"] == stu["preferTea"]), None)
        if not tea: continue
        ok_sub = None
        for sub in stu["subjects"]:
            for ts in tea["subjects"]:
                ts_sub = ts.get("subject", ts) if isinstance(ts, dict) else ts
                max_grade = ts.get("maxGrade", 99) if isinstance(ts, dict) else 99
                if is_match(ts_sub, sub) and stu["grade"] <= max_grade:
                    ok_sub = sub; break
            if ok_sub: break
        if not ok_sub: continue
        key = tea["name"]
        if key not in p1_by_tea:
            p1_by_tea[key] = {"tea": tea, "pairs": []}
        p1_by_tea[key]["pairs"].append((stu, ok_sub))
        reserved_teachers.add(tea["name"])

    for tea_name, entry in p1_by_tea.items():
        tea = entry["tea"]
        by_sub = {}
        for stu, sub in entry["pairs"]:
            by_sub.setdefault(sub, []).append(stu)
        for sub, stus in by_sub.items():
            names = "、".join(s["name"] for s in stus)
            result.append({
                "时段": time_text, "教室": get_room(), "老师": tea_name, "科目": sub,
                "人数": len(stus), "type": "1v1", "grade": stus[0]["grade"], "学生名单": names
            })
            for s in stus:
                s["placed"] = True; s["matchedTeacher"] = tea; s["matchedSubject"] = sub
        tea["busy"] = True

    # ── P2: 班课 + 指定老师 ──
    p2_by_tea_sub = {}
    for stu in p2_stus:
        tea = next((t for t in all_teachers if t["name"] == stu["preferTea"]), None)
        if not tea: continue
        ok_sub = None
        for sub in stu["subjects"]:
            for ts in tea["subjects"]:
                ts_sub = ts.get("subject", ts) if isinstance(ts, dict) else ts
                max_grade = ts.get("maxGrade", 99) if isinstance(ts, dict) else 99
                if is_match(ts_sub, sub) and stu["grade"] <= max_grade:
                    ok_sub = sub; break
            if ok_sub: break
        if not ok_sub: continue
        key = f"{tea['name']}||{ok_sub}"
        if key not in p2_by_tea_sub:
            p2_by_tea_sub[key] = {"tea": tea, "sub": ok_sub, "stus": []}
        p2_by_tea_sub[key]["stus"].append(stu)
        reserved_teachers.add(tea["name"])

    for entry in p2_by_tea_sub.values():
        tea = entry["tea"]
        sub = entry["sub"]
        stus = entry["stus"]
        tea["busy"] = True
        for i in range(0, len(stus), max_class_student):
            batch = stus[i:i+max_class_student]
            names = "、".join(s["name"] for s in batch)
            result.append({
                "时段": time_text, "教室": get_room(), "老师": tea["name"], "科目": sub,
                "人数": len(batch), "type": "group", "grade": batch[0]["grade"], "学生名单": names
            })
            for s in batch:
                s["placed"] = True; s["matchedTeacher"] = tea; s["matchedSubject"] = sub

    # ── P3: 1v1 + 无指定 → 匈牙利算法 ──
    def augment_1v1(stu, visited):
        for c in get_candidates(stu):
            tea = c["teacher"]
            if tea["name"] in visited: continue
            if tea["name"] in reserved_teachers: continue
            visited.add(tea["name"])
            if tea["name"] not in match_map:
                match_map[tea["name"]] = (stu, c["subject"])
                return True
            prev_stu, _ = match_map[tea["name"]]
            if augment_1v1(prev_stu, visited):
                match_map[tea["name"]] = (stu, c["subject"])
                return True
        return False

    p3_stus.sort(key=lambda s: len(get_candidates(s)))
    for stu in p3_stus:
        augment_1v1(stu, set())

    for tea_name, (stu, sub) in match_map.items():
        tea = next(t for t in all_teachers if t["name"] == tea_name)
        stu["matchedTeacher"] = tea; stu["matchedSubject"] = sub
        tea["busy"] = True; locked_teachers.add(tea_name)

    for stu in p3_stus:
        if stu.get("matchedTeacher"):
            result.append({
                "时段": time_text, "教室": get_room(), "老师": stu["matchedTeacher"]["name"],
                "科目": stu["matchedSubject"], "人数": 1, "type": "1v1",
                "grade": stu["grade"], "学生名单": stu["name"]
            })
            stu["placed"] = True

    match_map.clear()

    # ── P4: 班课 + 无指定 → 匈牙利算法 ──
    def augment(stu, visited):
        for c in get_candidates(stu):
            tea = c["teacher"]
            if tea["name"] in visited: continue
            if tea["name"] in reserved_teachers: continue
            if tea["name"] in locked_teachers: continue
            if tea["busy"]: continue
            visited.add(tea["name"])
            if tea["name"] not in match_map or augment(match_map[tea["name"]][0], visited):
                match_map[tea["name"]] = (stu, c["subject"])
                return True
        return False

    p4_stus.sort(key=lambda s: len(get_candidates(s)))
    for stu in p4_stus:
        augment(stu, set())

    for tea_name, (stu, sub) in match_map.items():
        tea = next(t for t in all_teachers if t["name"] == tea_name)
        stu["matchedTeacher"] = tea; stu["matchedSubject"] = sub
        tea["busy"] = True

    for stu in p4_stus:
        if stu.get("matchedTeacher") and not stu["placed"]:
            result.append({
                "时段": time_text, "教室": get_room(), "老师": stu["matchedTeacher"]["name"],
                "科目": stu["matchedSubject"], "人数": 1, "type": "group",
                "grade": stu["grade"], "学生名单": stu["name"]
            })
            stu["placed"] = True

    # ── 未排班课学生 → 合入已有班级 ──
    unmatched = [s for s in all_students if not s["placed"] and s["stu_type"] != "1v1"]
    for stu in unmatched:
        for sub in stu["subjects"]:
            key = get_group_key(sub, stu["grade"])
            open_classes = [c for c in result
                if c.get("type") != "1v1"
                and get_group_key(c["科目"], c["grade"]) == key
                and len(c["学生名单"].split("、")) < max_class_student
                and stu["name"] not in c["学生名单"].split("、")]
            open_classes.sort(key=lambda c: c["人数"])
            if open_classes:
                cls = open_classes[0]
                names = cls["学生名单"].split("、") + [stu["name"]]
                cls["学生名单"] = "、".join(names)
                cls["人数"] = len(names)
                stu["placed"] = True; break

    # ── 仍未排 → 找空闲老师新开班 → 最后手段扩展搜索 ──
    still_failed = [s for s in all_students if not s["placed"] and s["stu_type"] != "1v1"]
    for stu in still_failed:
        merged = False
        for sub in stu["subjects"]:
            key = get_group_key(sub, stu["grade"])
            open_classes = [c for c in result
                if c.get("type") != "1v1"
                and get_group_key(c["科目"], c["grade"]) == key
                and len(c["学生名单"].split("、")) < max_class_student
                and stu["name"] not in c["学生名单"].split("、")]
            if open_classes:
                open_classes.sort(key=lambda c: c["人数"])
                cls = open_classes[0]
                names = cls["学生名单"].split("、") + [stu["name"]]
                cls["学生名单"] = "、".join(names)
                cls["人数"] = len(names)
                stu["placed"] = True; merged = True; break
        if merged: continue

        # 找空闲老师
        for sub in stu["subjects"]:
            free_tea = next((t for t in all_teachers
                if not t["busy"] and t["name"] not in reserved_teachers
                and t["name"] not in locked_teachers
                and any(is_match(ts.get("subject",ts) if isinstance(ts,dict) else ts, sub)
                        and stu["grade"] <= (ts.get("maxGrade",99) if isinstance(ts,dict) else 99)
                        for ts in t["subjects"])), None)
            if free_tea:
                free_tea["busy"] = True
                result.append({
                    "时段": time_text, "教室": get_room(), "老师": free_tea["name"],
                    "科目": sub, "人数": 1, "type": "group",
                    "grade": stu["grade"], "学生名单": stu["name"]
                })
                stu["placed"] = True; break

        # 最后手段：扩展现有班
        if not stu["placed"]:
            for sub in stu["subjects"]:
                is_eng = is_english_subject(sub)
                open_classes = [c for c in result
                    if c.get("type") != "1v1"
                    and is_match(c["科目"], sub)
                    and (is_eng or c["grade"] == stu["grade"])
                    and len(c["学生名单"].split("、")) < max_class_student
                    and stu["name"] not in c["学生名单"].split("、")]
                if open_classes:
                    open_classes.sort(key=lambda c: c["人数"])
                    cls = open_classes[0]
                    names = cls["学生名单"].split("、") + [stu["name"]]
                    cls["学生名单"] = "、".join(names)
                    cls["人数"] = len(names)
                    stu["placed"] = True; break

    # ── 未排 1v1 学生 → 最后手段 ──
    failed_1v1 = [s for s in all_students if not s["placed"] and s["stu_type"] == "1v1"]
    for stu in failed_1v1:
        for sub in stu["subjects"]:
            free_tea = next((t for t in all_teachers
                if not t["busy"] and t["name"] not in reserved_teachers
                and t["name"] not in locked_teachers
                and any(is_match(ts.get("subject",ts) if isinstance(ts,dict) else ts, sub)
                        and stu["grade"] <= (ts.get("maxGrade",99) if isinstance(ts,dict) else 99)
                        for ts in t["subjects"])), None)
            if free_tea:
                free_tea["busy"] = True
                result.append({
                    "时段": time_text, "教室": get_room(), "老师": free_tea["name"],
                    "科目": sub, "人数": 1, "type": "1v1",
                    "grade": stu["grade"], "学生名单": stu["name"]
                })
                stu["placed"] = True; break

    all_failed    = [s for s in all_students if not s["placed"]]
    idle_teachers = [t["name"] for t in all_teachers if not t["busy"]]

    return {
        "result": result,
        "failCount": len(all_failed),
        "failNames": [f"{s['name']}（{'/'.join(s['subjects'])}，G{s['grade']}）" for s in all_failed],
        "idleTeachers": idle_teachers,
        "placedCount": sum(1 for s in all_students if s["placed"])
    }

# ──────────────────────── 格式化输出 ────────────────────────

def format_student_list(students: list) -> str:
    lines = []
    for i, s in enumerate(students):
        lines.append(f"  {i+1}. {s['Student']} | G{s['Grade']} | {s['Subject']} | {s.get('Type','group')}")
    return "\n".join(lines)

def format_attendance(attendance: dict, students: list, excluded: dict) -> str:
    today = datetime.now().strftime("%Y-%m-%d")
    day_att = attendance.get(today, {})
    day_exc = excluded.get(today, [])

    lines = [f"📋 {today} 选课情况："]
    for slot in TIME_SLOTS:
        names = day_att.get(slot, [])
        if names:
            lines.append(f"  [{slot}] {len(names)}人：{', '.join(names)}")
        else:
            lines.append(f"  [{slot}] （未选）")
    if day_exc:
        lines.append(f"\n🚫 排除老师：{', '.join(day_exc)}")
    return "\n".join(lines)

def format_schedule_result(result_data: dict, time_text: str) -> str:
    """格式化排课结果"""
    lines = [f"📋 [{time_text}] 排课结果："]
    for c in result_data["result"]:
        lines.append(f"  {c['教室']} | {c['老师']} | {c['科目']} | {c['学生名单']} | {c['type']}")
    if result_data["failNames"]:
        lines.append(f"\n⚠️ 未排入：{', '.join(result_data['failNames'])}")
    if result_data["idleTeachers"]:
        lines.append(f"📋 空闲老师：{', '.join(result_data['idleTeachers'])}")
    return "\n".join(lines)

def format_day_schedule(slots_data: dict, date: str) -> str:
    """格式化整天的排课"""
    if date not in slots_data:
        return f"📭 {date} 没有排课记录"

    day = slots_data[date]
    # 按老师汇总
    teacher_map = {}
    for tk in sorted(day.keys()):
        for c in day[tk].get("classes", []):
            t = c.get("老师", "?")
            teacher_map.setdefault(t, []).append({**c, "time": tk})

    lines = [f"📋 {date} 总课表："]
    for teacher in sorted(teacher_map.keys()):
        entries = teacher_map[teacher]
        rooms = list(set(e["教室"] for e in entries))
        for e in entries:
            lines.append(f"  {e['time']} | {e['教室']} | {teacher} | {e.get('科目','?')} | {e.get('学生名单','?')}")
    return "\n".join(lines)

def build_context(schedule_data: dict) -> str:
    """构建给 AI 的精简上下文"""
    students = schedule_data.get("students", [])
    teachers = schedule_data.get("teachers", [])
    slots = schedule_data.get("slots", {})
    attendance = schedule_data.get("attendance", {})
    excluded = schedule_data.get("excluded_teachers", {})
    today = datetime.now().strftime("%Y-%m-%d")

    ctx = [f"当前日期: {today}"]
    ctx.append(f"\n【学生 {len(students)} 人】")
    for s in students[:20]:
        ctx.append(f"  {s['Student']} | G{s['Grade']} | {s['Subject']} | {s.get('Type','group')}")
    if len(students) > 20:
        ctx.append(f"  ...共{len(students)}人")

    ctx.append(f"\n【老师 {len(teachers)} 人】")
    for t in teachers[:10]:
        subs = ", ".join(f"{x.get('subject','?')}" if isinstance(x,dict) else str(x) for x in t.get("subjects", []))
        ctx.append(f"  {t.get('name','?')} | {t.get('level','mid')} | {subs}")

    ctx.append(f"\n【今日考勤】")
    day_att = attendance.get(today, {})
    day_exc = excluded.get(today, [])
    for slot in TIME_SLOTS:
        names = day_att.get(slot, [])
        ctx.append(f"  [{slot}] {'、'.join(names) if names else '（未选）'}")
    if day_exc:
        ctx.append(f"  排除老师: {'、'.join(day_exc)}")

    ctx.append(f"\n【已排课: {len(slots)} 天】")
    return "\n".join(ctx)

# ──────────────────────── 操作执行 ────────────────────────

def execute_operation(schedule_data: dict, op: dict) -> tuple[str, dict]:
    action = op.get("action", "")
    slots = schedule_data.setdefault("slots", {})
    students = schedule_data.get("students", [])
    teachers = schedule_data.get("teachers", [])
    attendance = schedule_data.setdefault("attendance", {})
    excluded = schedule_data.setdefault("excluded_teachers", {})
    today = datetime.now().strftime("%Y-%m-%d")

    # ── list_students ──
    if action == "list_students":
        if not students:
            return "📭 还没有学生数据。请在电脑网页导入 Excel。", schedule_data
        return f"📋 学生列表（共 {len(students)} 人）：\n\n" + format_student_list(students), schedule_data

    # ── show_attendance ──
    if action == "show_attendance":
        return format_attendance(attendance, students, excluded), schedule_data

    # ── select_students ──
    if action == "select_students":
        slot_time = op.get("time", "")
        student_names_str = op.get("student_names", "")
        date = op.get("date", today)

        if slot_time not in TIME_SLOTS:
            return f"⚠️ 无效时段。可选：{' / '.join(TIME_SLOTS)}", schedule_data

        if student_names_str in ("全部", "所有人", "all", "所有"):
            # 排除的老师不排
            day_exc = excluded.get(date, [])
            selected = [s['Student'] for s in students if s['Student'] not in day_exc]
        else:
            selected = _parse_names(student_names_str, students)

        if not selected:
            return "⚠️ 没有识别到有效学生名", schedule_data

        attendance.setdefault(date, {})[slot_time] = selected
        msg = f"✅ {date} [{slot_time}] 已选 {len(selected)} 人：{', '.join(selected)}"
        return msg + "\n\n" + format_attendance(attendance, students, excluded), schedule_data

    # ── clear_slot ──
    if action == "clear_slot":
        slot_time = op.get("time", "")
        date = op.get("date", today)
        if date in attendance and slot_time in attendance[date]:
            del attendance[date][slot_time]
            return f"✅ 已清空 [{slot_time}]\n\n" + format_attendance(attendance, students, excluded), schedule_data
        return f"⚠️ [{slot_time}] 没有学生", schedule_data

    # ── exclude_teacher ──
    if action == "exclude_teacher":
        name = op.get("teacher_name", "").strip()
        date = op.get("date", today)
        if not name:
            return "⚠️ 请提供老师名", schedule_data
        exc = excluded.setdefault(date, [])
        if name not in exc:
            exc.append(name)
            return f"✅ 已排除 {name}\n\n" + format_attendance(attendance, students, excluded), schedule_data
        return f"⚠️ {name} 已被排除", schedule_data

    # ── include_teacher ──
    if action == "include_teacher":
        name = op.get("teacher_name", "").strip()
        date = op.get("date", today)
        exc = excluded.get(date, [])
        if name in exc:
            exc.remove(name)
            return f"✅ 已恢复 {name}\n\n" + format_attendance(attendance, students, excluded), schedule_data
        return f"⚠️ {name} 不在排除列表中", schedule_data

    # ── auto_schedule ──
    if action == "auto_schedule":
        time_slot = op.get("time", "")
        date = op.get("date", today)
        subject_filter = op.get("subject", "")  # 可选：只排某科目

        if time_slot and time_slot not in TIME_SLOTS:
            return f"⚠️ 时段必须是: {' / '.join(TIME_SLOTS)}", schedule_data

        day_att = attendance.get(date, {})
        day_exc = excluded.get(date, [])

        # 确定要排的时段
        slots_to_schedule = [time_slot] if time_slot else [s for s in TIME_SLOTS if day_att.get(s)]

        if not any(day_att.get(s) for s in slots_to_schedule):
            return "⚠️ 还没有选学生。请先用「选学生」选好谁来上课。", schedule_data

        if not teachers:
            return "⚠️ 还没有老师数据。请在电脑网页导入。", schedule_data

        all_results = []
        total_failed = []
        total_idle = []

        for slot in slots_to_schedule:
            names = day_att.get(slot, [])
            if not names:
                continue

            # 筛选该时段的学生
            slot_students = [s for s in students if s['Student'] in names]

            # 如果指定了科目，只保留选该科目的学生
            if subject_filter:
                slot_students = [s for s in slot_students
                    if subject_filter.lower() in [x.strip().lower() for x in (s.get("Subject","")+"").split("/")]]

            if not slot_students:
                all_results.append(f"[{slot}] 没有符合条件的学生")
                continue

            # 排除的老师
            exc_set = set(day_exc)

            r = schedule_slot(slot_students, teachers, slot,
                              max_class_student=4, excluded_teachers=exc_set)

            # 保存到 slots 数据
            slots.setdefault(date, {})[slot] = {
                "classes": r["result"],
                "failCount": r["failCount"],
                "idleTeachers": r["idleTeachers"],
                "placedCount": r["placedCount"],
            }

            all_results.append(format_schedule_result(r, slot))
            total_failed.extend(r["failNames"])
            total_idle = list(set(total_idle + r["idleTeachers"]))

        # 汇总消息
        summary = f"🚀 {date} 排课完成！\n\n" + "\n\n".join(all_results)
        if total_failed:
            summary += f"\n\n⚠️ 总共 {len(total_failed)} 人未排入"
        return summary, schedule_data

    # ── add_student ──
    if action == "add_student":
        name = op.get("student_name", "").strip()
        grade = int(op.get("grade", 0))
        subject = op.get("subject", "").strip()
        stu_type = (op.get("student_type", "group") or "group").strip()

        if not name or not grade or not subject:
            return "⚠️ 格式：添加学生：姓名, G年级, 科目/科目, 1v1或group\n例：添加学生：张三, G5, 数学/物理, 1v1", schedule_data
        if any(s['Student'] == name for s in students):
            return f"⚠️ 「{name}」已存在", schedule_data

        students.append({
            "Student": name, "Grade": int(grade), "Subject": subject,
            "Type": stu_type if stu_type in ("1v1", "group") else "group"
        })
        return f"✅ 已添加：{name} | G{grade} | {subject} | {stu_type}", schedule_data

    # ── remove_student ──
    if action == "remove_student":
        name = op.get("student_name", "").strip()
        for i, s in enumerate(students):
            if s['Student'] == name:
                del students[i]
                for d in attendance:
                    for slot in attendance[d]:
                        if name in attendance[d][slot]:
                            attendance[d][slot].remove(name)
                for d in slots:
                    for t in list(slots[d].keys()):
                        cls_list = slots[d][t].get("classes", [])
                        for c in list(cls_list):
                            names_in = [x.strip() for x in c.get("学生名单","").split("、")]
                            if name in names_in:
                                new_names = [x for x in names_in if x != name]
                                if new_names: c["学生名单"] = "、".join(new_names)
                                else: cls_list.remove(c)
                        slots[d][t]["classes"] = [c for c in cls_list if c.get("学生名单")]
                        if not slots[d][t]["classes"]:
                            del slots[d][t]
                    if not slots[d]: del slots[d]
                return f"✅ 已删除「{name}」", schedule_data
        return f"⚠️ 未找到「{name}」", schedule_data

    # ── 保留原有操作（不变）──
    if action == "query_student":
        name = op.get("student_name", "")
        found = _find_student_slots(schedule_data, name)
        if not found:
            return f"📭 {name} 无排课记录", schedule_data
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
        if not found: return f"📭 {name} 无排课", schedule_data
        lines = [f"📋 {name} 的课表："]
        for f in found:
            lines.append(f"  • {f['date']} {f['time']} | {f.get('科目','?')} | {f.get('学生名单','?')} | {f.get('教室','?')}")
        return "\n".join(lines), schedule_data

    if action == "query_day":
        date = op.get("date", today)
        return format_day_schedule(slots, date), schedule_data

    if action == "remove_class":
        student = op.get("student_name", "")
        date = op.get("date", today)
        time_key = op.get("time", "")
        subject = op.get("subject", "")
        if date in slots and time_key in slots[date]:
            classes = slots[date][time_key].get("classes", [])
            kept = []
            for c in classes:
                names = [s.strip() for s in c.get("学生名单","").split("、")]
                if student in names:
                    if subject and c.get("科目") != subject:
                        kept.append(c); continue
                    new_names = [s for s in names if s != student]
                    if new_names:
                        c["学生名单"] = "、".join(new_names); kept.append(c)
                else:
                    kept.append(c)
            slots[date][time_key]["classes"] = kept
            if not kept:
                del slots[date][time_key]
                if not slots[date]: del slots[date]
            return f"✅ 已取消 {student} 在 {date} {time_key} 的{subject or '课'}", schedule_data
        return "⚠️ 未找到匹配的课", schedule_data

    if action == "add_class":
        student = op.get("student_name", "")
        date = op.get("date", today)
        time_key = op.get("time", "")
        subject = op.get("subject", "")
        teacher = op.get("teacher_name", "")
        if not all([student, date, time_key, subject]):
            return "⚠️ 信息不足", schedule_data
        slots.setdefault(date, {}).setdefault(time_key, {"classes": []})
        for c in slots[date][time_key]["classes"]:
            if student in [s.strip() for s in c.get("学生名单","").split("、")]:
                return f"⚠️ {student} 已有课", schedule_data
        used = {c.get("教室") for c in slots[date][time_key]["classes"]}
        room = next((f"Room {i}" for i in range(1,31) if f"Room {i}" not in used), "Room 1")
        slots[date][time_key]["classes"].append({
            "学生名单": student, "老师": teacher or "待分配",
            "科目": subject, "教室": room, "时段": time_key,
        })
        return f"✅ 已添加：{student} | {subject} | {date} {time_key}", schedule_data

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
                if student in [s.strip() for s in c.get("学生名单","").split("、")]:
                    found = c; break
        if not found:
            return f"⚠️ 未找到", schedule_data
        old_names = [s.strip() for s in found.get("学生名单","").split("、")]
        new_old = [s for s in old_names if s != student]
        if new_old: found["学生名单"] = "、".join(new_old)
        else:
            slots[from_date][from_time]["classes"].remove(found)
            if not slots[from_date][from_time]["classes"]:
                del slots[from_date][from_time]
                if not slots[from_date]: del slots[from_date]
        slots.setdefault(to_date, {}).setdefault(to_time, {"classes": []})
        used = {c.get("教室") for c in slots[to_date][to_time]["classes"]}
        room = found.get("教室") if found.get("教室") not in used else next((f"Room {i}" for i in range(1,31) if f"Room {i}" not in used), "Room 1")
        slots[to_date][to_time]["classes"].append({
            "学生名单": student, "老师": found.get("老师","待分配"),
            "科目": found.get("科目",""), "教室": room, "时段": to_time,
        })
        return f"✅ 已移动：{student} {found.get('科目','')} | {from_date} {from_time} → {to_date} {to_time}", schedule_data

    if action == "chat":
        return op.get("message", "好的👌"), schedule_data

    return f"⚠️ 不支持: {action}", schedule_data

def _parse_names(text: str, students: list) -> list:
    """解析学生名（支持编号、逗号、顿号）"""
    known = {s['Student']: s['Student'] for s in students}
    known_lower = {k.lower(): k for k in known}
    parts = text.replace("，", ",").replace("、", ",").replace("\n", ",").split(",")
    parts = [p.strip() for p in parts if p.strip()]
    result = []
    for p in parts:
        try:
            idx = int(p) - 1
            if 0 <= idx < len(students):
                result.append(students[idx]['Student'])
                continue
        except: pass
        if p in known: result.append(p); continue
        match = known_lower.get(p.lower())
        if match: result.append(match); continue
        matches = [v for k, v in known_lower.items() if p.lower() in k]
        if len(matches) == 1: result.append(matches[0])
    seen = set()
    return [x for x in result if not (x in seen or seen.add(x))]

def _find_student_slots(schedule_data: dict, name: str) -> list:
    results = []
    for d, day_data in schedule_data.get("slots", {}).items():
        for tk, slot in day_data.items():
            for c in slot.get("classes", []):
                if name in [s.strip() for s in c.get("学生名单","").split("、")]:
                    results.append({**c, "date": d, "time": tk})
    return results

# ──────────────────────── Claude 解析 ────────────────────────

SYSTEM_PROMPT = """你是 UTU 排课系统的 AI 助手。学生名和老师名通常用英文（如 David, Tere）。

核心功能流程：

━━━ ① 选学生 ━━━
"9:30-11:30: David, Yufei, Eva" → select_students
"全部学生在12:30-14:30" → select_students with student_names="全部"
"14:30-16:30: 1,3,5,7" → select_students（支持编号）
"查看考勤" → show_attendance
"清空 9:30-11:30" → clear_slot

━━━ ② 排除/恢复老师 ━━━
默认所有老师可用。用户可以说：
"王老师不排" / "排除 Tere" → exclude_teacher
"恢复 Tere" / "让王老师也排" → include_teacher

━━━ ③ 自动排课 ━━━
"自动排课" / "排课" / "开始排课" → auto_schedule（对所有已选时段排课）
"只排 9:30-11:30" → auto_schedule with time="09:30-11:30"
"排英语课" → auto_schedule with subject="英语相关科目"
"给今天来的人排课" → auto_schedule

━━━ ④ 学生管理 ━━━
"添加学生：Zhang, G5, Math, 1v1" → add_student
"删除 Zhang" → remove_student
"有哪些学生" → list_students

━━━ ⑤ 微调 ━━━
"查看 David 的课表" → query_student
"取消 David 今天的课" → remove_class
"查看今天课表" → query_day

时段固定为: 09:30-11:30 / 12:30-14:30 / 14:30-16:30 / 16:30-18:40"""

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
                    "clear_slot", "exclude_teacher", "include_teacher",
                    "auto_schedule", "add_student", "remove_student",
                    "query_student", "query_teacher", "query_day",
                    "add_class", "remove_class", "move_class", "chat"
                ]
            },
            "student_name":   {"type": "string"},
            "student_names":  {"type": "string", "description": "多个学生名，逗号分隔，支持编号如1,3,5。'全部'=所有学生"},
            "teacher_name":   {"type": "string"},
            "subject":        {"type": "string"},
            "student_type":   {"type": "string"},
            "grade":          {"type": "integer"},
            "date":           {"type": "string"},
            "time":           {"type": "string"},
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
        return {"action": "chat", "message": "🤖 未配置 API Key"}

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
        return {"action": "chat", "message": f"⚠️ 出错: {str(e)[:200]}"}

# ──────────────────────── API 路由 ────────────────────────

@app.route("/api/schedule", methods=["GET"])
def api_get_schedule():
    return jsonify(get_schedule())

@app.route("/api/schedule", methods=["POST"])
def api_save_schedule():
    data = request.get_json(force=True)
    existing = get_schedule()
    # 网页传上来的数据不覆盖考勤和排除信息
    data["attendance"] = existing.get("attendance", {})
    data["excluded_teachers"] = existing.get("excluded_teachers", {})
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

    # —— 记录所有请求到文件，方便排查 ——
    log_entry = {
        "time": datetime.now().isoformat(),
        "body_keys": list(body.keys()) if body else [],
        "schema": body.get("schema", ""),
        "header_type": body.get("header", {}).get("event_type", ""),
        "body_type": body.get("type", ""),
    }
    _write_request_log(log_entry)

    if body.get("type") == "url_verification":
        return jsonify({"challenge": body.get("challenge", "")})

    event = body.get("event", {})
    if not event:
        _write_request_log({"error": "no event field", "body": str(body)[:500]})
        return jsonify({"ok": True})

    # 多种方式提取 sender_id
    sender_id = (event.get("open_id", "") or
                 event.get("sender", {}).get("sender_id", {}).get("open_id", "") or
                 event.get("operator", {}).get("open_id", ""))

    _write_request_log({"sender_found": bool(sender_id), "sender_id": sender_id[:20] if sender_id else "NONE"})

    if not sender_id:
        _write_request_log({"error": "no sender_id", "event_keys": list(event.keys())})
        return jsonify({"ok": True})

    # ── 处理卡片按钮点击 ──
    header_type = body.get("header", {}).get("event_type", "")
    schema = body.get("schema", "")
    is_card_event = (header_type == "card.action.trigger" or schema.startswith("2."))

    _write_request_log({"is_card_event": is_card_event, "has_message": "message" in event})

    if is_card_event:
        _write_request_log({"card_action": str(event.get("action", {}))[:300]})
        return _handle_card_action(sender_id, event.get("action", {}))

    # ── 处理消息文字 ──
    msg = event.get("message", {})
    if msg.get("message_type") != "text":
        _write_request_log({"skipped": f"msg_type={msg.get('message_type')}"})
        return jsonify({"ok": True})

    try:
        content = json.loads(msg.get("content", "{}"))
        user_msg = content.get("text", "").strip()
    except:
        _write_request_log({"error": "failed to parse msg content"})
        return jsonify({"ok": True})

    if not user_msg:
        return jsonify({"ok": True})
    if user_msg.startswith("@"):
        parts = user_msg.split(" ", 1)
        user_msg = parts[1] if len(parts) > 1 else ""

    _write_request_log({"user_msg": user_msg[:100]})
    return _handle_text_msg(sender_id, user_msg)

def _write_request_log(entry: dict):
    """写入请求日志"""
    try:
        log_path = DATA_DIR / "webhook_log.jsonl"
        with open(log_path, "a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except:
        pass

def _handle_card_action(sender_id, action):
    """处理卡片按钮点击"""
    try:
        value = json.loads(action.get("value", "{}"))
    except:
        value = {}

    card_action = value.get("action", "")
    slot = value.get("slot", "")
    mode = value.get("mode", "")

    sd = get_schedule()
    today = datetime.now().strftime("%Y-%m-%d")
    attendance = sd.get("attendance", {})
    students = sd.get("students", [])

    # ── 显示主菜单 ──
    if card_action == "show_main":
        send_feishu_card(sender_id, build_main_card(attendance, sd.get("excluded_teachers", {}), students))
        return jsonify({"toast": {"type": "info", "content": "已刷新"}})

    # ── 开始选学生：发送学生编号列表 ──
    if card_action == "start_select":
        user_sessions[sender_id] = {"state": "selecting", "current_slot": slot}
        # 发送学生列表文本（不用卡片，避免嵌套复杂）
        reply = f"📋 请回复 **[{slot}]** 的学生编号：\n\n" + format_student_list(students)
        reply += f"\n\n💡 输入编号（逗号分隔）如：1,3,5,7  或输入「全部」"
        send_feishu_msg(sender_id, reply)
        return jsonify({"toast": {"type": "info", "content": f"为 {slot} 选学生"}})

    # ── 快速操作：全选/清空 ──
    if card_action == "quick_select":
        day_att = attendance.setdefault(today, {})
        if mode == "all":
            day_att[slot] = [s['Student'] for s in students]
            reply = f"✅ [{slot}] 已选全部 {len(students)} 人"
        elif mode == "clear":
            if slot in day_att:
                del day_att[slot]
            reply = f"✅ [{slot}] 已清空"
        else:
            reply = "⚠️ 未知操作"
        save_schedule(sd)
        send_feishu_msg(sender_id, reply)

        # 刷新选学生卡片
        send_feishu_card(sender_id, build_select_card(slot, sd["students"],
            sd.get("attendance", {}), sd.get("excluded_teachers", {})))
        return jsonify({"toast": {"type": "success", "content": reply}})

    # ── 确认选择 → 通过文字回复 ──
    if card_action == "confirm_select":
        user_sessions[sender_id] = {"state": "confirming", "current_slot": slot}
        send_feishu_msg(sender_id,
            f"📋 请回复 **[{slot}]** 的学生名单：\n\n"
            + format_student_list(students)
            + f"\n\n输入学生**编号**或**名字**（逗号分隔），例如：1,3,5,7\n或输入「全部」选所有学生"
        )
        return jsonify({"toast": {"type": "info", "content": f"请在聊天框回复 {slot} 的学生"}})

    # ── 查看学生列表 ──
    if card_action == "list_students":
        reply = f"📋 学生列表（共 {len(students)} 人）：\n\n" + format_student_list(students) if students else "📭 暂无学生"
        send_feishu_msg(sender_id, reply)
        return jsonify({"toast": {"type": "info", "content": "已发送学生列表"}})

    # ── 自动排课 ──
    if card_action == "auto_schedule":
        send_feishu_msg(sender_id, "🚀 正在排课...")
        _handle_text_msg(sender_id, "自动排课")
        return jsonify({"toast": {"type": "info", "content": "正在排课..."}})

    # ── 查看课表 ──
    if card_action == "query_day":
        _handle_text_msg(sender_id, "查看今天课表")
        return jsonify({"toast": {"type": "info", "content": "查看课表"}})

    return jsonify({"toast": {"type": "error", "content": f"未知操作: {card_action}"}})

def _handle_text_msg(sender_id, user_msg):
    """处理文字消息"""
    try:
        return _do_handle_text(sender_id, user_msg)
    except Exception as e:
        import traceback
        err_msg = f"⚠️ 服务器内部错误: {str(e)[:200]}"
        print(f"[错误] {traceback.format_exc()}")
        send_feishu_msg(sender_id, err_msg)
        return jsonify({"ok": True})

def _do_handle_text(sender_id, user_msg):
    sd = get_schedule()
    today = datetime.now().strftime("%Y-%m-%d")

    # ── 如果用户在选学生流程中，智能解析 ──
    session = user_sessions.get(sender_id, {})
    if session.get("state") in ("selecting", "confirming") and session.get("current_slot"):
        slot = session["current_slot"]
        # 用户可能回复学生编号或名字
        students = sd.get("students", [])
        selected = _parse_names(user_msg, students)

        if selected:
            sd.setdefault("attendance", {}).setdefault(today, {})[slot] = selected
            save_schedule(sd)
            user_sessions.pop(sender_id, None)
            reply = f"✅ [{slot}] 已选 {len(selected)} 人：{', '.join(selected)}\n\n" + \
                    format_attendance(sd.get("attendance", {}), students, sd.get("excluded_teachers", {}))
            send_feishu_msg(sender_id, reply)
            # 也刷新卡片
            send_feishu_card(sender_id, build_main_card(sd.get("attendance", {}), sd.get("excluded_teachers", {}), students))
            return jsonify({"ok": True})
        # 不是学生名单，当作普通指令处理（fall through）

    # ── 快捷指令：直接发卡片 ──
    if user_msg.strip() in ("菜单", "选学生", "开始", "排课", "排课菜单"):
        send_feishu_card(sender_id, build_main_card(
            sd.get("attendance", {}), sd.get("excluded_teachers", {}), sd.get("students", [])))
        return jsonify({"ok": True})

    # ── 正常 AI 处理 ──
    print(f"[飞书] {sender_id[:8]}... 说: {user_msg[:100]}")
    op = parse_command(user_msg, sd)
    reply, updated = execute_operation(sd, op)
    save_schedule(updated)

    if len(reply) > 4500:
        reply = reply[:4500] + "\n\n...（截断）"

    send_feishu_msg(sender_id, reply)

    # 如果有考勤/排课变化，刷新主卡片
    if op.get("action") in ("select_students", "auto_schedule", "exclude_teacher",
                             "include_teacher", "add_student", "remove_student", "clear_slot"):
        attendance = updated.get("attendance", {})
        students = updated.get("students", [])
        excluded = updated.get("excluded_teachers", {})
        send_feishu_card(sender_id, build_main_card(attendance, excluded, students))

    return jsonify({"ok": True})

# ──────────────────────── 启动 ────────────────────────

@app.route("/debug/logs", methods=["GET"])
def debug_logs():
    """查看最近的 webhook 请求日志"""
    log_path = DATA_DIR / "webhook_log.jsonl"
    if not log_path.exists():
        return jsonify({"logs": [], "msg": "还没有任何请求"})
    lines = log_path.read_text().strip().split("\n")[-50:]  # 最近50条
    return jsonify({"logs": [json.loads(l) for l in lines if l]})

@app.route("/", methods=["GET"])
def health():
    sd = get_schedule()
    return jsonify({
        "status": "ok",
        "service": "UTU 排课系统",
        "feishu": bool(FEISHU_APP_ID and FEISHU_APP_SECRET),
        "claude": bool(ANTHROPIC_API_KEY),
        "students": len(sd.get("students", [])),
        "teachers": len(sd.get("teachers", [])),
        "slots": len(sd.get("slots", {})),
    })

if __name__ == "__main__":
    print("🚀 UTU 排课 AI 助手启动中...")
    print(f"{'✅' if FEISHU_APP_ID else '⚠️'} 飞书: {'已配置' if FEISHU_APP_ID else '未配置'}")
    print(f"{'✅' if ANTHROPIC_API_KEY else '⚠️'} Claude: {'已配置' if ANTHROPIC_API_KEY else '未配置'}")
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)
