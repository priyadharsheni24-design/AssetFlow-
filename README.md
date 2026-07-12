# AssetFlow — Booking Dashboard (with real login + real data)

This package wires the hackathon POC (login page + booking dashboard)
up to a small Flask backend, so it runs off your actual datasets
instead of hard-coded demo arrays.

## What changed

- **app.py** (new) — a Flask server that:
  - Reads `data/employees.csv` and authenticates against it (Username,
    Employee ID, or Email + Password; account must be `Active`). Roles
    come straight from the CSV (Employee / Manager / HR / Asset
    Manager) — nobody can self-elevate at signup, matching the
    problem statement.
  - Reads `data/products.csv` (one row per physical, serial-numbered
    unit) and groups it into the asset "cards" the dashboard expects —
    e.g. the 8 individual `AF-0001-01` … `AF-0001-08` HP EliteBook
    units become one `AF-0001` card showing quantity/available counts.
  - Exposes `POST /api/login`, `POST /api/logout`, `GET /api/me`,
    `GET /api/assets`, `GET /api/categories`.
  - Serves the login page at `/login` and the dashboard at `/dashboard`.
- **static/login.html** — the login form now calls `POST /api/login`
  for real instead of always showing a fake "success" message, and
  redirects to `/dashboard` once the server confirms the credentials.
  It also bounces you straight to the dashboard if you're already
  signed in.
- **static/dashboard.html** — the old hard-coded `ASSETS` / `UNITS`
  arrays are gone. On load it calls `GET /api/me` (redirecting to
  `/login` if you're not signed in) and `GET /api/assets`, then
  renders exactly like before. The top-right avatar, account panel,
  and "Good morning/afternoon/evening, <name>" greeting now reflect
  whoever actually logged in. "Log out" calls `POST /api/logout` and
  sends you back to the login page.

Everything else — the booking flow, transfers, maintenance list,
notifications, dark mode, etc. — is untouched; it's the same
front-end behavior, just backed by real data and a real session
instead of one hard-coded demo user ("Priya").

## Running it

```bash
pip install -r requirements.txt
python app.py
```

Then open **http://127.0.0.1:5000/login** and sign in with any
Active row from `data/employees.csv`, e.g.:

- Username: `vigneshreddy`
- Password: `Asset@202693`

(Any of the 100 rows in that CSV will work as long as `Status` is
`Active`.)

## Booking approval workflow

Booking a shared asset is no longer instantly confirmed — it now goes
through a real approval step:

1. An **Employee** books an asset. The request is created server-side
   as `Pending` (`POST /api/bookings`), and their screen shows **only**
   "Pending" / "Waiting for approval…" — there is no way to see it as
   booked before someone signs off on it.
2. Anyone logged in with the **HR**, **Manager**, or **Asset Manager**
   role sees a new **Approvals** icon in the sidebar (hidden for plain
   Employees) with a red dot when there's something waiting. Opening
   it (`GET /api/bookings/pending`) lists every employee's pending
   request — asset, requester, department, date/time, purpose — with
   **Approve** / **Reject** buttons.
3. Approving or rejecting (`POST /api/bookings/<id>/approve` or
   `/reject`) records who decided it and drops a notification for the
   requesting employee specifically (not everyone) — e.g. *"✅ Sanjay
   Devi (HR) approved your booking for AF-0014, 2026-07-20
   09:00–10:00."*
4. The employee's browser polls `GET /api/bookings/mine` every few
   seconds. As soon as it sees the decision, it flips the asset from
   "Pending" to "Booked" (or releases it back to Available on a
   rejection), fires a toast, and adds the notification to their bell
   — no page reload needed, and no other employee sees this update.
5. An employee can still cancel their own request with `POST
   /api/bookings/<id>/cancel` — but only while it's still Pending.

This means the same demo can be driven from two browser
tabs/sessions: sign in as an Employee in one, book something; sign in
as HR (or Manager / Asset Manager) in another, open Approvals, and
approve or reject it — the Employee's tab updates on its own.

## Notes / things you may want to extend next

- Bookings and notifications now live in an in-memory store in
  `app.py` (`BOOKINGS`, `NOTIFICATIONS`) rather than the browser, so
  they're visible across different logged-in sessions — but they still
  reset when the Flask process restarts, and the asset counts shown on
  page load always come fresh from `products.csv` (so a booking made
  in one session won't reduce the "available" count shown after a
  page refresh in another). Swap the in-memory lists for a real
  database if this needs to survive restarts or be fully consistent
  across sessions.
- Transfers and maintenance actions in the dashboard UI still only
  live in browser memory for the session, unchanged from the original
  POC — they aren't written back to the CSVs either.
- The 4 dashboard categories (Laptops, Headsets & Earbuds, Meeting
  Halls, Vehicles) are inferred from `products.csv`'s `category`
  column plus a keyword match on the asset name (see
  `classify_category()` in `app.py`) since the CSV only distinguishes
  Electronics / Meeting Room / Vehicle.
