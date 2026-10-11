import os
import io
import re
import time
import json
import threading
import requests
from difflib import SequenceMatcher
import speech_recognition as sr
from pydub import AudioSegment
from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import PlainTextResponse

app = FastAPI()

# משתני סביבה
GOOGLE_SCRIPT_URL = os.getenv("GOOGLE_SCRIPT_URL", "https://script.google.com/macros/s/AKfycbzLJrs2r6AsceENCsqbB1lLdo6jrIxErSNflNq-IaifcNESqyV3IgYG7SAjTy5L2RCjag/exec")
YEMOT_TOKEN = os.getenv("YEMOT_TOKEN", "093136451:112131")

cache_contacts = []
cache_personal = []
user_states = {}

def log_info(msg: str):
    print(f"[IVR-LOG] {msg}", flush=True)

def clean_yemot_text(text: str) -> str:
    """הסרת נקודות ומקפים למניעת שגיאות פענוח בימות המשיח"""
    if not text:
        return ""
    return text.replace(".", " ").replace("-", " ").strip()

def normalize_phone(phone: str) -> str:
    """משלים אוטומטית את ה-0 המוביל אם נמחק באקסל/שיטס"""
    clean = re.sub(r"\D", "", str(phone).strip())
    if clean.startswith("972"):
        clean = "0" + clean[3:]
    elif len(clean) in (8, 9) and not clean.startswith("0"):
        clean = "0" + clean
    return clean

# ==================== אלגוריתם חיפוש מטושטש / שמות דומים ====================

def normalize_hebrew(s: str) -> str:
    if not s:
        return ""
    trans = str.maketrans({
        'ך': 'כ',
        'ם': 'מ',
        'ן': 'נ',
        'ף': 'פ',
        'ץ': 'צ'
    })
    cleaned = s.translate(trans)
    return re.sub(r'[\s\-_]+', ' ', cleaned).strip()

def calculate_similarity(query: str, target: str) -> float:
    q = normalize_hebrew(query)
    t = normalize_hebrew(target)

    if not q or not t:
        return 0.0

    if q == t:
        return 1.0

    if q in t or t in q:
        return 0.95

    q_words = q.split()
    t_words = t.split()
    word_scores = []
    for qw in q_words:
        best_match_for_qw = max([SequenceMatcher(None, qw, tw).ratio() for tw in t_words], default=0.0)
        word_scores.append(best_match_for_qw)

    avg_word_score = sum(word_scores) / len(word_scores) if word_scores else 0.0
    full_ratio = SequenceMatcher(None, q, t).ratio()

    return max(full_ratio, avg_word_score)

# ==================== סנכרון Google Sheets ====================

def sync_from_sheets():
    global cache_contacts, cache_personal
    try:
        res = requests.get(f"{GOOGLE_SCRIPT_URL}?action=getAll", timeout=15)
        if res.status_code == 200:
            data = res.json()
            raw_contacts = data.get("contacts", [])
            cache_contacts = [
                {"name": c["name"], "phone": normalize_phone(c["phone"])}
                for c in raw_contacts if c.get("name") and c.get("phone")
            ]
            raw_personal = data.get("personal", [])
            cache_personal = [
                {
                    "owner": normalize_phone(p["owner"]),
                    "name": p["name"],
                    "phone": normalize_phone(p["phone"])
                }
                for p in raw_personal if p.get("owner")
            ]
            log_info(f"סנכרון משיטס הושלם בהצלחה: {len(cache_contacts)} אנשי קשר במאגר.")
        else:
            log_info(f"שגיאה בסנכרון sheets: קוד {res.status_code}")
    except Exception as e:
        log_info(f"שגיאה בחיבור ל-Google Sheets: {e}")

def auto_sync_loop():
    """לולאת רקע המרעננת את הנתונים מהגיליון כל 30 שניות באופן שקט"""
    while True:
        time.sleep(30)
        sync_from_sheets()

def ensure_routing_parent_folder():
    try:
        url = "https://www.call2all.co.il/ym/api/UploadFile"
        files = {
            'file': ('ext.ini', io.BytesIO("type=menu\r\n".encode('utf-8')), 'text/plain')
        }
        params = {
            'token': YEMOT_TOKEN,
            'path': "ivr2:/routing/ext.ini",
            'convertAudio': '0'
        }
        requests.post(url, params=params, files=files, timeout=10)
        log_info("תיקיית routing הראשית מאומתת ומוכנה.")
    except Exception as e:
        log_info(f"שגיאה בהגדרת תיקיית routing הראשית: {e}")

@app.on_event("startup")
def startup():
    threading.Thread(target=sync_from_sheets, daemon=True).start()
    threading.Thread(target=ensure_routing_parent_folder, daemon=True).start()
    threading.Thread(target=auto_sync_loop, daemon=True).start()

@app.get("/refresh")
def manual_refresh():
    sync_from_sheets()
    return {"status": "ok", "total_contacts": len(cache_contacts)}

def post_to_sheets(payload: dict):
    try:
        log_info(f"שולח עדכון ל-Google Sheets: {payload.get('action')}")
        requests.post(GOOGLE_SCRIPT_URL, json=payload, timeout=10)
    except Exception as e:
        log_info(f"שגיאה בכתיבה ל-Sheets: {e}")

def get_state(phone: str):
    if phone not in user_states:
        user_states[phone] = {
            "step": "main_menu",
            "search_results": [],
            "current_index": 0,
            "temp_name": "",
            "mode": "general",
            "found_phone_contact": None
        }
    return user_states[phone]

# ==================== טיפול בהורדה וזיהוי קולי ====================

def download_audio_from_yemot(path_or_url: str) -> bytes:
    clean_path = path_or_url.strip()
    if clean_path.startswith("http://") or clean_path.startswith("https://"):
        download_url = clean_path
    else:
        file_path = clean_path.lstrip("/")
        download_url = f"https://www.call2all.co.il/ym/api/DownloadFile?token={YEMOT_TOKEN}&path=ivr2:/{file_path}"
    
    log_info(f"מוריד קובץ שמע מכתובת: {download_url}")
    r = requests.get(download_url, timeout=15)
    if r.status_code != 200 or len(r.content) < 100:
        raise Exception(f"Failed to download audio file (Status: {r.status_code}, Length: {len(r.content)})")
    
    log_info(f"קובץ שמע הורד בהצלחה (גודל: {len(r.content)} בתים)")
    return r.content

def transcribe_audio(path_or_url: str) -> str:
    try:
        audio_bytes = download_audio_from_yemot(path_or_url)
        sound = AudioSegment.from_file(io.BytesIO(audio_bytes))
        sound = sound.set_frame_rate(16000).set_channels(1)
        wav_io = io.BytesIO()
        sound.export(wav_io, format="wav")
        wav_io.seek(0)

        recognizer = sr.Recognizer()
        with sr.AudioFile(wav_io) as source:
            recorded = recognizer.record(source)
            text = recognizer.recognize_google(recorded, language="he-IL")
            log_info(f"זיהוי דיבור הצליח: '{text}'")
            return clean_yemot_text(text)
    except sr.UnknownValueError:
        log_info("Audio recognition error: גוגל לא הצליח לזהות מילים (הקלטה שקטה מדי או לא ברורה)")
        return ""
    except sr.RequestError as e:
        log_info(f"Audio recognition error: שגיאת שרת גוגל ({e})")
        return ""
    except Exception as e:
        log_info(f"Audio recognition error ({type(e).__name__}): {e or repr(e)}")
        return ""

# ==================== ניתוביה וחיוג ====================

def route_via_nitoviya(target_phone: str):
    clean_target = normalize_phone(target_phone)
    unique_folder = f"routing/{clean_target}"
    ini_content = f"type=nitoviya\r\nnitoviya_dial_to={clean_target}\r\n"

    try:
        log_info(f"מגדיר ניתוביה בשלוחה /{unique_folder} עבור היעד {clean_target}...")
        url = "https://www.call2all.co.il/ym/api/UploadFile"
        files = {
            'file': ('ext.ini', io.BytesIO(ini_content.encode('utf-8')), 'text/plain')
        }
        params = {
            'token': YEMOT_TOKEN,
            'path': f"ivr2:/{unique_folder}/ext.ini",
            'convertAudio': '0'
        }
        res = requests.post(url, params=params, files=files, timeout=10)
        log_info(f"תשובת ימות המשיח: {res.text}")
    except Exception as e:
        log_info(f"שגיאה בהגדרת ניתוביה: {e}")

    return PlainTextResponse(f"go_to_folder=/{unique_folder}")

# ==================== ניתוב שיחה ראשי ====================

@app.api_route("/", methods=["GET", "POST", "HEAD"])
async def handle_ivr(request: Request, background_tasks: BackgroundTasks):
    if request.method == "HEAD":
        return PlainTextResponse("ok")

    # חילוץ הפרמטרים תוך טיפול בצבירת ערכי val
    if request.method == "POST":
        try:
            form = await request.form()
            val_list = [v.strip() for v in form.getlist("val") if str(v).strip()]
            params = dict(form)
        except Exception:
            val_list = []
            params = {}
    else:
        val_list = [v.strip() for v in request.query_params.getlist("val") if str(v).strip()]
        params = dict(request.query_params)

    val = val_list[-1] if val_list else ""
    wav_files = [v for v in val_list if v.endswith(".wav")]
    latest_record_path = wav_files[-1] if wav_files else val

    caller_phone = normalize_phone(params.get("ApiPhone", "000"))
    hangup = params.get("hangup", "")

    log_info(f"פנייה נכנסת | טלפון: {caller_phone} | ערכים: {val_list} | ערך נבחר: '{val}' | קובץ שמע: '{latest_record_path}'")

    if hangup == "yes":
        if caller_phone in user_states:
            del user_states[caller_phone]
        return PlainTextResponse("ok")

    user = get_state(caller_phone)
    log_info(f"שלב נוכחי: {user['step']}")

    # 1. תפריט ראשי
    # 1: חיפוש שם | 2: חיפוש מספר | 3: אזור אישי | 4: רישום חדש | 5: הודעה למנהל
    if not val and user["step"] == "main_menu":
        return PlainTextResponse(
            "read=t-שלום וברכה, לחיפוש בספר הטלפונים לפי שם הקישו 1, לחיפוש לפי מספר טלפון הקישו 2, לאזור אישי הקישו 3, לרישום מספר חדש הקישו 4, להשארת הודעה הקישו 5=val,no,1,1,7,NO,,,,,,,,no"
        )

    # 2. ניתוב מהתפריט הראשי
    if user["step"] == "main_menu":
        if val == "1":
            user["step"] = "menu_search"
            return PlainTextResponse(
                "read=t-לחיפוש קולי הקישו 1, לחיפוש בהקלדה הקישו 2=val,no,1,1,7,NO,,,,,,,,no"
            )
        elif val == "2":
            # שלוחה 2: חיפוש לפי מספר טלפון
            user["step"] = "search_by_phone_input"
            return PlainTextResponse(
                "read=t-נא להקיש את מספר הטלפון לחיפוש וסולמית לסיום=val,no,11,9,10,NO,,,,,,,,no"
            )
        elif val == "3":
            # שלוחה 3: אזור אישי
            matched = next((c for c in cache_contacts if c["phone"] == caller_phone), None)
            if not matched:
                user["step"] = "main_menu"
                return PlainTextResponse(
                    "id_list_message=t-מספר הטלפון אינו מופיע במאגר, ניתן להירשם בשלוחה 4&"
                    "read=t-לחיפוש שם 1, לחיפוש מספר 2, לאזור אישי 3, לרישום 4, להודעה 5=val,no,1,1,7,NO,,,,,,,,no"
                )
            user["user_name"] = clean_yemot_text(matched["name"])
            user["step"] = "personal_menu"
            return PlainTextResponse(
                "read=t-שלום, לשמיעת הפרטים האישיים הקישו 1, לשינוי הפרטים הקישו 2, לאלפון אישי הקישו 3=val,no,1,1,7,NO,,,,,,,,no"
            )
        elif val == "4":
            # שלוחה 4: רישום שם ומספר חדש
            user["step"] = "register_voice"
            return PlainTextResponse(
                "read=t-נא לומר את שמך לאחר הצליל ולסיום הקישו סולמית=val,no,record,,,no"
            )
        elif val == "5":
            # שלוחה 5: הודעה למנהל (מעבר לשלוחה 7/1)
            return PlainTextResponse("go_to_folder=/7/1")
        else:
            return PlainTextResponse(
                "read=t-בחירה שגויה, לחיפוש שם 1, לחיפוש מספר 2, לאזור אישי 3, לרישום 4, להודעה 5=val,no,1,1,7,NO,,,,,,,,no"
            )

    # ==================== שלוחה 1: חיפוש לפי שם ====================
    if user["step"] == "menu_search":
        if val == "1":
            user["step"] = "search_voice_record"
            return PlainTextResponse(
                "read=t-נא לומר את השם לחיפוש לאחר הצליל וסולמית לסיום=val,no,record,,,no"
            )
        elif val == "2":
            user["step"] = "search_tap"
            return PlainTextResponse(
                "read=t-נא להקליד את השם לחיפוש וסולמית לסיום=val,no,15,1,7,HebrewKeyboard,,,,,,,,no"
            )

    if user["step"] == "search_voice_record":
        text = transcribe_audio(latest_record_path)
        if not text:
            user["step"] = "menu_search"
            return PlainTextResponse(
                "id_list_message=t-השם לא נקלט&"
                "read=t-לחיפוש קולי הקישו 1, להקלדה הקישו 2=val,no,1,1,7,NO,,,,,,,,no"
            )
        return do_search(text, user, mode="general")

    if user["step"] == "search_tap":
        return do_search(clean_yemot_text(val), user, mode="general")

    # ניווט בתוצאות חיפוש לפי שם (0-6)
    if user["step"] == "browse_results":
        res = user["search_results"]
        idx = user["current_index"]
        c = res[idx]
        c_name = clean_yemot_text(c["name"])
        c_phone = normalize_phone(c["phone"])

        if val == "0":
            return play_contact(c_name, c_phone, user["mode"])
        elif val == "1":
            if idx + 1 < len(res):
                user["current_index"] += 1
            nxt = res[user["current_index"]]
            return play_contact(clean_yemot_text(nxt["name"]), normalize_phone(nxt["phone"]), user["mode"])
        elif val == "2":
            if idx - 1 >= 0:
                user["current_index"] -= 1
            prv = res[user["current_index"]]
            return play_contact(clean_yemot_text(prv["name"]), normalize_phone(prv["phone"]), user["mode"])
        elif val == "3":
            user["step"] = "menu_search"
            return PlainTextResponse(
                "read=t-לחיפוש קולי 1, להקלדה 2=val,no,1,1,7,NO,,,,,,,,no"
            )
        elif val == "4":
            return route_via_nitoviya(c_phone)
        elif val == "5":
            if user["mode"] == "general":
                cache_personal.append({"owner": caller_phone, "name": c_name, "phone": c_phone})
                background_tasks.add_task(
                    post_to_sheets,
                    {"action": "addPersonal", "owner": caller_phone, "name": c_name, "phone": c_phone}
                )
                return PlainTextResponse(
                    "id_list_message=t-המספר נשמר באלפון האישי בהצלחה&" +
                    get_menu_text(c_name, c_phone, user["mode"])
                )
        elif val == "6":
            user["step"] = "main_menu"
            return PlainTextResponse(
                "read=t-לחיפוש שם 1, לחיפוש מספר 2, לאזור אישי 3, לרישום 4, להודעה 5=val,no,1,1,7,NO,,,,,,,,no"
            )

    # ==================== שלוחה 2: חיפוש לפי מספר טלפון ====================
    if user["step"] == "search_by_phone_input":
        search_num = normalize_phone(val)
        matched = next((c for c in cache_contacts if normalize_phone(c["phone"]) == search_num), None)

        if not matched:
            user["step"] = "search_by_phone_menu"
            return PlainTextResponse(
                "id_list_message=t-לא נמצא איש קשר עבור מספר זה&"
                "read=t-לחיפוש מחדש הקישו 2, ליציאה הקישו 3=val,no,1,1,7,NO,,,,,,,,no"
            )

        user["found_phone_contact"] = matched
        user["step"] = "search_by_phone_menu"
        c_name = clean_yemot_text(matched["name"])
        c_phone = normalize_phone(matched["phone"])
        
        # השמעת התוצאה ותפריט: 1 לחיוג, 2 לחיפוש מחדש, 3 ליציאה
        return PlainTextResponse(
            f"id_list_message=t-נמצא איש קשר {c_name}, מספר טלפון {c_phone}&"
            "read=t-לחיוג הקישו 1, לחיפוש מחדש הקישו 2, ליציאה הקישו 3=val,no,1,1,7,NO,,,,,,,,no"
        )

    if user["step"] == "search_by_phone_menu":
        if val == "1":
            # חיוג לאיש הקשר שנמצא
            contact = user.get("found_phone_contact")
            if contact:
                return route_via_nitoviya(contact["phone"])
            user["step"] = "search_by_phone_input"
            return PlainTextResponse("read=t-נא להקיש את מספר הטלפון לחיפוש וסולמית לסיום=val,no,11,9,10,NO,,,,,,,,no")
        elif val == "2":
            # חיפוש מחדש
            user["step"] = "search_by_phone_input"
            return PlainTextResponse("read=t-נא להקיש את מספר הטלפון לחיפוש וסולמית לסיום=val,no,11,9,10,NO,,,,,,,,no")
        elif val == "3":
            # יציאה לתפריט ראשי
            user["step"] = "main_menu"
            return PlainTextResponse(
                "read=t-לחיפוש שם 1, לחיפוש מספר 2, לאזור אישי 3, לרישום 4, להודעה 5=val,no,1,1,7,NO,,,,,,,,no"
            )
        else:
            return PlainTextResponse(
                "read=t-בחירה שגויה, לחיוג הקישו 1, לחיפוש מחדש 2, ליציאה 3=val,no,1,1,7,NO,,,,,,,,no"
            )

    # ==================== שלוחה 3: אזור אישי ====================
    if user["step"] == "personal_menu":
        if val == "1":
            return PlainTextResponse(
                f"id_list_message=t-שמך הוא {user.get('user_name')}, ומספר הטלפון {caller_phone}&"
                "read=t-לשמיעה 1, לשינוי 2, לאלפון אישי 3=val,no,1,1,7,NO,,,,,,,,no"
            )
        elif val == "2":
            user["step"] = "update_voice"
            return PlainTextResponse(
                "read=t-נא לומר את שמך המעודכן וסולמית לסיום=val,no,record,,,no"
            )
        elif val == "3":
            p_list = [x for x in cache_personal if x["owner"] == caller_phone]
            if not p_list:
                return PlainTextResponse(
                    "id_list_message=t-האלפון האישי שלך ריק&"
                    "read=t-לשמיעה 1, לשינוי 2, לאלפון 3=val,no,1,1,7,NO,,,,,,,,no"
                )
            user["search_results"] = p_list
            user["current_index"] = 0
            user["mode"] = "personal"
            user["step"] = "browse_results"
            first = p_list[0]
            return PlainTextResponse(
                f"id_list_message=t-נמצאו {len(p_list)} אנשי קשר&" +
                get_menu_text(clean_yemot_text(first["name"]), normalize_phone(first["phone"]), "personal")
            )

    if user["step"] == "update_voice":
        new_name = transcribe_audio(latest_record_path)
        if not new_name:
            user["step"] = "update_voice"
            return PlainTextResponse(
                "read=t-השם לא נקלט, נא לומר את שמך שוב וסולמית לסיום=val,no,record,,,no"
            )

        for c in cache_contacts:
            if c["phone"] == caller_phone:
                c["name"] = new_name
        background_tasks.add_task(post_to_sheets, {"action": "update", "name": new_name, "phone": caller_phone})
        user["step"] = "main_menu"
        return PlainTextResponse(
            f"id_list_message=t-שמך עודכן בהצלחה ל {new_name}&"
            "read=t-לחיפוש שם 1, לחיפוש מספר 2, לאזור אישי 3, לרישום 4, להודעה 5=val,no,1,1,7,NO,,,,,,,,no"
        )

    # ==================== שלוחה 4: רישום חדש ====================
    if user["step"] == "register_voice":
        text = transcribe_audio(latest_record_path)
        if not text:
            user["step"] = "register_voice"
            return PlainTextResponse(
                "read=t-השם לא נקלט, נא לומר את שמך וסולמית לסיום=val,no,record,,,no"
            )
        
        user["temp_name"] = text
        user["step"] = "confirm_register_name"
        return PlainTextResponse(
            f"read=t-השם שנקלט הוא {text}, לאישור הקישו 1, להקלטה חוזרת 2=val,no,1,1,7,NO,,,,,,,,no"
        )

    if user["step"] == "confirm_register_name":
        if val == "1":
            user["step"] = "register_phone"
            return PlainTextResponse(
                f"read=t-נא להקיש את מספר הטלפון עבור {user['temp_name']} וסולמית לסיום=val,no,11,9,10,NO,,,,,,,,no"
            )
        else:
            user["step"] = "register_voice"
            return PlainTextResponse(
                "read=t-נא לומר את שמך שוב וסולמית לסיום=val,no,record,,,no"
            )

    if user["step"] == "register_phone":
        new_phone = normalize_phone(val)
        new_name = user["temp_name"]
        cache_contacts.append({"name": new_name, "phone": new_phone})
        background_tasks.add_task(post_to_sheets, {"action": "register", "name": new_name, "phone": new_phone})
        user["step"] = "main_menu"
        return PlainTextResponse(
            f"id_list_message=t-נרשמת בהצלחה השם {new_name} מספר {new_phone}&"
            "read=t-לחיפוש שם 1, לחיפוש מספר 2, לאזור אישי 3, לרישום 4, להודעה 5=val,no,1,1,7,NO,,,,,,,,no"
        )

    # ברירת מחדל
    user["step"] = "main_menu"
    return PlainTextResponse(
        "read=t-לחיפוש שם 1, לחיפוש מספר 2, לאזור אישי 3, לרישום 4, להודעה 5=val,no,1,1,7,NO,,,,,,,,no"
    )

# ==================== ביצוע חיפוש חכם ומדורג לפי שם ====================

def do_search(query: str, user: dict, mode: str = "general"):
    clean_q = normalize_hebrew(query)
    scored_results = []

    for c in cache_contacts:
        score = calculate_similarity(clean_q, c["name"])
        if score >= 0.65:
            scored_results.append((score, c))

    scored_results.sort(key=lambda x: x[0], reverse=True)
    matches = [item[1] for item in scored_results]

    if not matches:
        user["step"] = "menu_search"
        return PlainTextResponse(
            "id_list_message=t-לא נמצאו תוצאות לשם המבוקש&"
            "read=t-לחיפוש קולי הקישו 1, להקלדה הקישו 2=val,no,1,1,7,NO,,,,,,,,no"
        )

    user["search_results"] = matches
    user["current_index"] = 0
    user["mode"] = mode
    user["step"] = "browse_results"
    first = matches[0]
    first_name = clean_yemot_text(first["name"])
    first_phone = normalize_phone(first["phone"])
    
    log_info(f"נמצאו {len(matches)} תוצאות דומות עבור '{query}'. התוצאה המובילה: {first_name}")

    return PlainTextResponse(
        f"id_list_message=t-נמצאו {len(matches)} תוצאות&" +
        get_menu_text(first_name, first_phone, mode)
    )

def get_menu_text(name: str, phone: str, mode: str = "general"):
    phone_clean = normalize_phone(phone)
    if mode == "general":
        t = f"t-תוצאה {name}, טלפון {phone_clean}, לשמיעה חוזרת 0, לתוצאה הבאה 1, לקודמת 2, לחיפוש חדש 3, לחיוג 4, לשמירה באלפון 5, ליציאה 6"
    else:
        t = f"t-איש קשר {name}, טלפון {phone_clean}, לשמיעה חוזרת 0, לאיש קשר הבא 1, לקודם 2, לחיפוש חדש 3, לחיוג 4, ליציאה 6"
    
    return f"read={t}=val,no,1,1,7,NO,,,,,,,,no"

def play_contact(name: str, phone: str, mode: str = "general"):
    return PlainTextResponse(get_menu_text(name, phone, mode))
