"""Map Polish place names to Sejm electoral district numbers (okręg).

Uses PKW / Kodeks wyborczy numbering (41 districts, unchanged 2019–2023).
Matching is substring-based on the lowercased question; **longer phrases are
checked first** so specific names win over shorter ones (np. „lubliniec” vs „lublin”).

Województwa mapujemy na **wiele okręgów** (np. śląskie → 27–32). Nie używamy
samych „śląsk”/„slask” — występują w „dolnośląskie”; stosujemy „śląskie”, „śląska” itd.
"""

from __future__ import annotations

# (substring, district_nr) — lowercase; ASCII variants where useful.
_RAW_LOCATION_RULES: list[tuple[str, str]] = [
    # --- Resolve substring collisions (longer first via sort) ---
    ("lubliniec", "28"),
    ("radomsko", "10"),
    ("tarnowo podgórne", "39"),
    ("tarnowo podgorne", "39"),
    # --- Long / multi-word ---
    ("biała podlaska", "7"),
    ("biala podlaska", "7"),
    ("bielsko-biała", "27"),
    ("bielsko-biala", "27"),
    ("dąbrowa górnicza", "32"),
    ("dabrowa gornicza", "32"),
    ("grodzisk mazowiecki", "20"),
    ("jastrzębie-zdrój", "30"),
    ("jastrzebie-zdroj", "30"),
    ("jelenia góra", "1"),
    ("jelenia gora", "1"),
    ("nowy dwór mazowiecki", "20"),
    ("nowy dwor mazowiecki", "20"),
    ("nowy sącz", "14"),
    ("nowy sacz", "14"),
    ("piotrków trybunalski", "10"),
    ("piotrkow trybunalski", "10"),
    ("piekary śląskie", "31"),
    ("piekary slaskie", "31"),
    ("ruda śląska", "31"),
    ("ruda slaska", "31"),
    ("siemianowice śląskie", "31"),
    ("siemianowice slaskie", "31"),
    ("świętochłowice", "31"),
    ("swietochlowice", "31"),
    ("zielona góra", "8"),
    ("zielona gora", "8"),
    # --- Okręg 20 (aglomeracja warszawska) before „warszaw” ---
    ("legionowo", "20"),
    ("pruszków", "20"),
    ("pruszkow", "20"),
    ("piaseczno", "20"),
    ("otwock", "20"),
    ("wołomin", "20"),
    ("wolomin", "20"),
    ("warszawski zachodni", "20"),
    # --- Siedziby OKW i główne miasta (wg wykazu PKW / Wikipedia) ---
    ("legnica", "1"),
    ("wałbrzych", "2"),
    ("walbrzych", "2"),
    ("wrocław", "3"),
    ("wroclaw", "3"),
    ("bydgoszcz", "4"),
    ("inowrocław", "4"),
    ("inowroclaw", "4"),
    ("toruń", "5"),
    ("torun", "5"),
    ("grudziądz", "5"),
    ("grudziadz", "5"),
    ("włocławek", "5"),
    ("wloclawek", "5"),
    ("lublin", "6"),
    ("chełm", "7"),
    ("chelm", "7"),
    ("zamość", "7"),
    ("zamosc", "7"),
    ("łódź", "9"),
    ("lodz", "9"),
    ("skierniewice", "10"),
    ("sieradz", "11"),
    ("chrzanów", "12"),
    ("chrzanow", "12"),
    ("kraków", "13"),
    ("krakow", "13"),
    ("tarnów", "15"),
    ("tarnow", "15"),
    ("płock", "16"),
    ("plock", "16"),
    ("radom", "17"),
    ("siedlce", "18"),
    ("ostrołęka", "18"),
    ("ostroleka", "18"),
    ("warszawa", "19"),
    ("warszawy", "19"),
    ("warszawie", "19"),
    ("warszawę", "19"),
    ("warszawe", "19"),
    ("opole", "21"),
    ("krosno", "22"),
    ("przemyśl", "22"),
    ("przemysl", "22"),
    ("rzeszów", "23"),
    ("rzeszow", "23"),
    ("tarnobrzeg", "23"),
    ("białystok", "24"),
    ("bialystok", "24"),
    ("gdańsk", "25"),
    ("gdansk", "25"),
    ("sopot", "25"),
    ("słupsk", "26"),
    ("slupsk", "26"),
    ("gdynia", "26"),
    ("bielsko", "27"),
    ("częstochowa", "28"),
    ("czestochowa", "28"),
    ("częstochow", "28"),
    ("czestochow", "28"),
    ("gliwice", "29"),
    ("bytom", "29"),
    ("zabrze", "29"),
    ("rybnik", "30"),
    ("żory", "30"),
    ("zory", "30"),
    ("katowice", "31"),
    ("chorzów", "31"),
    ("chorzow", "31"),
    ("mysłowice", "31"),
    ("myslowice", "31"),
    ("tychy", "31"),
    ("sosnowiec", "32"),
    ("jaworzno", "32"),
    ("zawiercie", "32"),
    ("kielce", "33"),
    ("elbląg", "34"),
    ("elblag", "34"),
    ("olsztyn", "35"),
    ("kalisz", "36"),
    ("leszno", "36"),
    ("konin", "37"),
    ("piła", "38"),
    ("pila", "38"),
    ("poznań", "39"),
    ("poznan", "39"),
    ("koszalin", "40"),
    ("szczecin", "41"),
    ("świnoujście", "41"),
    ("swinoujscie", "41"),
]

_LOCATION_RULES_SORTED: list[tuple[str, str]] = sorted(
    _RAW_LOCATION_RULES,
    key=lambda item: len(item[0]),
    reverse=True,
)

# Województwo → numery okręgów sejmowych (PKW; podział 2019–2023).
_VOIVODESHIP_DISTRICT_RULES_RAW: list[tuple[str, list[str]]] = [
    ("warmińsko-mazurskie", ["34", "35"]),
    ("warminsko-mazurskie", ["34", "35"]),
    ("kujawsko-pomorskie", ["4", "5"]),
    ("kujawsko pomorskie", ["4", "5"]),
    ("zachodniopomorskie", ["40", "41"]),
    ("dolnośląskie", ["1", "2", "3"]),
    ("dolnoslaskie", ["1", "2", "3"]),
    ("małopolskie", ["12", "13", "14", "15"]),
    ("malopolskie", ["12", "13", "14", "15"]),
    ("mazowieckie", ["16", "17", "18", "19", "20"]),
    ("wielkopolskie", ["36", "37", "38", "39"]),
    ("podkarpackie", ["22", "23"]),
    ("łódzkie", ["9", "10", "11"]),
    ("lodzkie", ["9", "10", "11"]),
    ("lubelskie", ["6", "7"]),
    ("pomorskie", ["25", "26"]),
    ("śląskie", ["27", "28", "29", "30", "31", "32"]),
    ("slaskie", ["27", "28", "29", "30", "31", "32"]),
    # potocznie / odmiany (bez „śląsk” — zawarte w „dolnośląskie”)
    ("śląskiem", ["27", "28", "29", "30", "31", "32"]),
    ("slaskiem", ["27", "28", "29", "30", "31", "32"]),
    ("śląska", ["27", "28", "29", "30", "31", "32"]),
    ("slaska", ["27", "28", "29", "30", "31", "32"]),
    ("śląsku", ["27", "28", "29", "30", "31", "32"]),
    ("slasku", ["27", "28", "29", "30", "31", "32"]),
    ("lubuskie", ["8"]),
    ("opolskie", ["21"]),
    ("podlaskie", ["24"]),
    ("świętokrzyskie", ["33"]),
    ("swietokrzyskie", ["33"]),
]

_VOIVODESHIP_DISTRICT_RULES_SORTED: list[tuple[str, list[str]]] = sorted(
    _VOIVODESHIP_DISTRICT_RULES_RAW,
    key=lambda item: len(item[0]),
    reverse=True,
)


def districts_for_question(question: str) -> list[str] | None:
    """Districts for question."""
    lowered = question.lower()
    for key, district in _LOCATION_RULES_SORTED:
        if key in lowered:
            return [district]
    for key, districts in _VOIVODESHIP_DISTRICT_RULES_SORTED:
        if key in lowered:
            return list(districts)
    if "warszaw" in lowered:
        return ["19"]
    return None
