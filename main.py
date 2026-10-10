import os
import io
import json
import requests
import speech_recognition as sr
from pydub import AudioSegment
from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import PlainTextResponse

app = FastAPI()

# כתובת ה-Web App של Google Sheets
GOOGLE_SCRIPT_URL = os.getenv("GOOGLE_SCRIPT_URL", "https://script.google.com/macros/s/AKfycbzLJrs2r6AsceENCsqbB1lLdo6jrIxErSNflNq-IaifcNESqyV3IgYG7SAjTy5L2RCjag/exec")

# מטמון בזיכרון מהיר (ללא שיהוי בטלפון)
cache_contacts = []   # [{"name": "...", "phone": "..."}]
cache_personal = []   # [{"owner": "...", "name": "...", "phone": "..."}]
sessions = {}

def sync_from_sheets():
    """טעינת כל הנתונים מהגיליון לזיכרון של השרת"""
    global cache_contacts, cache_personal
    try:
        res = requests.get(f"{GOOGLE_SCRIPT_URL}?action=getAll", timeout=15)
        if res.status_code == 200:
            data = res.json()
            cache_contacts = data.get("contacts", [])
            cache_personal = data.get("personal", [])
            print(f"Loaded {len(cache_contacts)} contacts and {len(cache_personal)} personal items.")
    except Exception as e:
        print(f"Error syncing from Google Sheets: {e}")

@app.on_event("startup")
def startup():
    sync_from_sheets()

@app.get("/refresh")
def manual_refresh():
    """קריאה לכתובת זו מרעננת את הנתונים מ-Google Sheets מיד לאחר שהדבקת אקסל"""
    sync_from_sheets()
    return {"status": "ok", "total_contacts": len(cache_contacts)}

# פונקציות לעדכון Google Sheets ברקע (אינן מעכבות את המשתמש בקו)
def post_to_sheets(payload: dict):
    try:
        requests.post(GOOGLE_SCRIPT_URL, json=payload, timeout=10)
    except Exception as e:
        print(f"Error posting to sheet: {e}")

def get_session(phone: str):
    if phone not in sessions:
        sessions[phone] = {
            "search_results": [],
            "current_index": 0,
            "temp_name": "",
            "mode": "general"
        }
    return sessions[phone]

def transcribe_audio_from_url(audio_url: str) -> str:
    """הורדת הקלטה מימות המשיח והמרה לטקסט דרך גוגל"""
    try:
        r = requests.get(audio_url, timeout=10)
        sound = AudioSegment.from_file(io.BytesIO(r.content))
        sound = sound.set_frame_rate(16000).set_channels(1)
        wav_io = io.BytesIO()
        sound.export(wav_io, format="wav")
        wav_io.seek(0)

        recognizer = sr.Recognizer()
        with sr.AudioFile(wav_io) as source:
            recorded = recognizer.record(source)
            text = recognizer.recognize_google(recorded, language="he-IL")
            return text.strip()
    except Exception as e:
        print(f"Audio recognition error: {e}")
        return ""

# ==================== ניתוב שיחה ראשי (ימות המשיח) ====================

@app.api_route("/ivr", methods=["GET", "POST"])
async def ivr_endpoint(request: Request, background_tasks: BackgroundTasks):
    if request.method == "POST":
        params = dict(await request.form())
    else:
        params = dict(request.query_params)

    caller_phone = params.get("ApiPhone", "")
    action = params.get("action", "main_menu")
    val = params.get("val", "")
    sess = get_session(caller_phone)

    # ------------------ תפריט ראשי ------------------
    if action == "main_menu":
        if not val:
            return PlainTextResponse(
                "read=t-שלום וברכה. לחיפוש בספר הטלפונים הקישו 1. "
                "לאזור אישי הקישו 2. "
                "לרישום מספר חדש הקישו 3. "
                "להשארת הודעה למנהל הקישו 4.=val,no,1,1,7,Digits,,&action=main_menu"
            )
        if val == "1":
            return PlainTextResponse("id_list_message=&action=menu_search")
        elif val == "2":
            return PlainTextResponse("id_list_message=&action=personal_zone_check")
        elif val == "3":
            return PlainTextResponse("id_list_message=&action=register_start")
        elif val == "4":
            return PlainTextResponse("go_to_folder=/7/1")
        else:
            return PlainTextResponse("id_list_message=t-בחירה שגויה&action=main_menu")

    # ------------------ שלוחה 1: חיפוש שם ------------------
    if action == "menu_search":
        if not val:
            return PlainTextResponse(
                "read=t-לחיפוש קולי הקישו 1, לחיפוש בהקלדה הקישו 2.=val,no,1,1,7,Digits,,&action=menu_search"
            )
        if val == "1":
            return PlainTextResponse(
                "read=t-נא לומר את השם לחיפוש לאחר הצליל ולסיום הקישו סולמית.=val,record,0,0,10,File,,&action=search_voice_record"
            )
        elif val == "2":
            return PlainTextResponse(
                "read=t-נא להקליד את השם לחיפוש וסולמית לסיום.=val,no,1,15,7,HebrewTap,,&action=search_tap_process"
            )

    if action == "search_voice_record":
        record_url = params.get("val") or params.get("ApiRecordPath") or ""
        text = transcribe_audio_from_url(record_url)
        if not text:
            return PlainTextResponse("id_list_message=t-השם לא נקלט.&action=menu_search")
        sess["temp_name"] = text
        return PlainTextResponse(
            f"read=t-חיפשת את: {text}. לאישור הקישו 1, להקלטה חוזרת 2.=val,no,1,1,7,Digits,,&action=search_voice_confirm"
        )

    if action == "search_voice_confirm":
        if val == "1":
            return execute_search(sess["temp_name"], caller_phone, mode="general")
        else:
            return PlainTextResponse("id_list_message=&action=menu_search&val=1")

    if action == "search_tap_process":
        return execute_search(val, caller_phone, mode="general")

    # ------------------ ניווט תוצאות ------------------
    if action == "browse_results":
        results = sess["search_results"]
        idx = sess["current_index"]

        if not results or idx < 0 or idx >= len(results):
            return PlainTextResponse("id_list_message=t-חזרה לתפריט ראשי.&action=main_menu")

        contact = results[idx]
        name, phone = contact["name"], contact["phone"]

        if val == "0":
            return play_contact_menu(name, phone, sess["mode"])
        elif val == "1":
            if idx + 1 < len(results):
                sess["current_index"] += 1
            else:
                return PlainTextResponse("id_list_message=t-הגעת לסוף התוצאות.&action=browse_results&val=0")
            nxt = results[sess["current_index"]]
            return play_contact_menu(nxt["name"], nxt["phone"], sess["mode"])
        elif val == "2":
            if idx - 1 >= 0:
                sess["current_index"] -= 1
            else:
                return PlainTextResponse("id_list_message=t-הגעת לתוצאה הראשונה.&action=browse_results&val=0")
            prv = results[sess["current_index"]]
            return play_contact_menu(prv["name"], prv["phone"], sess["mode"])
        elif val == "3":
            return PlainTextResponse("id_list_message=&action=menu_search")
        elif val == "4":
            return PlainTextResponse(f"routing={phone}")
        elif val == "5":
            if sess["mode"] == "general":
                # שמירה מיידית בזיכרון
                cache_personal.append({"owner": caller_phone, "name": name, "phone": phone})
                # סנכרון לשיטס ברקע
                background_tasks.add_task(
                    post_to_sheets,
                    {"action": "addPersonal", "owner": caller_phone, "name": name, "phone": phone}
                )
                return PlainTextResponse("id_list_message=t-המספר נשמר באלפון האישי בהצלחה.&action=browse_results&val=0")
            else:
                return play_contact_menu(name, phone, sess["mode"])
        elif val == "6":
            return PlainTextResponse("id_list_message=&action=main_menu")
        else:
            return play_contact_menu(name, phone, sess["mode"])

    # ------------------ שלוחה 2: אזור אישי ------------------
    if action == "personal_zone_check":
        matched = next((c for c in cache_contacts if c["phone"] == caller_phone), None)
        if not matched:
            return PlainTextResponse(
                "id_list_message=t-מספר הטלפון אינו מופיע במאגר. ניתן להירשם בשלוחה 3.&action=main_menu"
            )

        sess["user_name"] = matched["name"]
        return PlainTextResponse(
            "read=t-שלום. לשמיעת הפרטים האישיים הקישו 1. "
            "לשינוי הפרטים הקישו 2. "
            "לאלפון אישי הקישו 3.=val,no,1,1,7,Digits,,&action=personal_zone_menu"
        )

    if action == "personal_zone_menu":
        if val == "1":
            u_name = sess.get("user_name", "")
            return PlainTextResponse(
                f"id_list_message=t-שמך במערכת הוא: {u_name}. ומספר הטלפון הוא: {caller_phone}.&action=personal_zone_check"
            )
        elif val == "2":
            # מעבר לרישום/עדכון
            sess["update_mode"] = True
            return PlainTextResponse("id_list_message=&action=register_start")
        elif val == "3":
            # אלפון אישי
            p_list = [c for c in cache_personal if c["owner"] == caller_phone]
            if not p_list:
                return PlainTextResponse("id_list_message=t-האלפון האישי שלך ריק.&action=personal_zone_check")

            sess["search_results"] = p_list
            sess["current_index"] = 0
            sess["mode"] = "personal"
            first = p_list[0]
            msg = f"id_list_message=t-נמצאו {len(p_list)} אנשי קשר באלפון האישי.&"
            return PlainTextResponse(msg + get_contact_read_string(first["name"], first["phone"], mode="personal"))

    # ------------------ שלוחה 3: רישום / שינוי פרטים ------------------
    if action == "register_start":
        return PlainTextResponse(
            "read=t-נא לומר את שמך המלא לאחר הצליל ולסיום הקישו סולמית.=val,record,0,0,10,File,,&action=register_voice_name"
        )

    if action == "register_voice_name":
        record_url = params.get("val") or params.get("ApiRecordPath") or ""
        text = transcribe_audio_from_url(record_url)
        if not text:
            return PlainTextResponse("id_list_message=t-השם לא נקלט. נסה שוב.&action=register_start")
        sess["temp_name"] = text
        return PlainTextResponse(
            f"read=t-אמרת: {text}. לאישור הקישו 1, להקלטה חוזרת 2.=val,no,1,1,7,Digits,,&action=register_confirm_name"
        )

    if action == "register_confirm_name":
        if val == "1":
            # אם הוא בא מעדכון פרטים באיזור אישי, משתמשים ישירות במספר שלו
            if sess.get("update_mode"):
                new_name = sess.get("temp_name")
                sess["update_mode"] = False
                
                # עדכון בזיכרון
                for c in cache_contacts:
                    if c["phone"] == caller_phone:
                        c["name"] = new_name
                        break
                
                # עדכון בשיטס ברקע
                background_tasks.add_task(
                    post_to_sheets,
                    {"action": "update", "name": new_name, "phone": caller_phone}
                )
                return PlainTextResponse(f"id_list_message=t-הפרטים עודכנו בהצלחה. השם החדש: {new_name}.&action=main_menu")

            return PlainTextResponse(
                "read=t-נא להקיש את מספר הטלפון וסולמית לסיום.=val,no,9,11,7,Digits,,&action=register_phone"
            )
        else:
            return PlainTextResponse("id_list_message=&action=register_start")

    if action == "register_phone":
        new_phone = val.strip()
        new_name = sess.get("temp_name")
        if len(new_phone) < 9:
            return PlainTextResponse("id_list_message=t-מספר טלפון שגוי.&action=register_phone")

        # עדכון מקומי בזיכרון מיד
        cache_contacts.append({"name": new_name, "phone": new_phone})

        # שמירה ב-Google Sheets ברקע
        background_tasks.add_task(
            post_to_sheets,
            {"action": "register", "name": new_name, "phone": new_phone}
        )

        return PlainTextResponse(
            f"id_list_message=t-נרשמת בהצלחה! השם: {new_name}, מספר: {new_phone}.&action=main_menu"
        )

    return PlainTextResponse("id_list_message=t-שגיאה בניתוב&action=main_menu")


# ==================== פונקציות עזר ====================

def execute_search(query: str, caller_phone: str, mode: str = "general"):
    matches = [c for c in cache_contacts if query in c["name"]]
    if not matches:
        return PlainTextResponse("id_list_message=t-לא נמצאו תוצאות לשם המבוקש.&action=menu_search")

    sess = get_session(caller_phone)
    sess["search_results"] = matches
    sess["current_index"] = 0
    sess["mode"] = mode

    first = matches[0]
    msg = f"id_list_message=t-נמצאו {len(matches)} תוצאות.&"
    return PlainTextResponse(msg + get_contact_read_string(first["name"], first["phone"], mode=mode))

def get_contact_read_string(name: str, phone: str, mode: str = "general") -> str:
    if mode == "general":
        menu = (
            f"t-תוצאה: {name}. מספר טלפון: {phone}. "
            "לשמיעה חוזרת 0. לתוצאה הבאה 1. לתוצאה קודמת 2. "
            "לחיפוש חדש 3. לחיוג 4. לשמירה באלפון האישי 5. ליציאה 6."
        )
    else:
        menu = (
            f"t-איש קשר: {name}. מספר טלפון: {phone}. "
            "לשמיעה חוזרת 0. לאיש הקשר הבא 1. לקודם 2. "
            "לחיפוש חדש 3. לחיוג 4. ליציאה 6."
        )
    return f"read={menu}=val,no,1,1,7,Digits,,&action=browse_results"

def play_contact_menu(name: str, phone: str, mode: str = "general"):
    return PlainTextResponse(get_contact_read_string(name, phone, mode=mode))
