# Putting SameFlight Cab online (Render)

Plan: start with Option A (free, for friends), then move to Option B (paid, keeps data).
Button names on these websites change sometimes, so follow the meaning, not the exact words.

## Option A: free demo for friends
Know before you start: the free version goes to sleep after about 15 minutes with no visitors
(the next visit waits 30-60 seconds), and its data resets when it restarts. The app re-creates the sample
riders each time. OTP codes are still shown on screen, so share the link ONLY with people you trust,
and tell them to use made-up phone numbers.

1. Make a free account at github.com. Click New repository, name it sameflight-cab, create it.
2. Unzip the project. On the repository page click "uploading an existing file", drag in everything
   inside the folder that has app.py (app.py, requirements.txt, .gitignore, README.md, DEPLOY.md and the
   static folder). app.py must be at the top level, not inside another folder. Click Commit.
3. Make a free account at render.com using "Sign in with GitHub".
4. Click New > Web Service, pick your sameflight-cab repository.
5. Fill in:
   - Language/Runtime: Python 3
   - Region: Singapore if offered (closest to India)
   - Build Command: pip install -r requirements.txt
   - Start Command: gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --threads 4
   - Instance type: Free
   - Environment variable (optional): AERODATABOX_KEY = your key
6. Click Deploy. When it says Live, open the address Render shows (ends in .onrender.com).
   The first load can be slow. Send that link to friends.

## Option B: paid, keeps your data
Do this after real SMS is working (see README: Phone OTP login).
1. In your Render service, change Instance type to Starter (about $7 a month at the time of writing).
2. Add a Disk: name data, mount path /var/data, size 1 GB (about $0.25 per GB a month).
3. Add environment variable DB_PATH = /var/data/samecab.db
4. Add the SMS keys (TWILIO_SID, TWILIO_TOKEN, TWILIO_FROM) and REQUIRE_SMS = 1.
   With REQUIRE_SMS = 1 the app refuses to show test codes on screen.
   Note: the built-in SMS code supports Twilio only. For MSG91 or another provider the code must be added first.
5. Save and let it redeploy. Your users, trips and payments now survive restarts.

## Before sharing publicly
- Real SMS working and REQUIRE_SMS = 1.
- A short privacy note: matched riders see each other's phone numbers.
- Check Indian rules for shared rides.
- Back up samecab.db now and then (Render's Shell tab or a scheduled copy).
