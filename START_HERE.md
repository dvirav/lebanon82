# חדר מצב 1982: הרצה ופריסה

## מה יש בתיקייה

- `static/index.html`: הדף שהאתר מגיש. עותק של `reference/index.html` עם שלושה שינויים בלבד: טוען את `sync.js`, ה־QR מגיע מהשרת, וסעיף "לפני המופע" בתסריט עודכן לאתר (בלי חשבון Claude).
- `static/sync.js`: בונה `window.claude` (room ו־user) מעל WebSocket, כך שהמנוע לא השתנה.
- `app.py`: שרת FastAPI. מגיש את הדף, את `/qr.svg` ואת `/healthz`, ומחזיק את החדר ב־`/ws`.
- `reference/index.html`: הגרסה המקורית מ־claude.ai. נפתחת ישירות בדפדפן כמצגת עצמאית (גיבוי).
- `tests/`: `test_server.py` (השרת), `test_e2e.py` (מסך, שלט וצופים בדפדפנים אמיתיים מול השרת).

**לשנות תוכן:** לשנות את הנתונים (`PEOPLE`, `SEGMENTS`, `ITEMS`, `COVER_NOTE`) ב־`static/index.html`. אם רוצים שגם הגיבוי יתעדכן, לשנות גם ב־`reference/index.html`.

## הרצה מקומית

```
pip install --user --break-system-packages -r requirements-dev.txt
python3 -m playwright install chromium        # פעם אחת, בשביל הבדיקות
PRESENTER_KEY=dev python3 -m uvicorn app:app --reload
python3 -m pytest tests/                       # צילומי מסך נשמרים ב־tests/screenshots/
```

- צופה: http://localhost:8000/
- מסך מקרן: http://localhost:8000/?role=screen&key=dev
- שלט: http://localhost:8000/?role=remote&key=dev

## פריסה ל־Render (Blueprint, כמו בפרויקטים הקודמים)

1. לדחוף את הריפו ל־GitHub.
2. ב־Render: **New → Blueprint**, לבחור את הריפו. Render קורא את `render.yaml` ויוצר את השירות (frankfurt, free, מופע אחד).
3. ב־Environment של השירות: להעתיק את `PRESENTER_KEY` ש־Render ייצר.
4. הקישורים (להחליף את הכתובת ואת המפתח):
   - צופים (זה מה שה־QR מקודד): `https://lebanon82.onrender.com/`
   - מסך מקרן: `https://lebanon82.onrender.com/?role=screen&key=המפתח`
   - שלט: `https://lebanon82.onrender.com/?role=remote&key=המפתח`

   המפתח נמחק משורת הכתובת מיד אחרי הטעינה, כך שהוא לא מופיע על המקרן. הוא נשמר ללשונית הזאת בלבד, ורענון לא מאבד אותו.

## ביום המופע

- **כמה דקות לפני:** לפתוח את האתר. בתוכנית החינמית השרת נרדם אחרי רבע שעה בלי תנועה, וההתעוררות לוקחת עד דקה. (או להעביר ל־starter ליום הזה.)
- ריצה אחת אמיתית: מחשב + טלפון, "הבא", לוודא שהמסך זז, ואז "איפוס". האיפוס מוחק גם את כל ההצבעות בשרת.
- גיבוי אם האתר לא עובד: לפתוח את `reference/index.html` מהמחשב ולבחור "מצגת עצמאית".
