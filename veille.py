#!/usr/bin/env python3
"""Veille du matin : 3 actus + 1 legal + 1 web/SEO, envoyes par mail pour NotebookLM."""

import email.utils
import json
import os
import re
import smtplib
import ssl
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from html import escape
from pathlib import Path
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------------------
# CONFIG : modifie ici les sources
# ---------------------------------------------------------------------------

# Un article par source (equilibre gauche / droite / centre)
NEWS_FEEDS = [
    ("franceinfo", "https://www.francetvinfo.fr/titres.rss"),
    ("Le Figaro", "https://www.lefigaro.fr/rss/figaro_actualites.xml"),
    ("20 Minutes", "https://www.20minutes.fr/feeds/rss-une.xml"),
]

# Legal / micro-entreprise : le plus recent parmi ces flux
LEGAL_FEEDS = [
    ("Service-public pro", "https://www.service-public.fr/abonnements/rss/actu-actu-pro.rss"),
]

# Web / SEO : le plus recent parmi ces flux
SEO_FEEDS = [
    ("Google Search Central", "https://developers.google.com/search/blog/feed.xml"),
    ("Abondance", "https://www.abondance.com/feed"),
]

# Legal : un article qui parle de ces sujets (publie depuis moins de 10 jours) passe en premier
LEGAL_KEYWORDS = [
    "micro-entre", "auto-entre", "micro-entrepr", "facturation électronique",
    "facture électronique", "tva", "urssaf", "cotisation", "cfe", "budget",
    "loi de finances", "plafond", "seuil", "indépendant",
]

# Actus : on ecarte les directs, le sport, les faits divers et le people
SKIP_TITLE = re.compile(r"^\s*(en direct|direct|live|vidéo|diaporama)\b", re.I)
SKIP_URL = re.compile(
    r"avis-vin\.|/(sports?|faits-divers|people|jeux|horoscope|meteo|gastronomie|vins?|mode|"
    r"voyages?|shopping|bien-etre|lifestyle|culture|cinema|musique|tv)(/|-|$)",
    re.I,
)

# Consigne a coller dans NotebookLM (Audio Overview, Personnaliser)
NOTEBOOK_PROMPT = (
    "Débat en français entre deux animateurs aux avis opposés, ton oral et naturel, "
    "sans parti pris politique. Trois parties annoncées clairement avec une vraie "
    "transition : 1) Actualité (les 3 premiers articles, un angle différent par "
    "animateur), 2) Légal et micro-entreprise (impact concret pour un "
    "micro-entrepreneur : charges, TVA, obligations), 3) Web et SEO (ce qu'il faut "
    "surveiller ou changer sur un site). Finis par 3 points à retenir."
)

MAIL_TO = os.environ.get("MAIL_TO") or "mathis.miallet89@gmail.com"
SENT_FILE = Path(__file__).parent / "sent.json"
MAX_SENT = 300
UA = "Mozilla/5.0 (compatible; veille-matin/1.0)"
PARIS = ZoneInfo("Europe/Paris")

# ---------------------------------------------------------------------------


def http_get(url, timeout=20, limit=600_000):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read(limit)


def parse_date(text):
    if not text:
        return datetime.min.replace(tzinfo=timezone.utc)
    text = text.strip()
    try:
        d = email.utils.parsedate_to_datetime(text)
    except (TypeError, ValueError):
        d = None
    if d is None:
        try:
            d = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return datetime.min.replace(tzinfo=timezone.utc)
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d


def local(tag):
    return tag.rsplit("}", 1)[-1]


TRACKING = re.compile(r"^(xtor|at_.*|utm_.*|fbclid|gclid)$", re.I)


def clean_link(url):
    """Retire fragment et parametres de suivi (xtor, utm, at_...)."""
    parts = urllib.parse.urlsplit(url)
    query = [(k, v) for k, v in urllib.parse.parse_qsl(parts.query) if not TRACKING.match(k)]
    return urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urllib.parse.urlencode(query), "")
    )


def read_feed(name, url):
    """Retourne une liste de dicts {source, title, link, date}. Liste vide si erreur."""
    try:
        root = ET.fromstring(http_get(url))
    except Exception as exc:  # noqa: BLE001
        print(f"[flux KO] {name} ({url}) : {exc}", file=sys.stderr)
        return []
    items = []
    for node in root.iter():
        if local(node.tag) not in ("item", "entry"):
            continue
        title = link = date = ""
        for child in node:
            tag = local(child.tag)
            if tag == "title":
                title = (child.text or "").strip()
            elif tag == "link":
                link = (child.get("href") or child.text or "").strip()
            elif tag in ("pubDate", "published", "updated", "date") and not date:
                date = child.text or ""
        if title and link:
            items.append(
                {
                    "source": name,
                    "title": title,
                    "link": clean_link(link),
                    "date": parse_date(date),
                }
            )
    print(f"[flux OK] {name} : {len(items)} articles")
    return items


PAYWALL_RE = re.compile(r'"isAccessibleForFree"\s*:\s*"?false', re.I)


def is_paywalled(url):
    """True seulement si la page annonce clairement un contenu payant."""
    try:
        html = http_get(url, timeout=15).decode("utf-8", errors="ignore")
    except Exception as exc:  # noqa: BLE001
        print(f"[verif impossible] {url} : {exc}", file=sys.stderr)
        return False
    if PAYWALL_RE.search(html):
        return True
    return "réservé aux abonnés" in html.lower()


def load_sent():
    try:
        return json.loads(SENT_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return []


def save_sent(sent):
    SENT_FILE.write_text(
        json.dumps(sent[-MAX_SENT:], ensure_ascii=False, indent=1), encoding="utf-8"
    )


def pick(items, sent, news=False):
    for it in items:
        if it["link"] in sent:
            continue
        if news and (SKIP_TITLE.search(it["title"]) or SKIP_URL.search(it["link"])):
            continue
        if is_paywalled(it["link"]):
            print(f"[payant, ignore] {it['link']}")
            continue
        return it
    return None


def build_selection(sent):
    sections = []

    news = []
    for name, url in NEWS_FEEDS:
        items = sorted(read_feed(name, url), key=lambda x: x["date"], reverse=True)
        news.append(pick(items, sent, news=True))
    sections.append(("Actualité", news, [n for n, _ in NEWS_FEEDS]))

    recent = datetime.now(timezone.utc) - timedelta(days=10)

    def legal_key(it):
        hit = any(k in it["title"].lower() for k in LEGAL_KEYWORDS)
        return (hit and it["date"] >= recent, it["date"])

    for label, feeds, key in (
        ("Légal et micro-entreprise", LEGAL_FEEDS, legal_key),
        ("Web et SEO", SEO_FEEDS, lambda it: it["date"]),
    ):
        items = []
        for name, url in feeds:
            items += read_feed(name, url)
        items.sort(key=key, reverse=True)
        sections.append((label, [pick(items, sent)], [n for n, _ in feeds]))

    return sections


def build_mail(sections, today):
    text, html = [f"Veille du {today}", ""], [f"<h2>Veille du {escape(today)}</h2>"]
    links = []
    for label, picks, sources in sections:
        text.append(label.upper())
        html.append(f"<h3>{escape(label)}</h3><ul>")
        for i, it in enumerate(picks):
            if it is None:
                src = sources[i] if len(sources) == len(picks) else ", ".join(sources)
                text.append(f"- Rien de trouvé ({src})")
                html.append(f"<li><i>Rien de trouvé ({escape(src)})</i></li>")
                continue
            links.append(it["link"])
            text.append(f"- [{it['source']}] {it['title']}\n  {it['link']}")
            html.append(
                f'<li><b>{escape(it["source"])}</b> : '
                f'<a href="{escape(it["link"])}">{escape(it["title"])}</a></li>'
            )
        html.append("</ul>")
        text.append("")
    html.append(
        "<p>NotebookLM : nouveau notebook, Ajouter une source, Site web, "
        "colle les liens ci-dessous, puis Audio Overview.</p>"
    )
    html.append("<pre>" + escape("\n".join(links)) + "</pre>")
    html.append("<p><b>Consigne à coller dans Personnaliser :</b></p>")
    html.append(
        '<pre style="white-space:pre-wrap;font-family:inherit">'
        + escape(NOTEBOOK_PROMPT)
        + "</pre>"
    )
    text.append("LIENS A COLLER DANS NOTEBOOKLM")
    text.extend(links)
    text.append("")
    text.append("CONSIGNE A COLLER DANS PERSONNALISER")
    text.append(NOTEBOOK_PROMPT)
    return "\n".join(text), "\n".join(html), links


def send_mail(subject, text, html):
    # strip() : un espace ou retour a la ligne colle dans le secret fait echouer la connexion
    user = os.environ["GMAIL_USER"].strip()
    password = "".join(os.environ["GMAIL_APP_PASSWORD"].split())
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = MAIL_TO
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")

    ctx = ssl.create_default_context()
    last_error = None
    # Deux voies : SSL 465 puis STARTTLS 587 (Gmail coupe parfois l'une des deux)
    for mode, port in (("ssl", 465), ("starttls", 587)):
        for mech in ("auto", "LOGIN"):
            try:
                if mode == "ssl":
                    server = smtplib.SMTP_SSL("smtp.gmail.com", port, context=ctx, timeout=30)
                else:
                    server = smtplib.SMTP("smtp.gmail.com", port, timeout=30)
                    server.ehlo()
                    server.starttls(context=ctx)
                    server.ehlo()
                with server:
                    if mech == "LOGIN":
                        server.esmtp_features["auth"] = "LOGIN"
                    server.login(user, password)
                    server.send_message(msg)
                print(f"Connexion Gmail OK ({mode} {port} {mech})")
                return
            except smtplib.SMTPAuthenticationError as exc:
                print(f"[Gmail refuse la connexion, {mode} {port} {mech}] code {exc.smtp_code}", file=sys.stderr)
                last_error = exc
            except (smtplib.SMTPException, OSError) as exc:
                print(f"[Gmail erreur, {mode} {port} {mech}] {type(exc).__name__}: {exc}", file=sys.stderr)
                last_error = exc
    print(f"Longueur identifiant: {len(user)}, longueur cle: {len(password)} (attendu: 16)", file=sys.stderr)
    raise last_error


def main():
    now = datetime.now(PARIS)
    event = os.environ.get("GITHUB_EVENT_NAME", "")
    # Le cron GitHub est en UTC : on tire a 6h et 7h UTC et on n'envoie qu'a 8h Paris.
    if event == "schedule" and now.hour != 8:
        print(f"Il est {now:%H:%M} a Paris, pas 8h : rien a envoyer.")
        return

    sent = load_sent()
    sections = build_selection(sent)
    today = now.strftime("%d/%m/%Y")
    text, html, links = build_mail(sections, today)
    print(text)

    if not links:
        print("Aucun article trouve, pas d'envoi.", file=sys.stderr)
        sys.exit(1)

    send_mail(f"Veille du matin {today}", text, html)
    print(f"Mail envoye a {MAIL_TO}")
    save_sent(sent + links)


if __name__ == "__main__":
    main()
