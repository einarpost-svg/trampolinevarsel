#!/usr/bin/env python3
"""
Vindvarsel for trampoline
==========================
Sjekker værmeldingen fra MET Norway (Yr) og sender et varsel via ntfy.sh
hvis forventet vind (vedvarende eller kast) overstiger en terskel de
neste `LOOKAHEAD_HOURS` timene.

Kjør som cron-jobb, f.eks. 2-4 ganger i døgnet:
    0 6,12,18 * * *  /usr/bin/python3 /path/to/vindvarsel.py

Avhengigheter:
    pip install requests --break-system-packages
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# KONFIGURASJON – juster disse etter behov
# ---------------------------------------------------------------------------

# Kristiansand sentrum – bytt til nøyaktig posisjon for trampolinen
LATITUDE = 58.1467
LONGITUDE = 7.9956

# Terskler i m/s. MET rapporterer i m/s.
# Referanse (Beaufort): 11-13.8 = liten kuling, 13.9-17.1 = stiv kuling,
# 17.2-20.7 = sterk kuling, 20.8-24.4 = liten storm
WARN_SUSTAINED_MS = 12.0   # vedvarende vind -> "vurder å rydde inn"
WARN_GUST_MS = 15.0        # vindkast -> "vurder å rydde inn"
URGENT_SUSTAINED_MS = 17.0  # vedvarende vind -> hastevarsel
URGENT_GUST_MS = 20.0       # vindkast -> hastevarsel

# Hvor mange timer fram i tid skal vi se etter høy vind?
LOOKAHEAD_HOURS = 36

# ntfy.sh-emne. FINN PÅ ET UNIKT, HEMMELIG NAVN (fungerer som passord).
# Eksempel: "einar-trampoline-vind-8f3k2"
# Abonner på samme emne i ntfy-appen (iOS/Android) eller https://ntfy.sh/<emne>
# Settes via miljøvariabelen NTFY_TOPIC (GitHub Secret i Actions-oppsett),
# IKKE hardkodet her — emnet fungerer som et passord og skal ikke committes.
NTFY_TOPIC = os.environ.get("NTFY_TOPIC")
if not NTFY_TOPIC:
    print("FEIL: Miljøvariabelen NTFY_TOPIC er ikke satt.", file=sys.stderr)
    sys.exit(1)
NTFY_URL = f"https://ntfy.sh/{NTFY_TOPIC}"

# MET Norway krever en identifiserbar User-Agent med kontaktinfo
USER_AGENT = "vindvarsel-trampoline/1.0 github.com/einar (kontakt: einarpost@gmail.com)"

MET_URL = (
    "https://api.met.no/weatherapi/locationforecast/2.0/compact"
    f"?lat={LATITUDE}&lon={LONGITUDE}"
)

# Fil som husker siste varsel, så vi ikke spammer ved hver kjøring
STATE_FILE = Path(__file__).parent / "state.json"

# ---------------------------------------------------------------------------


def hent_prognose() -> dict:
    """Henter værdata fra MET Norway."""
    headers = {"User-Agent": USER_AGENT}
    resp = requests.get(MET_URL, headers=headers, timeout=15)
    resp.raise_for_status()
    return resp.json()


def finn_max_vind(data: dict, timer_fram: int) -> dict | None:
    """
    Går gjennom tidsserien og finner høyeste vedvarende vind og vindkast
    innenfor de neste `timer_fram` timene.
    """
    now = datetime.now(timezone.utc)
    timeseries = data.get("properties", {}).get("timeseries", [])

    max_sustained = 0.0
    max_gust = 0.0
    tidspunkt_sustained = None
    tidspunkt_gust = None

    for entry in timeseries:
        tid_str = entry["time"]
        tid = datetime.fromisoformat(tid_str.replace("Z", "+00:00"))
        diff_timer = (tid - now).total_seconds() / 3600

        if diff_timer < 0 or diff_timer > timer_fram:
            continue

        details = entry.get("data", {}).get("instant", {}).get("details", {})
        vind = details.get("wind_speed")
        kast = details.get("wind_speed_of_gust")  # finnes ikke alltid

        if vind is not None and vind > max_sustained:
            max_sustained = vind
            tidspunkt_sustained = tid_str

        if kast is not None and kast > max_gust:
            max_gust = kast
            tidspunkt_gust = tid_str

    if tidspunkt_sustained is None and tidspunkt_gust is None:
        return None

    return {
        "max_sustained": max_sustained,
        "tidspunkt_sustained": tidspunkt_sustained,
        "max_gust": max_gust,
        "tidspunkt_gust": tidspunkt_gust,
    }


def vurder_nivaa(resultat: dict) -> str | None:
    """Returnerer 'urgent', 'warn' eller None basert på terskler."""
    if (
        resultat["max_sustained"] >= URGENT_SUSTAINED_MS
        or resultat["max_gust"] >= URGENT_GUST_MS
    ):
        return "urgent"
    if (
        resultat["max_sustained"] >= WARN_SUSTAINED_MS
        or resultat["max_gust"] >= WARN_GUST_MS
    ):
        return "warn"
    return None


def formater_tidspunkt(iso_str: str | None) -> str:
    if not iso_str:
        return "ukjent tidspunkt"
    tid = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
    lokal = tid.astimezone()  # konverter til lokal tidssone
    return lokal.strftime("%a %d.%m kl. %H:%M")


def bygg_melding(resultat: dict, nivaa: str) -> tuple[str, str]:
    """Returnerer (tittel, tekst) for varselet."""
    if nivaa == "urgent":
        tittel = "⚠️ Kraftig vind ventet – rydd inn trampolinen NÅ"
    else:
        tittel = "🌬️ Økende vind ventet – vurder å rydde inn trampolinen"

    linjer = []
    if resultat["max_sustained"] >= WARN_SUSTAINED_MS:
        linjer.append(
            f"Vedvarende vind opptil {resultat['max_sustained']:.0f} m/s "
            f"({formater_tidspunkt(resultat['tidspunkt_sustained'])})"
        )
    if resultat["max_gust"] >= WARN_GUST_MS:
        linjer.append(
            f"Vindkast opptil {resultat['max_gust']:.0f} m/s "
            f"({formater_tidspunkt(resultat['tidspunkt_gust'])})"
        )

    tekst = "\n".join(linjer) if linjer else "Høy vind i prognosen."
    return tittel, tekst


def send_ntfy(tittel: str, tekst: str, nivaa: str) -> None:
    prioritet = "urgent" if nivaa == "urgent" else "default"
    headers = {
        "Title": tittel.encode("utf-8"),
        "Priority": prioritet,
        "Tags": "warning,wind_blowing_face",
    }
    resp = requests.post(NTFY_URL, data=tekst.encode("utf-8"), headers=headers, timeout=10)
    resp.raise_for_status()


def les_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def skriv_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


def skal_varsle_paa_nytt(state: dict, nivaa: str, resultat: dict) -> bool:
    """
    Enkel dedupe: ikke send samme nivå-varsel for samme værhendelse
    (identifisert ved tidspunktet for maks vind) flere ganger.
    """
    siste_nivaa = state.get("siste_nivaa")
    siste_hendelse = state.get("siste_hendelse_tidspunkt")
    denne_hendelsen = resultat.get("tidspunkt_gust") or resultat.get("tidspunkt_sustained")

    if siste_nivaa == nivaa and siste_hendelse == denne_hendelsen:
        return False
    return True


def main() -> int:
    try:
        data = hent_prognose()
    except requests.RequestException as e:
        print(f"FEIL: Klarte ikke hente værdata: {e}", file=sys.stderr)
        # Valgfritt: send et lavterskel-varsel om at sjekken feilet.
        # Kommentert ut som standard for å unngå støy ved forbigående feil.
        # send_ntfy("Vindvarsel feilet", str(e), "warn")
        return 1

    resultat = finn_max_vind(data, LOOKAHEAD_HOURS)
    if resultat is None:
        print("Ingen vinddata funnet i tidsvinduet.")
        return 0

    nivaa = vurder_nivaa(resultat)
    if nivaa is None:
        print(
            f"OK – ingen høy vind ventet. "
            f"Maks vedvarende: {resultat['max_sustained']:.1f} m/s, "
            f"maks kast: {resultat['max_gust']:.1f} m/s."
        )
        return 0

    state = les_state()
    if not skal_varsle_paa_nytt(state, nivaa, resultat):
        print(f"Allerede varslet om denne hendelsen ({nivaa}). Hopper over.")
        return 0

    tittel, tekst = bygg_melding(resultat, nivaa)
    try:
        send_ntfy(tittel, tekst, nivaa)
        print(f"Varsel sendt ({nivaa}): {tittel}")
    except requests.RequestException as e:
        print(f"FEIL: Klarte ikke sende varsel: {e}", file=sys.stderr)
        return 1

    skriv_state(
        {
            "siste_nivaa": nivaa,
            "siste_hendelse_tidspunkt": resultat.get("tidspunkt_gust")
            or resultat.get("tidspunkt_sustained"),
            "sendt_tidspunkt": datetime.now(timezone.utc).isoformat(),
        }
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
