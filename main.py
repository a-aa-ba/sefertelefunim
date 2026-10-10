import os
import io
import json
import requests
import speech_recognition as sr
from pydub import AudioSegment
from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import PlainTextResponse

app = FastAPI()

GOOGLE_SCRIPT_URL = os.getenv("GOOGLE_SCRIPT_URL", "https://script.google.com/macros/s/AKfycbzLJrs2r6AsceENCsqbB1lLdo6jrIxErSNflNq-IaifcNESqyV3IgYG7SAjTy5L2RCjag/exec")

cache_contacts = []
cache_personal = []

# זיכרון של מצב השיחה לכל מתקשר
user_states = {}

def sync_from_sheets():
    global cache_contacts, cache_personal
    try:
        res = requests.get(f"{GOOGLE_SCRIPT_URL}?action=getAll", timeout=15)
        if res.status_code == 200:
            data = res.json()
            cache_contacts = data.get("contacts", [])
            cache_personal = data.get("personal", [])
            print(f"Loaded {len(cache_contacts)} contacts.")
    except Exception as e:
        print(f"Error syncing sheets: {e}")

@app.on_event("startup")
def startup():
    sync_from_sheets()

@app.get("/refresh")
def manual_refresh():
    sync_from_sheets()
    return {"status": "ok", "total_contacts": len(cache_contacts)}

def post_to_sheets(payload: dict):
    try:
        requests.post(GOOGLE_SCRIPT_URL, json=payload, timeout=10)
    except Exception as e:
        print(f"Error posting: {e}")

def get_state(phone: str):
    if phone not in user_states:
        user_states[phone] = {
            "step": "main_menu",
            "search_results": [],
            "current_index": 0,
            "temp_name": "",
            "mode": "general"
        }
    return user_states[phone]

def transcribe_audio_from_url(audio_url: str) -> str:
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

# ==================== הראוט הראשי ====================

@app.api_route("/", methods=["GET", "POST"])
async def handle_ivr(request: Request, background_tasks: BackgroundTasks):
    params = dict(request.query_params)
    if request.method == "POST":
        form_data = dict(await request.form())
        params.update(form_data)

    # אם זו קריאת ניתוק שיחה
    if params.get("hangup") == "yes":
        caller = params.get("ApiPhone", "")
        if caller in user_states:
            del user_states[caller]
        return PlainTextResponse("ok")

    caller_phone = params.get("ApiPhone", "000")
    user = get_state(caller_phone)
    val = params.get("val", "").strip()

    # 1. כניסה ראשונית לשיחה
    if not val and user["step"] == "main_menu":
        return PlainTextResponse(
            "read=t-שלום וברכה. לחיפוש בספר הטלפונים הקישו 1. לאזור אישי הקישו 2. לרישום מספר חדש הקישו 3. להשארת הודעה הקישו 4.=val,no,1,1,7,Digits,,"
        )

    # 2. בחירה בתפריט הראשי
    if user["step"] == "main_menu":
        if val == "1":
            user["step"] = "menu_search"
            return PlainTextResponse(
                "read=t-לחיפוש קולי הקישו 1, לחיפוש בהקלדה הקישו 2.=val,no,1,1,7,Digits,,"
            )
        elif val == "2":
            user["step"] = "personal_check"
            # בדיקת קיום באיזור אישי
            matched = next((c for c in cache_contacts if c["phone"] == caller_phone), None)
            if not matched:
                user["step"] = "main_menu"
                return PlainTextResponse(
                    "id_list_message=t-מספר הטלפון אינו מופיע במאגר. ניתן להירשם בשלוחה 3.&"
                    "read=t-לחיפוש בספר הטלפונים הקישו 1. לאזור אישי הקישו 2. לרישום הקישו 3. להודעה הקישו 4.=val,no,1,1,7,Digits,,"
                )
            user["user_name"] = matched["name"]
            user["step"] = "personal_menu"
            return PlainTextResponse(
                "read=t-שלום. לשמיעת הפרטים האישיים הקישו 1. לשינוי הפרטים הקישו 2. לאלפון אישי הקישו 3.=val,no,1,1,7,Digits,,"
            )
        elif val == "3":
            user["step"] = "register_voice"
            return PlainTextResponse(
                "read=t-נא לומר את שמך לאחר הצליל ולסיום הקישו סולמית.=val,record,0,0,10,File,,"
            )
        elif val == "4":
            return PlainTextResponse("go_to_folder=/7/1")
        else:
            return PlainTextResponse(
                "read=t-בחירה שגויה. לחיפוש הקישו 1. לאזור אישי 2. לרישום 3. להודעה 4.=val,no,1,1,7,Digits,,"
            )

    # 3. שלוחה 1: חיפוש שם
    if user["step"] == "menu_search":
        if val == "1":
            user["step"] = "search_voice_record"
            return PlainTextResponse(
                "read=t-נא לומר את השם לחיפוש ולאחר מכן הקישו סולמית.=val,record,0,0,10,File,,"
            )
        elif val == "2":
            user["step"] = "search_tap"
            return PlainTextResponse(
                "read=t-נא להקליד את השם לחיפוש וסולמית לסיום.=val,no,1,15,7,HebrewTap,,"
            )

    # עיבוד הקלטת חיפוש
    if user["step"] == "search_voice_record":
        record_url = val or params.get("ApiRecordPath", "")
        text = transcribe_audio_from_url(record_url)
        if not text:
            user["step"] = "menu_search"
            return PlainTextResponse(
                "id_list_message=t-השם לא נקלט.&"
                "read=t-לחיפוש קולי הקישו 1, להקלדה הקישו 2.=val,no,1,1,7,Digits,,"
            )
        user["temp_name"] = text
        user["step"] = "confirm_voice_search"
        return PlainTextResponse(
            f"read=t-חיפשת את {text}. לאישור הקישו 1, להקלטה חוזרת 2.=val,no,1,1,7,Digits,,"
        )

    if user["step"] == "confirm_voice_search":
        if val == "1":
            return do_search(user["temp_name"], user, mode="general")
        else:
            user["step"] = "search_voice_record"
            return PlainTextResponse(
                "read=t-נא לומר את השם לחיפוש שוב וסולמית לסיום.=val,record,0,0,10,File,,"
            )

    if user["step"] == "search_tap":
        return do_search(val, user, mode="general")

    # ניווט תוצאות (0 עד 6)
    if user["step"] == "browse_results":
        res = user["search_results"]
        idx = user["current_index"]
        c = res[idx]

        if val == "0":
            return play_contact(c["name"], c["phone"], user["mode"])
        elif val == "1":
            if idx + 1 < len(res):
                user["current_index"] += 1
            nxt = res[user["current_index"]]
            return play_contact(nxt["name"], nxt["phone"], user["mode"])
        elif val == "2":
            if idx - 1 >= 0:
                user["current_index"] -= 1
            prv = res[user["current_index"]]
            return play_contact(prv["name"], prv["phone"], user["mode"])
        elif val == "3":
            user["step"] = "menu_search"
            return PlainTextResponse("read=t-לחיפוש קולי 1, להקלדה 2.=val,no,1,1,7,Digits,,")
        elif val == "4":
            return PlainTextResponse(f"routing={c['phone']}")
        elif val == "5":
            if user["mode"] == "general":
                cache_personal.append({"owner": caller_phone, "name": c["name"], "phone": c["phone"]})
                background_tasks.add_task(
                    post_to_sheets,
                    {"action": "addPersonal", "owner": caller_phone, "name": c["name"], "phone": c["phone"]}
                )
                return PlainTextResponse(
                    "id_list_message=t-המספר נשמר באלפון האישי בהצלחה.&" +
                    get_menu_text(c["name"], c["phone"], user["mode"])
                )
        elif val == "6":
            user["step"] = "main_menu"
            return PlainTextResponse(
                "read=t-לחיפוש הקישו 1. לאזור אישי 2. לרישום 3. להודעה 4.=val,no,1,1,7,Digits,,"
            )

    # 4. שלוחה 2: אזור אישי
    if user["step"] == "personal_menu":
        if val == "1":
            return PlainTextResponse(
                f"id_list_message=t-שמך הוא {user.get('user_name')}, מספר הטלפון: {caller_phone}.&"
                "read=t-לשמיעה 1. לשינוי 2. לאלפון 3.=val,no,1,1,7,Digits,,"
            )
        elif val == "2":
            user["step"] = "update_voice"
            return PlainTextResponse("read=t-נא לומר את שמך המעודכן וסולמית לסיום.=val,record,0,0,10,File,,")
        elif val == "3":
            p_list = [x for x in cache_personal if x["owner"] == caller_phone]
            if not p_list:
                return PlainTextResponse(
                    "id_list_message=t-האלפון האישי שלך ריק.&"
                    "read=t-לשמיעה 1. לשינוי 2. לאלפון 3.=val,no,1,1,7,Digits,,"
                )
            user["search_results"] = p_list
            user["current_index"] = 0
            user["mode"] = "personal"
            user["step"] = "browse_results"
            first = p_list[0]
            return PlainTextResponse(
                f"id_list_message=t-נמצאו {len(p_list)} אנשי קשר.&" +
                get_menu_text(first["name"], first["phone"], "personal")
            )

    # שינוי פרטים
    if user["step"] == "update_voice":
        record_url = val or params.get("ApiRecordPath", "")
        text = transcribe_audio_from_url(record_url)
        user["temp_name"] = text
        user["step"] = "confirm_update"
        return PlainTextResponse(f"read=t-אמרת {text}. לאישור 1, להקלטה חוזרת 2.=val,no,1,1,7,Digits,,")

    if user["step"] == "confirm_update":
        if val == "1":
            new_name = user["temp_name"]
            for c in cache_contacts:
                if c["phone"] == caller_phone:
                    c["name"] = new_name
            background_tasks.add_task(post_to_sheets, {"action": "update", "name": new_name, "phone": caller_phone})
            user["step"] = "main_menu"
            return PlainTextResponse(
                f"id_list_message=t-שמך עודכן בהצלחה ל-{new_name}.&"
                "read=t-לחיפוש הקישו 1. לאזור אישי 2. לרישום 3. להודעה 4.=val,no,1,1,7,Digits,,"
            )
        else:
            user["step"] = "update_voice"
            return PlainTextResponse("read=t-נא לומר את שמך שוב וסולמית לסיום.=val,record,0,0,10,File,,")

    # 5. שלוחה 3: רישום חדש
    if user["step"] == "register_voice":
        record_url = val or params.get("ApiRecordPath", "")
        text = transcribe_audio_from_url(record_url)
        if not text:
            user["step"] = "register_voice"
            return PlainTextResponse("read=t-השם לא נקלט. אמור את שמך וסולמית לסיום.=val,record,0,0,10,File,,")
        user["temp_name"] = text
        user["step"] = "confirm_register_name"
        return PlainTextResponse(f"read=t-אמרת {text}. לאישור 1, להקלטה חוזרת 2.=val,no,1,1,7,Digits,,")

    if user["step"] == "confirm_register_name":
        if val == "1":
            user["step"] = "register_phone"
            return PlainTextResponse("read=t-נא להקיש את מספר הטלפון וסולמית לסיום.=val,no,9,11,7,Digits,,")
        else:
            user["step"] = "register_voice"
            return PlainTextResponse("read=t-אמור את שמך שוב וסולמית לסיום.=val,record,0,0,10,File,,")

    if user["step"] == "register_phone":
        new_phone = val
        new_name = user["temp_name"]
        cache_contacts.append({"name": new_name, "phone": new_phone})
        background_tasks.add_task(post_to_sheets, {"action": "register", "name": new_name, "phone": new_phone})
        user["step"] = "main_menu"
        return PlainTextResponse(
            f"id_list_message=t-נרשמת בהצלחה! השם {new_name}, מספר {new_phone}.&"
            "read=t-לחיפוש הקישו 1. לאזור אישי 2. לרישום 3. להודעה 4.=val,no,1,1,7,Digits,,"
        )

    # ברירת מחדל
    user["step"] = "main_menu"
    return PlainTextResponse(
        "read=t-לחיפוש בספר הטלפונים הקישו 1. לאזור אישי הקישו 2. לרישום 3. להודעה 4.=val,no,1,1,7,Digits,,"
    )

# עזרי השמעה
def do_search(query: str, user: dict, mode: str = "general"):
    matches = [c for c in cache_contacts if query in c["name"]]
    if not matches:
        user["step"] = "menu_search"
        return PlainTextResponse(
            "id_list_message=t-לא נמצאו תוצאות לשם המבוקש.&"
            "read=t-לחיפוש קולי הקישו 1, להקלדה הקישו 2.=val,no,1,1,7,Digits,,"
        )
    user["search_results"] = matches
    user["current_index"] = 0
    user["mode"] = mode
    user["step"] = "browse_results"
    first = matches[0]
    return PlainTextResponse(
        f"id_list_message=t-נמצאו {len(matches)} תוצאות.&" +
        get_menu_text(first["name"], first["phone"], mode)
    )

def get_menu_text(name: str, phone: str, mode: str = "general"):
    if mode == "general":
        t = f"t-תוצאה: {name}. טלפון: {phone}. לשמיעה חוזרת 0. לתוצאה הבאה 1. לקודמת 2. לחיפוש חדש 3. לחיוג 4. לשמירה באלפון 5. ליציאה 6."
    else:
        t = f"t-איש קשר: {name}. טלפון: {phone}. לשמיעה חוזרת 0. לאיש קשר הבא 1. לקודם 2. לחיפוש חדש 3. לחיוג 4. ליציאה 6."
    return f"read={t}=val,no,1,1,7,Digits,,"

def play_contact(name: str, phone: str, mode: str = "general"):
    return PlainTextResponse(get_menu_text(name, phone, mode))
