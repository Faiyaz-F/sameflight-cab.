# SameFlight Cab

Share an airport cab with someone from your flight. Python (Flask) backend, SQLite database, plain HTML/JS frontend.

## Run it on your computer
1. Install Python 3.9 or newer from python.org.
2. In this folder run:
   pip install -r requirements.txt
   python app.py
3. Open http://localhost:5000

Friends on the same Wi-Fi can open http://YOUR-COMPUTER-IP:5000 and use it too.
The database file `samecab.db` is created on first run, with sample riders for today.
Delete it to start fresh.

## Try the full flow
1. Log in as yourself (any name, 10-digit phone). Add a trip: flight 6E 204, Bandra.
2. Tap "Share cab" on Meera or Aarav. They are sample riders, so to accept as them,
   open a private window, log in with phone 9000000002, and add a trip for the same flight.
   Or use two browsers as two real users.
3. The other user sees your request and taps Accept. Both see the confirmed ride and fare.

## Files
- app.py: API, matching rules (flight, landing gap, detour, women-only), fare split
- static/index.html: the website
- samecab.db: data (users, trips, requests)

## Not built yet (needed before real launch)
Phone OTP login, live flight-status API, real payments (UPI), driver booking,
ratings, admin screen, HTTPS and a real server (gunicorn), and checking Indian
rules for shared rides.

## Live flight status (optional)
The "Check flight" button fills in the landing time and delay.
- Without a key it works only for two demo flights: 6E 204 (on time) and AI 617 (30 min late).
- For real flights, get a free key for AeroDataBox on RapidAPI (rapidapi.com), then in the
  Command Prompt, before starting the app, run:
      set AERODATABOX_KEY=your_key_here
      python app.py
  Free plans have a small monthly limit, so check their current limits.

## Phone OTP login
Login now asks for a 6-digit code. Codes last 5 minutes, allow 5 tries, and each login gets a private session token.
- **Test mode (default):** no SMS is sent. The code appears on the login screen and in the black window.
  To log in as a sample rider, use their phone (for example 9000000002 for Meera) and the code shown.
- **Real SMS:** set Twilio details before starting the app (untested here, and India SMS rules may need
  extra registration, so check with your SMS provider):
      set TWILIO_SID=your_sid
      set TWILIO_TOKEN=your_token
      set TWILIO_FROM=your_twilio_number
      python app.py
  Once these are set, the code is no longer shown on screen.

## Flight delays
- With a live flight key, the app re-checks your flight about every 5 minutes while the trip page is open and
  moves your landing time if the flight is delayed. Matches and pickup time update by themselves.
- Without a key (or if the service has no data), use "Flight early or late? Update" on the trip page to change
  the landing time by hand. A matched partner sees "flight is N min late, so pickup moved".
- Checks only run while someone has that trip page open. Background checks need a scheduled job, which is a later step.

## Paying the fare split (UPI)
This app does not hold or move any money. After a match is accepted:
- The rider with the farther drop is the "host". They book and pay the whole cab, and add their UPI ID.
- The other rider sees "Pay your share". On a phone, the button opens their UPI app with the amount filled in.
  On a computer they copy the UPI ID and pay from their phone.
- The guest taps "I have paid", then the host taps "I received" to close it.
Shares are rounded to the nearest 10 rupees, so the two shares can differ slightly from the exact cab total.
There is no automatic proof of payment, so riders should check their own UPI app. Later you can add a
payment gateway such as Razorpay, which needs a business account and KYC.

## Paying cash
The guest can tap "Pay cash instead" (or "Pay by UPI instead" to switch back before paying).
In cash mode the guest hands the money to the host in the cab, and the host taps "I received".
The host does not need a UPI ID when the guest pays cash.
