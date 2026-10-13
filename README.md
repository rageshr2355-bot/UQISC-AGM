# UQISC AGM — live voting

Live voting site for the UQISC Annual General Meeting. It has three views that update in real time:

| View | URL | Who |
|---|---|---|
| **Audience** | `/` | Members' phones. Shows the ballot while a vote is open; otherwise a UQISC waiting screen. |
| **Big screen** | `/screen` | Projector. UQISC waiting screen with a QR code to check in (while check-in is open), and results or awards only when the admin airs them. |
| **Admin** | `/admin` | Committee laptop. Full control. |

## Run it

Needs Python 3 and nothing else (no installs).

```bash
ADMIN_PASSWORD=pick-something python3 server.py
```

Then open `http://localhost:8000/admin`. `/screen` uses the same password.

## One person, one vote (no proxies)

Each member gets a **personal voting code card** at the door. There are 300 unique 4-character codes in `codes.json`, each a mix of letters and digits.
- **Print the cards:** open `/codes` (or Admin → Settings → *Print code cards*). It prints 30 per A4 page across 10 pages; cut along the dashed lines. A ready-made `UQISC-AGM-voting-codes.pdf` is included. You can type your website address on the print page and it'll appear on every card.
- **Check in:** members enter their name and code. **Each code works once.** Whoever uses it first owns it, and anyone else who tries it is refused.
- **Lost phone:** in the Attendees tab, remove that person. This frees their code so they can check in again with the same card.
- **Close check-in** once the meeting starts so no new codes can be used.
- Ballots are secret: admins see only the totals.
- `codes.json` **must be deployed with the app**, or the printed cards won't match. *Generate new codes* (Settings) replaces them all and means reprinting.

## Running the meeting

1. **Elections tab.** The positions are pre-loaded in poster order (T5 → Creative Team → Operations Team → International Officer). Add candidates for each, including floor nominations on the night.
2. Click **Open voting**. The ballot appears on every checked-in phone. For x2 positions, members can choose up to 2.
3. Optionally click **Show on screen** to put the live vote count on the projector.
4. Click **Close voting**, then **Air results** when you're ready to reveal. Winners are marked *Elected*. A tie for the last seat is flagged and you can **Re-run vote**.
5. **Motions tab.** Use it for any other vote (constitution changes and so on).
6. **Awards tab.** Add special awards, then click **Present on screen** for each one.
7. **Back to waiting** returns the projector to the UQISC screen.
8. **Settings → Download results CSV** gives you the minutes record.

State is saved to `data.json`, so restarting the server loses nothing.

## Getting phones onto it

- **Same Wi-Fi:** run it on a laptop and share `http://<laptop-ip>:8000`. On a Mac, find the IP with `ipconfig getifaddr en0`. UQ eduroam often blocks device-to-device traffic, so test this beforehand or use a hotspot.
- **Online (recommended):** deploy to any Python host (Render, Railway, Fly.io…). Start command `python3 server.py`, and set the `ADMIN_PASSWORD` env var. The host's `PORT` is picked up automatically.

The big-screen QR code points to this laptop's network address, even if the screen is opened as `localhost`. Once the site is online, set `PUBLIC_URL` (e.g. `https://uqisc-agm.onrender.com`) so it points to the public site. A printable QR poster is at `/qr`.

Options: `PUBLIC_URL`, `CODE_COUNT` (default 300; only used when `codes.json` doesn't exist yet), `DATA_FILE` (default `./data.json`).
