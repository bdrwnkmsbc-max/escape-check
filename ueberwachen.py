"""Ueberwacht freie Termine bei Finest Escape (Legenden der Zauberakademie)
und schickt eine Mail, sobald ein Tag frei wird.

Benutzung:
  python3 ueberwachen.py --info      # Pruefmodus: speichert info.json (kein Mailversand)
  python3 ueberwachen.py --test      # schickt eine Test-Mail
  python3 ueberwachen.py             # einmal pruefen
  python3 ueberwachen.py --dauer     # alle 10 Minuten pruefen (Endlosschleife)

Mail-Zugangsdaten als Umgebungsvariablen (nicht ins Skript schreiben!):
  MAIL_VON, MAIL_PASSWORT, optional MAIL_AN, SMTP_HOST, SMTP_PORT
"""
import json
import os
import random
import smtplib
import sys
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from email.message import EmailMessage
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import sync_playwright

SEITE = "https://finest-escape.de/nuernberg/legenden-der-zauberakademie/#Buchung"
TAGE_VORAUS = 240          # ca. 8 Monate
SCHRITT = 14               # Tage pro Abfrage
PAUSE_MINUTEN = 30
STATE_DATEI = "gemeldet.json"


def mail_senden(betreff, text):
    von = os.environ["MAIL_VON"]
    msg = EmailMessage()
    msg["From"] = von
    msg["To"] = os.environ.get("MAIL_AN") or von
    msg["Subject"] = betreff
    msg.set_content(text)
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("SMTP_PORT", "465"))
    with smtplib.SMTP_SSL(host, port) as s:
        s.login(von, os.environ["MAIL_PASSWORT"])
        s.send_message(msg)


def abfragen(info_modus=False):
    """Laedt die Seite, uebernimmt den Token des Browsers und fragt die Tage ab."""
    auth = {}
    info = {}
    gesehen = []

    def on_request(req):
        if "quinbook" in req.url:
            gesehen.append(req.url[:120])
        if "getslotscounts" in req.url and "h" not in auth:
            h = req.headers.get("authorization")
            if h:
                auth["h"] = h
                q = parse_qs(urlparse(req.url).query)
                auth["tz"] = q.get("tzoffset", ["-60"])[0]

    def on_response(resp):
        if not info_modus:
            return
        if "/v2/shop/getevents" in resp.url or "/v2/shop/slots/" in resp.url:
            try:
                info[resp.url] = resp.json()
            except Exception:
                info[resp.url] = resp.text()[:5000]

    frei = {}
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(
            locale="de-DE", timezone_id="Europe/Berlin",
            user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"))
        page.on("request", on_request)
        page.on("response", on_response)
        antwort = page.goto(SEITE, wait_until="networkidle")
        print(f"  Seite geladen: Status {antwort.status if antwort else '?'}, Titel: {page.title()!r}")
        for name in ("Alle akzeptieren", "Akzeptieren", "Zustimmen", "Alle zulassen"):
            try:
                page.get_by_role("button", name=name).first.click(timeout=1500)
                print(f"  Cookie-Banner geklickt: {name}")
                break
            except Exception:
                pass
        for _ in range(10):
            if "h" in auth:
                break
            page.mouse.wheel(0, 1500)
            page.wait_for_timeout(1000)
        page.wait_for_timeout(2000)
        if "h" not in auth:
            try:
                page.screenshot(path="fehler.png", full_page=True)
                print("  Seitentext:", page.inner_text("body")[:600].replace("\n", " "))
            except Exception as ex:
                print("  Diagnose fehlgeschlagen:", ex)
            print("  Quinbook-Anfragen gesehen:", gesehen)
            browser.close()
            raise RuntimeError("Kein Token gefunden - Seite hat die Terminabfrage nicht gestartet.")

        kopf = {
            "Authorization": auth["h"],
            "Accept": "application/json",
            "Origin": "https://finest-escape.de",
            "Referer": "https://finest-escape.de/",
        }
        heute = datetime.now(ZoneInfo("Europe/Berlin")).date()
        d = heute
        while d <= heute + timedelta(days=TAGE_VORAUS):
            e = d + timedelta(days=SCHRITT - 1)
            url = (f"https://api.quinbook.com/v2/shop/getslotscounts"
                   f"?tzoffset={auth['tz']}&start={d}&end={e}")
            r = page.request.get(url, headers=kopf)
            if not r.ok:
                print(f"  Warnung: {url} -> Status {r.status}")
            else:
                for tag, v in r.json().items():
                    if v.get("available") or v.get("count", 0) > 0:
                        frei[tag[:10]] = v.get("count")
            d = e + timedelta(days=1)
            time.sleep(0.5)

        # Uhrzeiten der freien Slots je Tag holen (fuer die Mail)
        for tag in sorted(frei):
            zeiten = []
            try:
                r = page.request.get(
                    f"https://api.quinbook.com/v2/shop/slots/{tag}?tzoffset={auth['tz']}",
                    headers=kopf)
                if r.ok:
                    for ev in r.json().get("events", []):
                        for s in ev.get("slots", []):
                            if s.get("available"):
                                zeiten.append(s["start"][11:16])
            except Exception:
                pass
            frei[tag] = zeiten
            time.sleep(0.5)
        browser.close()

    if info_modus:
        with open("info.json", "w", encoding="utf-8") as f:
            json.dump(info, f, ensure_ascii=False, indent=2)
        print("info.json gespeichert.")
    return frei


def pruefen():
    frei = abfragen()
    try:
        with open(STATE_DATEI, encoding="utf-8") as f:
            bekannt = set(json.load(f))
    except FileNotFoundError:
        bekannt = set()

    neu = sorted(set(frei) - bekannt)
    stamp = time.strftime("%d.%m. %H:%M")
    if neu:
        zeilen = "\n".join(
            f"  {t}:  {', '.join(frei[t]) if frei[t] else '(Uhrzeiten siehe Seite)'}"
            for t in neu)
        mail_senden(
            "Escape Room: Termin frei!",
            f"Bei 'Legenden der Zauberakademie' sind Termine frei geworden:\n\n{zeilen}\n\n"
            f"Schnell buchen: {SEITE}\n",
        )
        print(f"[{stamp}] MAIL gesendet fuer: {', '.join(neu)}")
    else:
        print(f"[{stamp}] nichts Neues ({len(frei)} freie Tage insgesamt)")

    with open(STATE_DATEI, "w", encoding="utf-8") as f:
        json.dump(sorted(frei), f)


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    if arg == "--info":
        abfragen(info_modus=True)
    elif arg == "--test":
        mail_senden("Escape-Room-Skript: Testmail", "Die Mail-Einstellungen funktionieren.")
        print("Testmail gesendet.")
    elif arg == "--dauer":
        while True:
            try:
                pruefen()
            except Exception as ex:
                print("Fehler:", ex)
            time.sleep(PAUSE_MINUTEN * 60 + random.randint(0, 60))
    else:
        pruefen()
