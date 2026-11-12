# מדריך מעשי וארכיטקטוני ל-Gemini Decisions API (`gemini-decision-1`): שכבת קבלת ההחלטות החדשה של מערכות AI ארגוניות

> **סביבת הדמו החיה ב-Google Cloud Run:**
> https://gemini-decisions-studio-1085727639300.us-central1.run.app

---

## 1. למה ארגונים צריכים API ייעודי לקבלת החלטות?

במערכות ייצור ארגוניות — החל מנתבי סוכנים (Agent Routers), דרך סינון הונאות (Fraud Triage), ביקורת קבלות ומסמכים, ועד שערי אבטחה (CyberStrike Guardrails) — רוב הקריאות למודלי שפה אינן מבקשות לנסח מאמר או תשובה ארוכה למשתמש. הן מבקשות **הכרעה מובנית**:
- *"האם התמונה שצירף הלקוח אכן מראה חבילה פגומה?"* (`כן / לא`)
- *"לאיזה מסלול טיפול או מודל יש לנתב את הפנייה?"* (`בחירה מתוך רשימה סגורה`)
- *"מהי רמת הסיכון להזרקת פרומפט או הונאה במסמך הסרוק?"* (`דירוג מכויל על סולם חומרה`)

ה-**Gemini Decisions API (`gemini-decision-1`)** מפריד בין **שכבת הבקרה וקבלת ההחלטות (Control Plane)** לבין **שכבת היצירה הגנרטיבית (Generation Plane)**: המודל מעבד קלט רב-מודאלי משותף במעבר קדימה יחיד (Single Forward Pass), עונה על עשרות שאלות במקביל, ומחזיר ישירות **וקטורי הסתברויות מכוילים ורמת ביטחון (`confidence`)** — בתעריף של **`$0.10` למיליון טוקני קלט** ו-**`$0.00` (אפס מוחלט!) לטוקני פלט**.

---

## 2. שלושת אבני הבניין של ה-API: `noul`, `choice` ו-`score`

ה-Endpoint הרשמי (`POST https://generativelanguage.googleapis.com/v1beta/decisions`) מקבל `model="gemini-decision-1"`, מערך בלוקים משותף `input` (טקסט, תמונה, אודיו, וידאו או PDF) שמחושב ומתומחר פעם אחת בלבד, ומילון שאלות `questions`:

| סוג שאלה (`type`) | שימוש מומלץ | הגדרת `criteria` | מבנה התשובה המוחזרת |
| :--- | :--- | :--- | :--- |
| **`noul`** | בדיקת תנאי בוליאני, עמידה במדיניות או זיהוי חריגה | ללא `criteria` — מעבירים `instructions` בלבד | `noul` (`0.01..0.99`) + `confidence` |
| **`choice`** | ניתוב בין 2 עד 255 מסלולים/כלים/סוכנים | אובייקט מיפוי (`dict`) של מזהה אפשרות -> תיאור | `choice` + `probabilities` לכל אפשרות + `confidence` |
| **`score`** | דירוג חומרה, איכות או סיכון על סולם סדור (החל מ-`0`) | מערך סדור (`list`) של 2 עד 255 רמות | `score` (ממוצע משוקלל רציף) + `legend` + `probabilities` + `confidence` |

---

## 3. שילוב ב-Python (`google-genai >= 3.2.0`) עם FastAPI ו-Cloud Run

```python
import base64
import os
from pathlib import Path
from google import genai

client = genai.Client(
    api_key=os.environ["GEMINI_API_KEY"],
    http_options={"api_version": "v1beta"},
)

async def evaluate_claim_photo(photo_path: Path, claim_text: str) -> dict:
    img_b64 = base64.b64encode(photo_path.read_bytes()).decode("ascii")
    resp = await client.aio.decisions.create(
        model="gemini-decision-1",
        input=[
            {"type": "image", "data": img_b64, "mime_type": "image/jpeg", "resolution": "medium"},
            {"type": "text", "text": f"\nCustomer Claim: {claim_text}"},
        ],
        questions={
            "photo_corroborates_claim": {
                "type": "noul",
                "instructions": "Does the visual evidence in the photo corroborate the customer's damage claim?",
            },
            "claim_decision": {
                "type": "choice",
                "instructions": "Choose the automated fulfillment workflow.",
                "criteria": {
                    "instant_refund_and_reship": "Clear physical shipping damage visible in the photo.",
                    "flag_photo_mismatch_review": "Photo contradicts the claim (box is intact and dry).",
                    "escalate_specialist": "Image is blurry or inconclusive.",
                },
            },
            "damage_severity": {
                "type": "score",
                "instructions": "Rate physical package damage severity.",
                "criteria": [
                    "0 - Intact pristine parcel",
                    "1 - Cosmetic denting only",
                    "2 - Severe structural crush / water damage / shattered contents",
                ],
            },
        },
    )
    return resp.model_dump()
```

---

## 4. ששת היתרונות המרכזיים (מבוסס על מדידות אמת ב-Cloud Run)

1. **חיסכון FinOps של עד פי 85 (`$0.10 / 1M` קלט, `$0.00` פלט):** ניתוח חי של 6 תמונות ברזולוציה מלאה ו-24 שאלות במקביל עלה `$0.00051` בלבד עם `0` טוקני פלט.
2. **אמורטיזציה של הקלט המשותף (Shared Context Amortization):** הקלט המשותף מחויב פעם אחת בלבד בין אם שואלים שאלה אחת או 25 שאלות באותה קריאה.
3. **הבנה רב-מודאלית ישירה ללא OCR נפרד:** פענוח קבלות מצולמות, חשבוניות סרוקות והקלטות אודיו בקריאה יחידה.
4. **אפס שבירות JSON ואפס הזיות סכמה:** כל תשובה חוזרת במבנה מתמטי קבוע ומוגדר.
5. **שער הסלמה מכויל (`Smart Cascade`):** ניתוב אוטומטי כאשר `confidence >= 0.75`, והסלמה למודל `gemini-4-carbon` או למודל הסייבר `gemini-v4p1s-rev25-skimaki-cyber-permissive-raw-thoughts` אך ורק במקרי קצה או חשד לתקיפה.
6. **הגנה חזקה מפני הזרקת פרומפט ויזואלית:** בבדיקת חשבונית פישינג סרוקה שכללה הזרקת `[AI_OVERRIDE]`, המודל זיהה `visual_prompt_injection = 93.7%` וחסם את העברת הכספים בביטחון של `99.9%`.

---

## 5. חיבור מפתח Gemini אישי (BYOK) ובדיקת תרחישים מותאמים אישית ב-Cloud Run

במערכת החיה (`https://gemini-decisions-studio-1085727639300.us-central1.run.app`):
- לחצו על **`🔑 Connect Gemini Key`** בסרגל העליון כדי לחבר מפתח אישי מ-Google AI Studio. המפתח מאומת בלייב מול `gemini-decision-1` ונשמר מוצפן ב-`AES-256-GCM` בתוך עוגיית סשן `HttpOnly; Secure; SameSite=Strict` (ללא שמירה ב-`localStorage`).
- בלשונית **`2 · Custom Scenario Builder`** לחצו על **`+ Blank Custom Scenario`** או בחרו תבנית התחלה (כולל תרחיש שירות לקוחות בעברית), העלו קובצי תמונה/PDF/אודיו משלכם, ערכו שאלות בלחיצה על **`✎ Edit`**, וייצאו/ייבאו תרחישים בפורמט JSON.
