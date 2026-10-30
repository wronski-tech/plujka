from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import os
import re
import unicodedata
from typing import Any
from datetime import date, timedelta

import altair as alt
import pandas as pd
import pydeck as pdk
import requests
import streamlit as st

from chart_columns import numeric_columns, scatter_columns
from map_units import boundaries, feature_key, is_osm, unit_values, visible_features, voivodeship_level

API_URL = os.getenv("API_URL", "http://localhost:8000")
# Sent as X-API-Key when the API requires keys (API_KEYS on the API side).
API_KEY = os.getenv("PLUJKA_API_KEY", "")
API_HEADERS = {"X-API-Key": API_KEY} if API_KEY else {}
# Result column that identifies a unit -> boundary file (gminy/powiaty by TERYT, voivodeships by name;
# 1990s gmina codes map to current gminas, 1990s voivodeship names to wojewodztwa_1975).
MAP_LEVELS = (
    ("teryt_gmina", "gminy"), ("teryt_gmina_pre1999", "gminy"), ("teryt_powiat", "powiaty"),
    ("wojewodztwo", "wojewodztwa"),
)
# Charts and maps are drawn only for results that fit on screen.
MAX_CHART_ROWS = 40
# Persons offered for the timeline view (rows of a long result may name many people).
MAX_PERSON_OPTIONS = 50
# Matches listed by the person search box (GET /persons limit).
PERSON_SEARCH_LIMIT = 20
# Polish names of v_person_history.chamber values.
CHAMBER_LABELS = {
    "sejm": "Sejm",
    "senat": "Senat",
    "prezydent": "Prezydent RP",
    "pe": "Parlament Europejski",
    "sejmik": "Sejmik województwa",
    "wbp": "Wójt / burmistrz / prezydent miasta",
    "rada_gminy": "Rada gminy",
    "rada_powiatu": "Rada powiatu",
    "rada_dzielnicy": "Rada dzielnicy (Warszawa)",
}
# Same wording as the person caveat in api/services/answer.py (CAVEATS).
PERSON_CAVEAT = (
    "PKW nie publikuje identyfikatorów osób: kandydatury łączone są po imieniu, nazwisku, płci "
    "(i roku urodzenia w wyborach samorządowych), więc imiennicy mogą być połączeni lub rozdzieleni błędnie."
)

# How often the readiness fragment may refresh while data is still loading (avoid hammering /health).
_READINESS_POLL_INTERVAL = timedelta(seconds=15)
# Diagnostics panels (catalog + table stats): slower polling than readiness banner.
_DIAG_POLL_INTERVAL = timedelta(seconds=30)


def _feedback_fingerprint(question: str, data: dict) -> str:
    """Feedback fingerprint."""
    blob = json.dumps(
        {
            "question": question,
            "intent": data.get("intent"),
            "sql": data.get("sql"),
            "params": data.get("params"),
            "candidate_geo_source": data.get("candidate_geo_source"),
            "mandate_extremes_source": data.get("mandate_extremes_source"),
        },
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(blob.encode()).hexdigest()[:24]


def _fetch_catalog_summary() -> dict | None:
    """KBW mirror file inventory counts from ``GET /kbw/catalog/summary``."""
    try:
        response = requests.get(f"{API_URL}/kbw/catalog/summary", timeout=15)
        response.raise_for_status()
        return response.json()
    except requests.RequestException:
        return None


def _fetch_health_with_kb_stats() -> dict | None:
    """``GET /health?details=1`` — approximate row counts (`kbw_stats`)."""
    try:
        response = requests.get(f"{API_URL}/health", params={"details": "true"}, timeout=15)
        response.raise_for_status()
        return response.json()
    except requests.RequestException:
        return None


def _ask(question: str, entity_refs: list[dict] | None = None, previous: dict | None = None) -> dict | None:
    """POST /ask; shows an error and returns None on failure.

    ``previous`` is the preceding turn ({question, sql, entity_refs}) for a follow-up question.
    """
    payload: dict = {"question": question}
    if entity_refs:
        payload["entity_refs"] = entity_refs
    if previous:
        payload["previous"] = previous
    try:
        response = requests.post(f"{API_URL}/ask", json=payload, headers=API_HEADERS, timeout=60)
        if response.status_code == 429:
            st.warning("Za dużo pytań w krótkim czasie. Spróbuj ponownie za minutę.")
            return None
        if response.status_code == 401:
            st.error("API wymaga klucza (ustaw PLUJKA_API_KEY dla interfejsu).")
            return None
        response.raise_for_status()
        return response.json()
    except requests.RequestException as error:
        st.error(f"Coś poszło nie tak z API: {error}")
        return None


def _looks_like_follow_up(question: str) -> bool:
    """Heuristic default for the follow-up toggle: "a w 2019?", "i w Gdańsku?", very short questions."""
    words = question.lower().split()
    return bool(words) and (words[0] in ("a", "i", "oraz", "to", "tam", "tylko") or len(words) <= 3)


HISTORY_SIZE = 20


def _history_sidebar() -> None:
    """Questions asked in this session; a click shows that answer again without asking the API."""
    history = st.session_state.get("ask_history") or []
    with st.sidebar:
        st.markdown("**Historia pytań**")
        if not history:
            st.caption("Pytania z tej sesji pojawią się tutaj.")
            return
        for i, turn in enumerate(history):
            if st.button(turn["question"][:80], key=f"history_{i}", use_container_width=True):
                st.session_state["last_ask"] = turn
        if st.button("Wyczyść historię", key="history_clear"):
            st.session_state["ask_history"] = []
            st.rerun()


def _remember(question: str, data: dict, entity_refs: list[dict] | None, previous: dict | None) -> None:
    """Store the answered turn; a follow-up keeps the earlier questions in its context text."""
    context = f"{previous['question']} {question}" if previous else question
    st.session_state["last_ask"] = {
        "question": question,
        "data": data,
        # Context sent with the next follow-up (tail only, so a long conversation stays short).
        "previous": {"question": context[-600:], "sql": data.get("sql") or None, "entity_refs": entity_refs},
    }
    st.session_state.pop("_feedback_ack_fp", None)
    st.session_state.pop("person_view", None)
    history = st.session_state.setdefault("ask_history", [])
    history.insert(0, st.session_state["last_ask"])
    del history[HISTORY_SIZE:]


def _value_column(rows: list[dict]) -> str | None:
    """Column to chart: a percentage first, then votes, then any other number."""
    numeric = numeric_columns(rows)
    for pattern in ("pct", "votes", "seats", "mandat"):
        for name in numeric:
            if pattern in name:
                return name
    return numeric[0] if numeric else None


def _render_scatter(rows: list[dict], label: str, x: str, y: str) -> None:
    """One point per territorial unit: first measure on x, second on y, unit name on hover."""
    frame = pd.DataFrame(rows)[[label, x, y]].dropna()
    if frame.empty:
        return
    chart = (
        alt.Chart(frame)
        .mark_circle(color="#6366f1", size=60, opacity=0.7)
        .encode(
            x=alt.X(f"{x}:Q", title=x, scale=alt.Scale(zero=False)),
            y=alt.Y(f"{y}:Q", title=y, scale=alt.Scale(zero=False)),
            tooltip=[label, x, y],
        )
        .properties(height=420)
    )
    st.altair_chart(chart, use_container_width=True)
    st.caption(f"Każdy punkt to jedna jednostka ({len(frame)}): {x} (oś pozioma) i {y} (oś pionowa).")


def _render_chart(rows: list[dict]) -> None:
    """Scatter plot for two measures per territorial unit, otherwise a horizontal bar chart when each row is
    one labelled unit with a number (e.g. results by party)."""
    scatter = scatter_columns(rows)
    if scatter:
        _render_scatter(rows, *scatter)
        return
    if not 2 <= len(rows) <= MAX_CHART_ROWS:
        return
    value = _value_column(rows)
    labels = [n for n, v in rows[0].items() if isinstance(v, str) and not n.startswith("teryt")]
    if not value or not labels:
        return
    label = labels[0]
    frame = pd.DataFrame(rows)[[label, value]].dropna()
    if frame[label].duplicated().any():
        return
    chart = (
        alt.Chart(frame)
        .mark_bar(color="#6366f1")
        .encode(
            x=alt.X(f"{value}:Q", title=value),
            y=alt.Y(f"{label}:N", sort="-x", title=None, axis=alt.Axis(labelOverlap=False, labelLimit=240)),
            tooltip=[label, value],
        )
        .properties(height=max(120, 26 * len(frame)))
    )
    st.altair_chart(chart, use_container_width=True)


def _points(coordinates: list) -> list[list[float]]:
    """Flatten (Multi)Polygon coordinates to [lon, lat] points."""
    if isinstance(coordinates[0], (int, float)):
        return [coordinates]
    return [p for part in coordinates for p in _points(part)]


def _view_of(points: list[list[float]]) -> pdk.ViewState:
    """Map view centred on the coloured units, zoomed so they fit (all of Poland at most)."""
    if not points:
        return pdk.ViewState(latitude=52.0, longitude=19.4, zoom=5.1)
    lons, lats = [p[0] for p in points], [p[1] for p in points]
    # At zoom z the map shows about 360 / 2**z * (width / 512) degrees of longitude. A degree of
    # latitude in Poland is about 1.6 degrees of longitude on screen, and the map is about half as
    # tall as it is wide, hence the factor 3 for latitude.
    span = max(max(lons) - min(lons), 3.0 * (max(lats) - min(lats)), 0.05)
    zoom = min(10.0, max(4.8, math.log2(360 / span) + 0.6))
    return pdk.ViewState(latitude=(min(lats) + max(lats)) / 2, longitude=(min(lons) + max(lons)) / 2, zoom=zoom)


# Map palettes from the lowest to the highest value; viridis stays readable with colour blindness.
MAP_PALETTES = {
    "indygo": [(224, 231, 255), (67, 56, 202)],
    "viridis (dla daltonistów)": [(253, 231, 37), (94, 201, 98), (33, 145, 140), (59, 82, 139), (68, 1, 84)],
}
MAP_CLASSES = 5


def _palette_color(stops: list[tuple[int, int, int]], share: float) -> list[int]:
    """Colour at ``share`` (0..1) along the palette stops."""
    position = min(max(share, 0.0), 1.0) * (len(stops) - 1)
    i = min(int(position), len(stops) - 2)
    t = position - i
    return [round(a + (b - a) * t) for a, b in zip(stops[i], stops[i + 1])]


def _map_legend(items: list[tuple[list[int], str]]) -> None:
    """Colour swatches with their labels under a map."""
    swatches = "".join(
        f'<span style="display:inline-flex;align-items:center;margin-right:14px">'
        f'<span style="width:18px;height:12px;background:rgb({c[0]},{c[1]},{c[2]});border:1px solid #94a3b8;'
        f'margin-right:5px;display:inline-block"></span>{label}</span>'
        for c, label in items
    )
    st.markdown(f'<div style="font-size:0.85rem;line-height:1.9">{swatches}</div>', unsafe_allow_html=True)


def _render_map(rows: list[dict], key: str = "answer") -> None:
    """Choropleth of a number per gmina, powiat or voivodeship when the rows carry the unit code."""
    value = _value_column(rows) if rows else None
    if not value:
        return
    for column, level in MAP_LEVELS:
        codes = [r.get(column) for r in rows]
        if len(rows) >= 2 and all(codes) and len(set(codes)) == len(codes):
            break
    else:
        return
    if level == "wojewodztwa":
        level = voivodeship_level(rows)
    # Old TERYT codes are mapped to current units; Warsaw, split gminas, changed powiats and voivodeships use
    # their former extent when the result describes the old layout.
    values, merged, skipped = unit_values(rows, column, value, level)
    years = {r["year"] for r in rows if isinstance(r.get("year"), int)}
    shown = visible_features(boundaries(level)["features"], set(values) | set(skipped), years, level)
    drawn = [values[k] for k in (feature_key(f["properties"], level) for f in shown) if k in values]
    if not drawn:
        return
    low, high = min(drawn), max(drawn)
    c1, c2 = st.columns(2)
    scale = c1.radio("Skala mapy", ["ciągła", f"{MAP_CLASSES} przedziałów (kwantyle)"], horizontal=True, key=f"map_scale_{key}")
    stops = MAP_PALETTES[c2.radio("Paleta", list(MAP_PALETTES), horizontal=True, key=f"map_palette_{key}")]
    ordered = sorted(drawn)
    # Upper bounds of the quantile classes (the last one is the maximum).
    breaks = [ordered[min(len(ordered) - 1, (len(ordered) * (i + 1)) // MAP_CLASSES - 1)] for i in range(MAP_CLASSES)]
    classes = scale != "ciągła"

    def share_of(found: float) -> float:
        if classes:
            return next(i for i, b in enumerate(breaks) if found <= b) / (MAP_CLASSES - 1)
        return (found - low) / (high - low) if high > low else 1.0

    features, matched_points = [], []
    for feature in shown:
        props = feature["properties"]
        found = values.get(feature_key(props, level))
        if found is None:
            fill = [226, 232, 240, 90]
        else:
            fill = [*_palette_color(stops, share_of(found)), 230]
            matched_points.extend(_points(feature["geometry"]["coordinates"]))
        features.append({**feature, "properties": {**props, "fill": fill, "value": "—" if found is None else found}})
    layer = pdk.Layer(
        "GeoJsonLayer",
        {"type": "FeatureCollection", "features": features},
        pickable=True,
        stroked=True,
        filled=True,
        get_fill_color="properties.fill",
        get_line_color=[100, 116, 139, 120],
        line_width_min_pixels=0.5,
    )
    st.pydeck_chart(
        pdk.Deck(
            layers=[layer],
            initial_view_state=_view_of(matched_points),
            map_style=None,
            tooltip={"text": f"{{name}}\n{value}: {{value}}"},
        ),
        use_container_width=True,
    )
    if classes:
        bounds = [low, *breaks]
        _map_legend([(_palette_color(stops, i / (MAP_CLASSES - 1)), f"{bounds[i]:g}–{bounds[i + 1]:g}")
                     for i in range(MAP_CLASSES) if i == 0 or bounds[i + 1] > bounds[i]])
    else:
        _map_legend([(_palette_color(stops, i / (MAP_CLASSES - 1)), f"{low + (high - low) * i / (MAP_CLASSES - 1):.4g}")
                     for i in range(MAP_CLASSES)])
    st.caption(f"Kolor: {value}. Szare jednostki nie występują w wyniku.")
    if level == "wojewodztwa_1975":
        st.caption("Granice 49 województw z lat 1975–1998 złożone z obecnych gmin (przybliżone).")
    elif column == "teryt_gmina_pre1999":
        st.caption(
            "Gminy sprzed 1999 r. w obecnych granicach; gminy wydzielone później są w granicach gminy, "
            "z której powstały, albo szare."
        )
    if any(is_osm(f) for f in shown):
        st.caption("© OpenStreetMap contributors (ODbL)")
    names = {feature_key(f["properties"], level): f["properties"]["name"] for f in shown}
    if merged:
        st.caption(
            "Dawne jednostki zsumowane w obecnych granicach: "
            + ", ".join(f"{names.get(code, code)} ({count})" for code, count in merged.items()) + "."
        )
    if skipped:
        st.caption(
            f"Pominięto (szare), bo kilka dawnych jednostek leży w jednej obecnej, a wartości {value} "
            "nie da się zsumować: "
            + ", ".join(names.get(code, code) for code in skipped) + "."
        )


def _code_columns(rows: list[dict]) -> dict:
    """Show years, rounds, numbers and ids without thousand separators (2015, not 2,015)."""
    if not rows:
        return {}
    return {
        name: st.column_config.NumberColumn(format="%d")
        for name in rows[0]
        if (name in ("year", "round", "prev_year") or name.endswith(("_no", "_id")))
        and isinstance(rows[0][name], (int, float))
    }


def _render_answer(data: dict) -> None:
    """Polish answer text; LLM text is shown only after its numbers were checked against the rows."""
    answer = data.get("answer") or {}
    if not answer.get("text"):
        return
    st.markdown(f"**{answer['text']}**")
    if answer.get("source") == "llm":
        st.caption("✓ Każda liczba w tej odpowiedzi występuje w wyniku zapytania SQL.")
    else:
        st.caption("Podsumowanie złożone bezpośrednio z wyników zapytania.")
    for caveat in answer.get("caveats") or []:
        st.warning(caveat, icon="⚠️")


def _render_clarification(question: str, data: dict) -> None:
    """Buttons for the options of an ambiguous question; a click re-asks with the chosen entity."""
    clarification = data["clarification"]
    st.info(clarification.get("prompt") or "Doprecyzuj pytanie.")
    for i, option in enumerate(clarification.get("options") or []):
        if st.button(option["label"], key=f"clarify_{i}_{option['ref']}", use_container_width=True):
            refs = [{"kind": option["kind"], "ref": str(option["ref"])}]
            previous = st.session_state["last_ask"].get("asked_with")
            with st.spinner("Szukam odpowiedzi…"):
                chosen = _ask(question, refs, previous)
            if chosen is not None:
                _remember(question, chosen, refs, previous)
                st.rerun()


def _render_result_emotes(data: dict) -> None:
    """Small visual summary for the first row in results."""
    rows = data.get("result") or []
    if not rows:
        st.info("😕 Brak wyników dla tego pytania.")
        return

    first = rows[0] if isinstance(rows[0], dict) else {}
    candidate = first.get("candidate") or first.get("candidate_name")
    votes = first.get("votes")
    if candidate and votes is not None:
        st.success(f"🥇 **Top wynik:** {candidate} — **{votes:,}** głosów")
    else:
        st.caption("✅ Wynik gotowy.")


def _csv_cell(value: object) -> object:
    """CSV text of one value: decimal comma for fractions (Polish Excel), lists joined, None empty."""
    if value is None:
        return ""
    if isinstance(value, float):
        return repr(value).replace(".", ",")
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    return value


def _csv_bytes(rows: list[dict]) -> bytes:
    """Rows as CSV for Excel in Polish locale: ';' separator, decimal comma, UTF-8 with BOM."""
    buffer = io.StringIO()
    columns = list(dict.fromkeys(name for row in rows for name in row))
    writer = csv.writer(buffer, delimiter=";", lineterminator="\r\n")
    writer.writerow(columns)
    for row in rows:
        writer.writerow([_csv_cell(row.get(name)) for name in columns])
    return buffer.getvalue().encode("utf-8-sig")


def _csv_file_name(text: str) -> str:
    """File name from the question (ASCII, no diacritics) and today's date."""
    folded = unicodedata.normalize("NFKD", text.lower().replace("ł", "l")).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", folded).strip("-")[:60].rstrip("-") or "wynik"
    return f"plujka-{slug}-{date.today().isoformat()}.csv"


def _csv_download(rows: list[dict], name: str, key: str) -> None:
    """"Pobierz CSV" button exporting exactly these rows."""
    if rows:
        st.download_button(
            "Pobierz CSV", data=_csv_bytes(rows), file_name=_csv_file_name(name), mime="text/csv", key=key,
            help="Separator „;” i przecinek dziesiętny — plik otwiera się poprawnie w polskim Excelu.",
        )


def _persons_in_answer(data: dict, entity_refs: list[dict] | None) -> list[tuple[int, str]]:
    """Persons an answer refers to: clarification options, matched or chosen entities, person_id rows."""
    found: dict[int, str] = {}

    def add(ref: object, label: str | None) -> None:
        """Remember a person id (first label wins)."""
        try:
            person_id = int(ref)
        except (TypeError, ValueError):
            return
        found.setdefault(person_id, label or f"Osoba nr {person_id}")

    for option in (data.get("clarification") or {}).get("options") or []:
        if option.get("kind") == "person":
            add(option.get("ref"), option.get("label"))
    for entity in ((data.get("params") or {}).get("_dynamic_sql_meta") or {}).get("entities") or []:
        if entity.get("kind") == "person":
            add(entity.get("ref"), entity.get("name"))
    for ref in entity_refs or []:
        if ref.get("kind") == "person":
            add(ref.get("ref"), None)
    for row in data.get("result") or []:
        if isinstance(row, dict) and row.get("person_id") is not None:
            add(row["person_id"], row.get("full_name") or row.get("candidate") or row.get("candidate_name"))
    return list(found.items())[:MAX_PERSON_OPTIONS]


def _match_note(rule: str) -> str:
    """Polish explanation of mart.persons.match_rule."""
    if rule == "name+gender":
        return (
            "Kandydatury w wyborach ogólnokrajowych połączone po imieniu, nazwisku i płci. Kandydatury samorządowe "
            "dołączone, gdy imię i nazwisko są jednoznaczne, a lata urodzenia się zgadzają."
        )
    if rule == "name+gender+residence":
        return (
            "Kandydatury w wyborach ogólnokrajowych połączone po imieniu, nazwisku, płci i miejscu zamieszkania — "
            "w jednych wyborach startowało kilka osób o tym imieniu i nazwisku."
        )
    if rule == "name+gender+voivodeship":
        return "Kandydatury rozdzielone według województwa zamieszkania — jedno imię i różne regiony."
    if rule == "name+gender+birth year":
        return "Kandydatury rozdzielone według roku urodzenia z rejestrów 1991–1997."
    if rule == "surname+first given name+gender":
        return (
            "Kandydatury sprzed 2011 r. z jednym imieniem połączone z osobą o pełnych imionach (to samo nazwisko "
            "i pierwsze imię, potwierdzone miejscem zamieszkania lub rokiem urodzenia)."
        )
    if rule.startswith("local:"):
        area = "województwie" if rule.endswith("voivodeship") else "powiecie"
        return (
            "Osoba lokalna (bez startu w wyborach ogólnokrajowych): kandydatury samorządowe połączone po imieniu, "
            f"nazwisku, płci, roku urodzenia (wiek w dniu wyborów, ±1 rok) i {area}."
        )
    if rule == "manual":
        return "Połączenie ustalone ręcznie (korekta w danych, np. po zmianie nazwiska lub rozdzieleniu imienników)."
    return f"Reguła łączenia: {rule}."


def _fetch_person(person_id: int) -> dict | None:
    """``GET /persons/{id}``; shows a message and returns None on failure."""
    try:
        response = requests.get(f"{API_URL}/persons/{person_id}", headers=API_HEADERS, timeout=30)
    except requests.RequestException as error:
        st.error(f"Coś poszło nie tak z API: {error}")
        return None
    if response.status_code == 404:
        st.warning("Nie ma już tej osoby w bazie (baza mogła zostać przebudowana). Zadaj pytanie ponownie.")
        return None
    if response.status_code == 401:
        st.error("API wymaga klucza (ustaw PLUJKA_API_KEY dla interfejsu).")
        return None
    if response.status_code == 503:
        st.warning("Baza wyników jest chwilowo niedostępna. Spróbuj ponownie za chwilę.")
        return None
    if not response.ok:
        st.error(f"Coś poszło nie tak z API: HTTP {response.status_code}")
        return None
    return response.json()


def _api_get(path: str, params: dict | None = None, invalid: str = "Nieprawidłowe parametry.") -> Any:
    """GET an API endpoint; shows a message and returns None on failure (``invalid`` for HTTP 422)."""
    try:
        response = requests.get(f"{API_URL}{path}", params=params, headers=API_HEADERS, timeout=60)
    except requests.RequestException as error:
        st.error(f"Coś poszło nie tak z API: {error}")
        return None
    if response.status_code == 401:
        st.error("API wymaga klucza (ustaw PLUJKA_API_KEY dla interfejsu).")
        return None
    if response.status_code == 503:
        st.warning("Baza wyników jest chwilowo niedostępna. Spróbuj ponownie za chwilę.")
        return None
    if response.status_code == 422:
        st.info(invalid)
        return None
    if response.status_code == 404:
        return None
    if not response.ok:
        st.error(f"Coś poszło nie tak z API: HTTP {response.status_code}")
        return None
    return response.json()


def _search_persons(name: str) -> list[dict] | None:
    """``GET /persons?name=...``; shows a message and returns None on failure."""
    data = _api_get("/persons", {"name": name, "limit": PERSON_SEARCH_LIMIT},
                    invalid="Wpisz imię lub nazwisko (co najmniej 2 litery, bez cyfr).")
    return None if data is None else data.get("persons") or []


def _render_person_timeline(person_id: int, view_key: str = "person_view") -> None:
    """Every candidacy of one person, ordered by date, with how the candidacies were linked.

    ``view_key`` is the session key holding the shown person ("Zamknij" clears it); it also prefixes the
    widget keys, so the answer's timeline and the person search may show the same person at once.
    """
    payload = _fetch_person(person_id)
    if payload is None:
        return
    person, candidacies = payload["person"], payload["candidacies"]
    with st.container(border=True):
        head, close = st.columns([5, 1])
        head.markdown(f"#### 🗂️ {person['full_name']}")
        if close.button("Zamknij", key=f"{view_key}_close_{person_id}", use_container_width=True):
            st.session_state.pop(view_key, None)
            st.rerun()
        first, last = person.get("first_year"), person.get("last_year")
        c1, c2, c3 = st.columns(3)
        c1.metric("Kandydatury", person["candidacies"])
        c2.metric("Lata aktywności", "—" if first is None else (f"{first}–{last}" if first != last else str(first)))
        c3.metric("Razy wybrany(a)", person.get("elected_count", 0))
        st.info(f"{_match_note(person['match_rule'])}\n\n{PERSON_CAVEAT}", icon="🔗")
        rows = [
            {
                "Data": c["election_date"] or str(c["year"]),
                "Wybory": CHAMBER_LABELS.get(c["chamber"], c["chamber"]),
                "Tura": c["round"] if c["chamber"] in ("prezydent", "wbp") else None,
                "Okręg": c["district_no"],
                "Obszar": c["area"],
                "Komitet": c["committee_name"],
                "Miejsce na liście": c["list_position"],
                "Głosy": c["votes"],
                "Wybrany": None if c["elected"] is None else ("tak" if c["elected"] else "nie"),
                "Wiek": c["age"],
            }
            for c in candidacies
        ]
        if rows:
            chart = (
                alt.Chart(pd.DataFrame(rows))
                .mark_point(filled=True, size=140, opacity=0.9)
                .encode(
                    x=alt.X(
                        "Data:T", title=None, axis=alt.Axis(format="%Y", tickCount="year"), scale=alt.Scale(padding=16)
                    ),
                    y=alt.Y(
                        "Wybory:N", title=None, sort=list(dict.fromkeys(r["Wybory"] for r in rows)),
                        axis=alt.Axis(labelLimit=260),
                    ),
                    color=alt.Color(
                        "Wybrany:N", title="Wybrany", scale=alt.Scale(domain=["tak", "nie"], range=["#16a34a", "#94a3b8"]),
                        legend=alt.Legend(orient="bottom"),
                    ),
                    tooltip=["Data", "Wybory", "Tura", "Obszar", "Okręg", "Komitet", "Głosy", "Wybrany"],
                )
                .properties(height=alt.Step(40))
            )
            st.altair_chart(chart, use_container_width=True)
        st.dataframe(rows, use_container_width=True, hide_index=True)
        _csv_download(rows, f"kandydatury {person['full_name']}", key=f"csv_{view_key}_{person_id}")


def _render_person_links(data: dict, entity_refs: list[dict] | None, key: str) -> None:
    """"Zobacz wszystkie kandydatury" for persons the answer refers to, then the chosen person's timeline."""
    persons = _persons_in_answer(data, entity_refs)
    if persons:
        labels = dict(persons)
        with st.container(border=True):
            st.markdown("###### Osoby w odpowiedzi")
            if len(persons) == 1:
                chosen = persons[0][0]
                st.caption(labels[chosen])
            else:
                chosen = st.selectbox(
                    "Osoba", [p for p, _ in persons], format_func=labels.get, key=f"person_pick_{key}"
                )
            if st.button("Zobacz wszystkie kandydatury", key=f"person_open_{key}", use_container_width=True):
                st.session_state["person_view"] = chosen
    if st.session_state.get("person_view"):
        _render_person_timeline(st.session_state["person_view"])


@st.fragment
def _person_search() -> None:
    """"Szukaj osoby": persons by name without asking a question, then the chosen person's timeline.

    A fragment, so typing and picking here rerun only this box, not the answer below. Its session keys
    (``person_search_*``) are separate from the answer's ``person_view``.
    """
    with st.expander("🔎 Szukaj osoby", expanded=False):
        name = st.text_input(
            "Imię i nazwisko", placeholder="np. Kwaśniewski · Szymon Hołow", key="person_search_name"
        ).strip()
        if st.session_state.get("person_search_for") != name:
            # A new search closes the timeline of the previous one.
            st.session_state["person_search_for"] = name
            st.session_state.pop("person_search_view", None)
        if not name:
            st.caption("Wpisz imię i nazwisko albo sam początek nazwiska, a potem Enter.")
            return
        if sum(c.isalpha() for c in name) < 2:
            st.info("Wpisz co najmniej 2 litery imienia lub nazwiska.")
            return
        persons = _search_persons(name)
        if persons is None:
            return
        if not persons:
            st.info(f"Nie znaleziono osoby „{name}”. Sprawdź pisownię albo wpisz sam początek nazwiska.")
            return
        more = " (pierwsze wyniki — doprecyzuj, by zawęzić)" if len(persons) >= PERSON_SEARCH_LIMIT else ""
        st.caption(f"Znalezione osoby: {len(persons)}{more}")
        # A long list scrolls inside the box, so the timeline below stays close to the search field.
        with st.container(height=340 if len(persons) > 3 else None):
            for person in persons:
                text, button = st.columns([5, 1], vertical_alignment="center")
                text.markdown(f"**{person['full_name']}** · kandydatury: {person['candidacies']}")
                if person.get("detail"):
                    text.caption(person["detail"])
                if button.button("Pokaż", key=f"person_search_open_{person['person_id']}", use_container_width=True):
                    st.session_state["person_search_view"] = person["person_id"]
        if st.session_state.get("person_search_view"):
            _render_person_timeline(st.session_state["person_search_view"], view_key="person_search_view")


st.set_page_config(
    page_title="Plujka KBW",
    page_icon="🗳️",
    layout="wide",
    initial_sidebar_state="collapsed",
)

st.markdown(
    """
    <style>
        :root {
            --plujka-ink: #1e1b4b;
            --plujka-muted: #64748b;
            --plujka-line: rgba(99, 102, 241, 0.15);
            --plujka-glow: rgba(99, 102, 241, 0.08);
        }
        .main .block-container {
            padding-top: 2rem;
            padding-bottom: 3rem;
            max-width: 880px;
        }
        h1 { letter-spacing: -0.03em !important; font-weight: 700 !important; color: var(--plujka-ink) !important; }
        .plujka-hero {
            background: linear-gradient(125deg, #f4f7ff 0%, #fef6ff 48%, #fff9f3 100%);
            border: 1px solid var(--plujka-line);
            border-radius: 18px;
            padding: 1.35rem 1.5rem 1.5rem;
            margin-bottom: 1.25rem;
            box-shadow: 0 12px 40px var(--plujka-glow);
        }
        .plujka-hero h2 {
            margin: 0;
            font-size: 1.65rem;
            font-weight: 700;
            color: var(--plujka-ink);
            line-height: 1.2;
        }
        .plujka-hero p {
            margin: 0.45rem 0 0 0;
            color: var(--plujka-muted);
            font-size: 0.98rem;
            line-height: 1.45;
        }
    </style>
    """,
    unsafe_allow_html=True,
)

st.markdown(
    """
    <div class="plujka-hero">
        <h2>Pluj faktami</h2>
    </div>
    """,
    unsafe_allow_html=True,
)


ELECTION_LABELS = {
    "sejm": "Sejm", "senat": "Senat", "prezydent": "Prezydent", "pe": "Parlament Europejski",
    "referendum": "Referendum", "sejmik": "Sejmiki", "wbp": "Wójtowie / burmistrzowie", "rada": "Rady",
}
RESULT_ELECTIONS = ("sejm", "sejmik", "pe", "prezydent")
CHANGE_ELECTIONS = ("sejm", "sejmik", "pe")
LEVEL_LABELS = {"country": "Kraj", "wojewodztwo": "Województwa", "powiat": "Powiaty", "gmina": "Gminy",
                "wojewodztwo_1975": "Województwa 1975–1998"}
UNIT_NAME = {"wojewodztwo": "wojewodztwo", "powiat": "powiat", "gmina": "gmina", "wojewodztwo_1975": "wojewodztwo"}
UNIT_SINGULAR = {"wojewodztwo": "województwo", "powiat": "powiat", "gmina": "gmina"}
UNIT_CODE = {"powiat": "teryt_powiat", "gmina": "teryt_gmina"}


def _election_card(kind: str, year: int, rnd: int) -> None:
    """Key facts of the selected election: turnout, precincts, districts, seats, method, thresholds."""
    card = _api_get(f"/elections/{kind}/{year}", {"round": rnd})
    if not card:
        return
    turnout = (card.get("turnout") or {}).get("turnout_pct")
    facts = [("Frekwencja", f"{turnout:.2f}%".replace(".", ",") if turnout is not None else None),
             ("Obwody", card.get("precincts")), ("Gminy", card.get("gminas")), ("Okręgi", card.get("districts")),
             ("Mandaty", card.get("seats"))]
    facts = [(label, value) for label, value in facts if value is not None]
    for column, (label, value) in zip(st.columns(len(facts)), facts):
        column.metric(label, f"{value:,}".replace(",", " ") if isinstance(value, int) else value)
    notes = [f"{card['election_date']}" if card.get("election_date") else None,
             f"metoda: {card['seat_method']}" if card.get("seat_method") else None,
             "progi: " + " / ".join(f"{t:g}%" for t in card["thresholds_pct"]) if card.get("thresholds_pct") else None]
    st.caption(" · ".join(n for n in notes if n))


def _unit_rows(rows: list[dict], level: str, value: str) -> list[dict]:
    """Rows for the chart and map: unit name, its code and one value (the map reads the code column)."""
    keep = [UNIT_NAME[level]] + ([UNIT_CODE[level]] if level in UNIT_CODE else []) + ["year", value]
    return [{k: r.get(k) for k in keep} for r in rows]


# "Przeglądaj wyniki" state in the page URL (?wybory=sejm&rok=2023&...), so a view can be shared.
BROWSE_LINK = {"wybory": "browse_kind", "rok": "browse_year", "tura": "browse_round", "pokaz": "browse_view",
               "poziom": "browse_level", "woj": "browse_woj"}


def _from_link(key: str, options: list) -> None:
    """Preselect widget ``key`` from the page URL once, when the linked value is one of ``options``."""
    param = next(p for p, k in BROWSE_LINK.items() if k == key)
    value = st.query_params.get(param)
    if value is None or st.session_state.get(f"_linked_{key}"):
        return
    st.session_state[f"_linked_{key}"] = True
    match = next((o for o in options if str(o) == value), None)
    if match is not None:
        st.session_state[key] = match


@st.fragment
def _browse() -> None:
    """"Przeglądaj wyniki": turnout or results of one election per unit, with fixed SQL (no LLM)."""
    with st.expander("📊 Przeglądaj wyniki (bez pytania)", expanded="wybory" in st.query_params):
        elections = _api_get("/elections")
        if not elections:
            st.caption("Brak załadowanych wyborów.")
            return
        kinds = [k for k in ELECTION_LABELS if any(e["election"] == k for e in elections) and k != "rada"]
        c1, c2, c3 = st.columns(3)
        _from_link("browse_kind", kinds)
        kind = c1.selectbox("Wybory", kinds, format_func=ELECTION_LABELS.get, key="browse_kind")
        years = sorted({e["year"] for e in elections if e["election"] == kind}, reverse=True)
        _from_link("browse_year", years)
        year = c2.selectbox("Rok", years, key="browse_year")
        rounds = sorted({e["round"] for e in elections if e["election"] == kind and e["year"] == year})
        _from_link("browse_round", rounds)
        rnd = c3.selectbox("Tura", rounds, key="browse_round") if len(rounds) > 1 else rounds[0]
        _election_card(kind, year, rnd)
        c4, c5, c6 = st.columns(3)
        views = ["Wyniki", "Frekwencja"] if kind in RESULT_ELECTIONS else ["Frekwencja"]
        if kind in CHANGE_ELECTIONS and year > min(years):
            views.insert(1, "Zmiana")
        if kind in ("prezydent", "wbp") and 2 in rounds:
            views.insert(1, "II tura")
        if kind == "sejm":
            views.insert(-1, "Głosy zmarnowane")
        _from_link("browse_view", views)
        view = c4.radio("Pokaż", views, horizontal=True, key="browse_view")
        levels = [k for k in LEVEL_LABELS if year < 1999 or k != "wojewodztwo_1975"]
        _from_link("browse_level", levels)
        level = c5.selectbox("Poziom", levels, index=1, format_func=LEVEL_LABELS.get, key="browse_level")
        if year < 1999 and level in ("powiat", "gmina"):
            st.caption("Gminy sprzed 1999 r. przypisane do dzisiejszych przez mart.gmina_codes_pre1999; przed 1995 r. "
                       "gmina obejmuje też gminy wydzielone później, a dzielnice Warszawy z lat 1990–1994 są tylko w powiecie.")
        voivodeship = None
        if level in ("powiat", "gmina"):
            if "woj" in st.query_params and not st.session_state.get("_linked_browse_woj"):
                st.session_state["_linked_browse_woj"] = True
                st.session_state["browse_woj"] = st.query_params["woj"][:40]
            voivodeship = c6.text_input("Województwo (opcjonalnie)", placeholder="np. mazowieckie", key="browse_woj")
        params = {"election": kind, "year": year, "round": rnd, "level": level,
                  "wojewodztwo": (voivodeship or "").strip().lower() or None}
        link = {"wybory": kind, "rok": year, "tura": rnd, "pokaz": view, "poziom": level, "woj": params["wojewodztwo"]}
        st.query_params.from_dict({k: str(v) for k, v in link.items() if v is not None})
        st.caption("Adres strony w przeglądarce otwiera ten widok — skopiuj go, aby go udostępnić.")
        if view == "Głosy zmarnowane":
            data = _api_get("/wasted", {k: v for k, v in params.items() if k not in ("round", "election")})
            rows = (data or {}).get("rows") or []
            if not rows:
                st.caption("Brak danych dla tego wyboru.")
                return
            value = "wasted_pct"
            st.caption("Głosy na komitety poniżej progu krajowego (bez udziału w podziale mandatów), w % ważnych głosów na listy.")
            shown = ([{k: r[k] for k in ("valid_votes", "wasted_votes", "wasted_pct")} for r in rows] if level == "country"
                     else _unit_rows(rows, level, value))
        elif view == "II tura":
            if kind == "wbp" and level != "gmina":
                st.caption("II tura wyborów wójtów i burmistrzów jest tylko na poziomie gmin.")
                level = params["level"] = "gmina"
            data = _api_get("/runoff", {k: v for k, v in params.items() if k != "round"})
            rows = (data or {}).get("rows") or []
            if not rows:
                st.caption("Brak danych dla tego wyboru.")
                return
            value = "gain_pp"
            st.caption("Przyrost poparcia finalistów między turami (punkty procentowe i głosy). Wyniki nie pokazują, "
                       "kto na kogo przeszedł, tylko zmianę netto.")
            if level == "country":
                shown = [{k: r[k] for k in ("name", "votes_round1", "pct_round1", "votes", "pct", "gain_votes", "gain_pp")}
                         for r in rows]
            elif kind == "wbp":
                # Each mayoral finalist runs in one gmina: show the winner of each run-off.
                winners: dict[str, dict] = {}
                for r in rows:
                    if r["teryt_gmina"] not in winners or r["pct"] > winners[r["teryt_gmina"]]["pct"]:
                        winners[r["teryt_gmina"]] = r
                st.caption("Na mapie: przyrost zwycięzcy II tury w każdej gminie.")
                shown = [{"gmina": r["gmina"], "teryt_gmina": r["teryt_gmina"], "zwycięzca": r["name"], "year": r["year"],
                          value: r[value]} for r in winners.values()]
            else:
                names = list(dict.fromkeys(r["name"] for r in rows))
                name = st.selectbox("Kandydat na mapie", names, key="browse_runoff_name")
                shown = _unit_rows([r for r in rows if r["name"] == name], level, value)
        elif view == "Zmiana":
            data = _api_get("/change", {k: v for k, v in params.items() if k != "round"})
            rows = (data or {}).get("rows") or []
            if not rows:
                st.caption("Brak danych dla tego wyboru.")
                return
            value = "change_pp"
            st.caption(f"Zmiana w punktach procentowych względem wyborów z {rows[0]['prev_year']} r.; "
                       "„porównywalny” = ten sam komitet wiodący w obu wyborach.")
            if level == "country":
                shown = [{"name": r["name"], "pct": r["pct"], "prev_pct": r["prev_pct"], "change_pp": r["change_pp"],
                          "porównywalny": r["comparable"]} for r in rows]
            else:
                names = list(dict.fromkeys(r["name"] for r in rows))
                name = st.selectbox("Komitet na mapie", names, key="browse_change_name")
                shown = _unit_rows([r for r in rows if r["name"] == name], level, value)
        elif view == "Frekwencja":
            data = _api_get("/turnout", params)
            rows = (data or {}).get("rows") or []
            if not rows:
                st.caption("Brak danych dla tego wyboru.")
                return
            value = "turnout_pct"
            shown = rows if level == "country" else _unit_rows(rows, level, value)
        else:
            data = _api_get("/results", params)
            rows = (data or {}).get("rows") or []
            if not rows:
                st.caption("Brak danych dla tego wyboru.")
                return
            names = list(dict.fromkeys(r["name"] for r in rows))
            value = "pct"
            if level == "country":
                shown = [{"name": r["name"], "votes": r["votes"], "pct": r["pct"]} for r in rows]
            else:
                name = st.selectbox("Komitet / kandydat na mapie", names, key="browse_name")
                shown = _unit_rows([r for r in rows if r["name"] == name], level, value)
        if level != "country":
            order = st.radio("Jednostki", ["Wszystkie", "20 najwyższych", "20 najniższych"], horizontal=True, key="browse_rank")
            if order != "Wszystkie":
                ranked = sorted((r for r in shown if r.get(value) is not None), key=lambda r: r[value],
                                reverse=order == "20 najwyższych")
                shown = ranked[:20]
        # The year column stays in the rows for the map (former extents by year), not in the table.
        st.dataframe(pd.DataFrame(shown).drop(columns=["year"], errors="ignore"), hide_index=True, use_container_width=True)
        _csv_download(rows, f"{kind}_{year}_{view}_{level}", key="csv_browse")
        _render_chart(shown)
        _render_map(shown, key="browse")


@st.fragment
def _unit_card() -> None:
    """"Karta jednostki": one gmina, powiat or voivodeship across every election, with GUS statistics."""
    with st.expander("🏛️ Karta gminy / powiatu / województwa", expanded=False):
        name = st.text_input("Nazwa jednostki", placeholder="np. Wieliczka · krakowski · śląskie", key="unit_search").strip()
        if len(name) < 2:
            st.caption("Wpisz początek nazwy gminy, powiatu lub województwa.")
            return
        hits = _api_get("/units", {"name": name}) or []
        if not hits:
            st.caption(f"Nie znaleziono jednostki „{name}”.")
            return
        hit = st.selectbox(
            "Jednostka", hits, key="unit_pick",
            format_func=lambda h: f"{h['name']} ({UNIT_SINGULAR[h['level']]}{', ' + h['detail'] if h.get('detail') else ''})",
        )
        card = _api_get(f"/units/{hit['unit']}")
        if not card:
            st.caption("Brak wyników dla tej jednostki (np. jednostka spoza okresu w bazie).")
            return
        st.subheader(card["name"] or hit["name"])
        gus = card.get("gus")
        if gus:
            m1, m2, m3 = st.columns(3)
            m1.metric(f"Ludność ({gus['year']})", f"{gus['population']:,}".replace(",", " ") if gus.get("population") else "—")
            m2.metric("Powierzchnia", f"{gus['area_km2']} km²" if gus.get("area_km2") else "—")
            share = gus.get("unemployed_share_working_age")
            m3.metric("Bezrobotni (% w wieku prod.)", f"{share}%" if share is not None else "—")
            st.caption("Źródło: GUS, Bank Danych Lokalnych (CC BY 4.0).")
        turnout = pd.DataFrame(card["turnout"])
        if not turnout.empty:
            turnout["wybory"] = turnout["election"].map(ELECTION_LABELS) + turnout["round"].map(lambda r: "" if r == 1 else " (II tura)")
            chart = (
                alt.Chart(turnout)
                .mark_line(point=True)
                .encode(x=alt.X("year:Q", title=None, axis=alt.Axis(format="d"), scale=alt.Scale(zero=False)), y=alt.Y("turnout_pct:Q", title="frekwencja %", scale=alt.Scale(zero=False)),
                        color=alt.Color("wybory:N", title=None, legend=alt.Legend(labelLimit=300)), tooltip=["wybory", "year", "turnout_pct"])
                .properties(height=280, padding={"left": 5, "right": 5, "top": 5, "bottom": 25})
            )
            st.altair_chart(chart, use_container_width=True)
        _compare_units(card)
        if card["level"] == "gmina":
            mayors = _api_get(f"/mayors/gmina/{card['unit']}") or []
            if mayors:
                st.markdown("**Wójtowie / burmistrzowie**")
                st.dataframe(pd.DataFrame(mayors).assign(year=lambda d: d["year"].astype(str))[
                    ["year", "round", "full_name", "committee_name", "pct", "same_as_before"]].rename(columns={
                    "year": "rok", "round": "tura", "full_name": "imię i nazwisko", "committee_name": "komitet",
                    "pct": "%", "same_as_before": "ta sama osoba co wcześniej"}), hide_index=True, use_container_width=True)
        winners = pd.DataFrame(card["winners"])
        if not winners.empty:
            winners = winners.sort_values(["year", "election", "round"], ascending=[False, True, True])
            winners["election"] = winners["election"].map(ELECTION_LABELS)
            winners["year"] = winners["year"].astype(str)
            st.markdown("**Zwycięzcy**")
            st.dataframe(winners.rename(columns={"election": "wybory", "year": "rok", "round": "tura", "name": "komitet / kandydat",
                                                 "votes": "głosy", "pct": "%"}), hide_index=True, use_container_width=True)


SEAT_METHODS = {
    "dhondt": "D'Hondt", "sainte_lague": "Sainte-Laguë", "sainte_lague_modified": "Sainte-Laguë zmodyfikowana (1,4)",
    "hare_niemeyer": "Hare-Niemeyer",
}


def _compare_units(card: dict) -> None:
    """Turnout of the unit next to a second unit, for one kind of election."""
    other = st.text_input("Porównaj z jednostką", placeholder="np. Niepołomice", key="unit_compare").strip()
    if len(other) < 2:
        return
    hits = _api_get("/units", {"name": other}) or []
    if not hits:
        st.caption(f"Nie znaleziono jednostki „{other}”.")
        return
    hit = st.selectbox("Druga jednostka", hits, key="unit_compare_pick",
                       format_func=lambda h: f"{h['name']} ({UNIT_SINGULAR[h['level']]}{', ' + h['detail'] if h.get('detail') else ''})")
    second = _api_get(f"/units/{hit['unit']}")
    if not second:
        st.caption("Brak wyników dla drugiej jednostki.")
        return
    frames = []
    for c in (card, second):
        frame = pd.DataFrame(c["turnout"])
        frame["jednostka"] = c["name"] or c["unit"]
        frames.append(frame)
    both = pd.concat(frames)
    kinds = [k for k in ELECTION_LABELS if k in set(both["election"])]
    kind = st.selectbox("Wybory do porównania", kinds, format_func=ELECTION_LABELS.get, key="unit_compare_kind")
    both = both[(both["election"] == kind) & (both["round"] == 1)]
    chart = (
        alt.Chart(both)
        .mark_line(point=True)
        .encode(x=alt.X("year:Q", title=None, axis=alt.Axis(format="d"), scale=alt.Scale(zero=False)),
                y=alt.Y("turnout_pct:Q", title="frekwencja %", scale=alt.Scale(zero=False)),
                color=alt.Color("jednostka:N", title=None), tooltip=["jednostka", "year", "turnout_pct"])
        .properties(height=240, padding={"left": 5, "right": 5, "top": 5, "bottom": 25})
    )
    st.altair_chart(chart, use_container_width=True)


@st.fragment
def _seat_simulator() -> None:
    """"Symulator mandatów": Sejm seats under another method, threshold or one national district."""
    with st.expander("🧮 Symulator mandatów do Sejmu", expanded=False):
        elections = _api_get("/elections") or []
        years = sorted({e["year"] for e in elections if e["election"] == "sejm" and e["year"] >= 2001}, reverse=True)
        if not years:
            st.caption("Brak wyborów do Sejmu od 2001 r.")
            return
        c1, c2 = st.columns(2)
        year = c1.selectbox("Wybory do Sejmu", years, key="seats_year")
        method = c2.selectbox("Metoda", list(SEAT_METHODS), format_func=SEAT_METHODS.get, key="seats_method")
        c3, c4 = st.columns(2)
        threshold = c3.slider("Próg dla komitetów (%)", 0.0, 15.0, 5.0, 0.5, key="seats_threshold")
        coalition = c4.slider("Próg dla koalicji (%)", 0.0, 15.0, 8.0, 0.5, key="seats_coalition")
        c5, c6 = st.columns(2)
        minority = c5.checkbox("Mniejszości narodowe bez progu", value=True, key="seats_minority")
        national = c6.checkbox("Jeden okręg krajowy (460 mandatów)", value=False, key="seats_national")
        committees = {r["committee_ids"][0]: r["party"] for r in _api_get("/seats/simulate", {"year": year}) or []}
        merge = st.multiselect("Połącz komitety we wspólną listę (próg dla koalicji)", list(committees),
                               format_func=committees.get, key=f"seats_merge_{year}")
        rows = _api_get("/seats/simulate", {"year": year, "method": method, "threshold": threshold,
                                            "coalition_threshold": coalition, "exempt_minority": minority,
                                            "national": national, "merge": merge if len(merge) > 1 else []})
        if not rows:
            return
        table = pd.DataFrame(rows)[["party", "votes", "pct", "seats_official", "seats_simulated"]]
        table["zmiana"] = table["seats_simulated"] - table["seats_official"]
        table["głosy na mandat"] = (table["votes"] / table["seats_simulated"].where(table["seats_simulated"] > 0)).round(0)
        wasted = int(table.loc[table["seats_simulated"] == 0, "votes"].sum())
        total = int(table["votes"].sum())
        shown = table[(table["seats_official"] > 0) | (table["seats_simulated"] > 0)]
        st.dataframe(shown.rename(columns={"party": "komitet", "votes": "głosy", "pct": "%", "seats_official": "mandaty (oficjalnie)",
                                           "seats_simulated": "mandaty (symulacja)"}), hide_index=True, use_container_width=True)
        long = shown.melt(id_vars="party", value_vars=["seats_official", "seats_simulated"], var_name="wariant", value_name="mandaty")
        long["wariant"] = long["wariant"].map({"seats_official": "oficjalnie", "seats_simulated": "symulacja"})
        chart = (
            alt.Chart(long)
            .mark_bar()
            .encode(y=alt.Y("party:N", title=None, sort="-x"), x=alt.X("mandaty:Q"), color=alt.Color("wariant:N", title=None),
                    yOffset="wariant:N", tooltip=["party", "wariant", "mandaty"])
            .properties(height=max(160, 52 * len(shown)), padding={"left": 5, "right": 5, "top": 5, "bottom": 25})
        )
        st.altair_chart(chart, use_container_width=True)
        st.caption(f"Głosy na komitety bez mandatu: {wasted:,} z {total:,} ({100 * wasted / total:.1f}%). ".replace(",", " ")
                   + "Przeliczone z oficjalnych głosów na listy (mart.allocate_seats); próg liczony od głosów ważnych na listy w kraju."
                   + (" Połączona lista ma sumę głosów połączonych komitetów, tak jakby wyborcy głosowali tak samo." if len(merge) > 1 else ""))


CHAMBER_LABELS = {
    "sejm": "Sejm", "senat": "Senat", "pe": "Parlament Europejski", "prezydent": "Prezydent", "sejmik": "Sejmiki",
    "wbp": "Wójtowie / burmistrzowie", "rada_gminy": "Rady gmin", "rada_powiatu": "Rady powiatów",
    "rada_dzielnicy": "Rady dzielnic Warszawy",
}


@st.fragment
def _candidates() -> None:
    """"Kandydaci": women's share and age of candidates and elected per election year."""
    with st.expander("👥 Kandydaci: kobiety i wiek", expanded=False):
        chamber = st.selectbox("Wybory", list(CHAMBER_LABELS), format_func=CHAMBER_LABELS.get, key="cand_chamber")
        rows = _api_get("/candidates/stats", {"chamber": chamber}) or []
        if not rows:
            st.caption("Brak danych.")
            return
        stats = pd.DataFrame(rows)
        long = stats.melt(id_vars="year", value_vars=["women_pct", "elected_women_pct"], var_name="grupa", value_name="kobiety %")
        long["grupa"] = long["grupa"].map({"women_pct": "kandydaci", "elected_women_pct": "wybrani"})
        chart = (
            alt.Chart(long.dropna())
            .mark_line(point=True)
            .encode(x=alt.X("year:Q", title=None, axis=alt.Axis(format="d"), scale=alt.Scale(zero=False)),
                    y=alt.Y("kobiety %:Q"), color=alt.Color("grupa:N", title=None), tooltip=["year", "grupa", "kobiety %"])
            .properties(height=240, padding={"left": 5, "right": 5, "top": 5, "bottom": 25})
        )
        st.altair_chart(chart, use_container_width=True)
        table = stats.assign(year=stats["year"].astype(str)).rename(columns={
            "year": "rok", "candidates": "kandydaci", "women_pct": "kobiety %", "elected": "wybrani",
            "elected_women_pct": "kobiety wśród wybranych %", "median_age": "mediana wieku",
            "elected_median_age": "mediana wieku wybranych"})
        st.dataframe(table.dropna(axis=1, how="all"), hide_index=True, use_container_width=True)
        if chamber == "wbp":
            _mayor_terms()
        aged = [r["year"] for r in rows if r.get("median_age") is not None]
        if not aged:
            st.caption("Pliki PKW dla tych wyborów nie podają wieku kandydatów.")
            return
        c1, c2 = st.columns(2)
        year = c1.selectbox("Rok", sorted(aged, reverse=True), key="cand_year")
        elected = c2.checkbox("Tylko wybrani", key="cand_elected")
        ages = _api_get("/candidates/ages", {"chamber": chamber, "year": year, "elected": elected}) or []
        columns = {"full_name": "imię i nazwisko", "age": "wiek", "area": "obszar", "committee_name": "komitet", "elected": "wybrany"}
        for end, title in (("youngest", "Najmłodsi"), ("oldest", "Najstarsi")):
            st.markdown(f"**{title}**")
            part = pd.DataFrame([r for r in ages if r["end_"] == end])
            if not part.empty:
                st.dataframe(part[list(columns)].rename(columns=columns), hide_index=True, use_container_width=True)
        st.caption("Wiek z dnia wyborów, jak w plikach PKW (wybory samorządowe od 1998 r., Sejm i Senat 1991–1997).")


def _mayor_terms() -> None:
    """How many regular elections mayors won since 2002, and the longest-serving ones."""
    terms = _api_get("/mayors/terms") or {}
    if not terms.get("top"):
        return
    st.markdown("**Liczba wygranych wyborów na wójta / burmistrza (od 2002 r.)**")
    histogram = pd.DataFrame([{"kadencje": int(k), "osoby": v} for k, v in terms["histogram"].items()]).sort_values("kadencje")
    st.dataframe(histogram, hide_index=True, use_container_width=True)
    top = pd.DataFrame(terms["top"])
    top["lata"] = top["years"].map(lambda ys: ", ".join(map(str, ys)))
    top["gminy"] = top["gminas"].map(lambda gs: ", ".join(g for g in gs if g))
    st.dataframe(top[["full_name", "terms", "lata", "gminy"]].rename(columns={"full_name": "imię i nazwisko", "terms": "kadencje"}),
                 hide_index=True, use_container_width=True)
    st.caption("Wybory zwykłe (bez przedterminowych); osoby łączone po imionach, nazwisku, płci, roku urodzenia i powiecie.")


@st.fragment
def _elected() -> None:
    """"Wybrani": elected candidates of one election by district, with a committee filter."""
    with st.expander("🗳️ Wybrani według okręgu", expanded=False):
        elections = _api_get("/elections") or []
        kinds = [k for k in ("sejm", "senat", "pe", "sejmik") if any(e["election"] == k for e in elections)]
        if not kinds:
            return
        c1, c2 = st.columns(2)
        chamber = c1.selectbox("Wybory", kinds, format_func=CHAMBER_LABELS.get, key="elected_chamber")
        years = sorted({e["year"] for e in elections if e["election"] == chamber}, reverse=True)
        year = c2.selectbox("Rok", years, key="elected_year")
        rows = _api_get("/elected", {"chamber": chamber, "year": year}) or []
        if not rows:
            st.caption("Brak wybranych w danych.")
            return
        table = pd.DataFrame(rows)
        c3, c4 = st.columns(2)
        parties = ["wszystkie", *table["party"].value_counts().index]
        party = c3.selectbox("Komitet", parties, key=f"elected_party_{chamber}_{year}")
        districts = ["wszystkie", *sorted(table["district_no"].dropna().astype(int).unique())]
        district = c4.selectbox("Okręg", districts, key=f"elected_district_{chamber}_{year}")
        if party != "wszystkie":
            table = table[table["party"] == party]
        if district != "wszystkie":
            table = table[table["district_no"] == district]
        st.caption(f"{len(table)} wybranych.")
        columns = {"area": "województwo", "district_no": "okręg", "party": "komitet", "full_name": "imię i nazwisko",
                   "list_position": "miejsce na liście", "votes": "głosy"}
        st.dataframe(table[[c for c in columns if table[c].notna().any()]].rename(columns=columns),
                     hide_index=True, use_container_width=True)


RANKING_LABELS = {"candidacies": "liczba kandydowań", "elected": "liczba wygranych", "career": "długość kariery",
                  "votes": "suma głosów"}


@st.fragment
def _rankings() -> None:
    """"Rankingi osób": top persons by candidacies, wins, career or votes, and moves between party families."""
    with st.expander("🏅 Rankingi osób i zmiany komitetów", expanded=False):
        c1, c2 = st.columns(2)
        by = c1.selectbox("Ranking według", list(RANKING_LABELS), format_func=RANKING_LABELS.get, key="rank_by")
        chamber = c2.selectbox("Wybory", [None, *CHAMBER_LABELS], key="rank_chamber",
                               format_func=lambda c: "osoby z kandydowaniem krajowym (wszystkie wybory)" if c is None else CHAMBER_LABELS[c])
        rows = _api_get("/rankings/persons", {"by": by, **({"chamber": chamber} if chamber else {})}) or []
        if rows:
            table = pd.DataFrame(rows)
            table["wybory"] = table["chambers"].map(lambda cs: ", ".join(CHAMBER_LABELS.get(c, c) for c in cs))
            table["lata"] = table["first_year"].astype(str) + "–" + table["last_year"].astype(str)
            st.dataframe(table[["full_name", "candidacies", "elected", "lata", "career_years", "votes", "wybory"]].rename(columns={
                "full_name": "imię i nazwisko", "candidacies": "kandydowania", "elected": "wygrane", "career_years": "lat kariery",
                "votes": "głosy"}), hide_index=True, use_container_width=True)
            st.caption("Głosy: suma ze wszystkich kandydowań i tur. Osoby łączone po imionach i nazwisku (zob. „Szukaj osoby”).")
        st.markdown("**Przejścia między rodzinami partyjnymi (Sejm, Senat, PE)**")
        since = st.slider("Kandydowania od roku", 1991, 2023, 1991, key="switch_since")
        switches = _api_get("/rankings/party-switches", {"since": since}) or []
        if switches:
            flows = pd.DataFrame(switches)
            flows["przykłady"] = flows["examples"].map(", ".join)
            st.dataframe(flows[["from_family", "to_family", "persons", "przykłady"]].rename(columns={
                "from_family": "z", "to_family": "do", "persons": "osoby"}), hide_index=True, use_container_width=True)
            st.caption("Kolejne kandydowania tej samej osoby z list różnych rodzin (mart.party_families); listy koalicyjne "
                       "liczą się jako rodzina komitetu (np. Razem na liście Lewicy).")


@st.fragment
def _data_coverage() -> None:
    """"Co jest w bazie": loaded elections and years (``GET /elections``)."""
    with st.expander("ℹ️ Co jest w bazie", expanded=False):
        elections = _api_get("/elections") or []
        years: dict[str, list[int]] = {}
        for e in elections:
            years.setdefault(e["election"], [])
            if e["year"] not in years[e["election"]]:
                years[e["election"]].append(e["year"])
        st.dataframe(
            pd.DataFrame([{"wybory": ELECTION_LABELS.get(k, k), "lata": ", ".join(map(str, v))} for k, v in years.items()]),
            hide_index=True, use_container_width=True,
        )
        version = _api_get("/data-version") or {}
        loaded = (version.get("mart_loaded_at") or "")[:10]
        sources = (version.get("sources_last_modified") or "")[:10]
        st.caption("Dane PKW/KBW; statystyki GUS dla gmin z lat 1995–2024. Znane luki: docs/MART.md."
                   + (f" Baza zbudowana {loaded}; {version['source_files']} plików źródłowych, najnowszy z {sources}." if loaded else ""))


@st.fragment
def _data_dictionary() -> None:
    """"Słownik danych": mart relations and columns with their descriptions (``GET /data-dictionary``)."""
    with st.expander("📖 Słownik danych", expanded=False):
        relations = _api_get("/data-dictionary") or []
        if not relations:
            return
        search = st.text_input("Szukaj tabeli lub kolumny", placeholder="np. turnout, teryt_gmina", key="dict_search").strip().lower()
        if search:
            relations = [r for r in relations if search in r["relation"] or search in (r["comment"] or "").lower()
                         or any(search in c["name"] for c in r["columns"])]
        if not relations:
            st.caption("Nic nie pasuje.")
            return
        names = [r["relation"] for r in relations]
        name = st.selectbox(f"Relacja ({len(names)})", names, key="dict_relation")
        rel = next(r for r in relations if r["relation"] == name)
        st.markdown(f"**mart.{rel['relation']}** ({rel['kind']})")
        st.caption(rel["comment"] or "")
        st.dataframe(pd.DataFrame(rel["columns"]).rename(columns={"name": "kolumna", "type": "typ", "comment": "opis"}),
                     hide_index=True, use_container_width=True)
        flat = [{"relation": r["relation"], "kind": r["kind"], "relation_comment": r["comment"], **{f"column_{k}": v for k, v in c.items()}}
                for r in relations for c in r["columns"]]
        _csv_download(flat, "slownik_danych", key="csv_dictionary")
        st.caption("Opisy po angielsku, te same, które czyta model piszący SQL.")


@st.fragment
def _gaps_and_sources() -> None:
    """"Znane luki i źródła": documented data gaps, answer caveats and the source files by republication date."""
    with st.expander("⚠️ Znane luki w danych i pliki źródłowe", expanded=False):
        gaps_tab, sources_tab = st.tabs(["Znane luki", "Pliki źródłowe"])
        with gaps_tab:
            gaps = _api_get("/known-gaps") or {}
            if gaps.get("caveats"):
                st.markdown("**Uwagi dodawane do odpowiedzi, gdy luka ich dotyczy**")
                st.markdown("\n".join(f"- {c}" for c in gaps["caveats"]))
            if gaps.get("markdown"):
                st.markdown("**Szczegóły (docs/MART.md, po angielsku)**")
                st.markdown(gaps["markdown"])
        with sources_tab:
            files = _api_get("/sources", {"limit": 200}) or []
            if files:
                table = pd.DataFrame(files)
                table["last_modified"] = table["last_modified"].str[:10]
                st.dataframe(table[["last_modified", "path", "bytes", "url"]].rename(columns={
                    "last_modified": "opublikowano", "path": "plik", "bytes": "bajty", "url": "adres"}),
                    hide_index=True, use_container_width=True, column_config={"adres": st.column_config.LinkColumn("adres")})
                st.caption("Pliki KBW/PKW, z których zbudowano bazę, od ostatnio (ponownie) opublikowanych. "
                           "Ponowną publikację wykrywa scripts/check_sources.py.")


@st.fragment(run_every=_READINESS_POLL_INTERVAL)
def _api_data_readiness_banner() -> None:
    """Api data readiness banner."""
    if st.session_state.get("_plujka_api_data_ready"):
        st.success("**Baza gotowa** — możesz korzystać z pełnych danych KBW.")
        return

    try:
        response = requests.get(f"{API_URL}/health", timeout=5)
        response.raise_for_status()
        payload = response.json()
    except requests.RequestException:
        st.error("Nie mogę połączyć się z API. Sprawdź, czy backend działa (`/health`).")
        return
    if payload.get("data_ready"):
        st.session_state["_plujka_api_data_ready"] = True
        st.success("**Baza gotowa** — możesz korzystać z pełnych danych KBW.")
    else:
        st.warning(
            "**Brak danych KBW w bazie** — uruchom import kontenerem `loader` "
            "(profil `tools`), np. `docker compose --profile tools run --rm loader`. "
            "Status odświeża się automatycznie po załadowaniu `kbw_facts`."
        )


_api_data_readiness_banner()


@st.fragment(run_every=_DIAG_POLL_INTERVAL)
def _catalog_inventory_expander() -> None:
    """Catalog summary — refreshes on interval without full-page rerun."""
    with st.expander("Inwentaryzacja mirror KBW (`kbw_dane_files`)", expanded=False):
        cat = _fetch_catalog_summary()
        if cat is None:
            st.caption("Nie udało się pobrać `/kbw/catalog/summary` — sprawdź API.")
        else:
            total = int(cat.get("total_files") or 0)
            st.metric("Pliki zapisane w katalogu", total)
            col_y, col_k = st.columns(2)
            with col_y:
                st.caption("Według roku (ścieżka)")
                st.json(cat.get("by_year") or {})
            with col_k:
                st.caption("Według rodzaju pliku")
                st.json(cat.get("by_file_kind") or {})
            st.caption(
                "Wypełniane przy profilowaniu mirroru (`kbw_catalog`). Import faktów jest osobnym krokiem."
            )


@st.fragment(run_every=_DIAG_POLL_INTERVAL)
def _kbw_table_stats_expander() -> None:
    """Approximate KBW table sizes — refreshes on interval."""
    with st.expander("Wiersze w tabelach KBW (przybliżenie, `pg_stat`)", expanded=False):
        hp = _fetch_health_with_kb_stats()
        if hp is None:
            st.caption("Nie udało się pobrać `/health?details=1`.")
        else:
            st.caption(f"`data_ready`: **{hp.get('data_ready')}**")
            stats = hp.get("kbw_stats")
            if stats:
                st.json(stats)
            else:
                st.caption(
                    "Brak pola `kbw_stats` — uruchom ponownie API lub poczekaj na ANALYZE po imporcie."
                )
            st.caption("Źródło: `pg_stat_user_tables.n_live_tup` (odświeżane po DML / ANALYZE).")


_catalog_inventory_expander()
_kbw_table_stats_expander()

with st.container(border=True):
    question = st.text_input(
        "Twoje pytanie",
        placeholder="np. Ile głosów miała KO w 2023? · posłowie PiS z Wrocławia",
        label_visibility="visible",
        key="plujka_question_input",
    )
    q_val = (question or "").strip()
    earlier = st.session_state.get("last_ask")
    # A follow-up ("a w 2019?") reuses the previous answered question and its SQL.
    can_follow = bool(earlier and earlier["data"].get("intent") == "dynamic_sql" and earlier["data"].get("sql"))
    follow_up = can_follow and st.toggle(
        "Dopytaj o poprzedni wynik",
        value=_looks_like_follow_up(q_val),
        help=f"Poprzednie pytanie: {earlier['question']}" if can_follow else None,
    )
    submitted = st.button("Wyślij zapytanie", type="primary", use_container_width=True)

_person_search()
_browse()
_unit_card()
_seat_simulator()
_candidates()
_elected()
_rankings()
_data_coverage()
_data_dictionary()
_gaps_and_sources()

if submitted:
    if not q_val:
        st.warning("Wpisz treść pytania.")
    else:
        previous = earlier["previous"] if follow_up else None
        with st.spinner("Szukam odpowiedzi…"):
            data = _ask(q_val, previous=previous)
        if data is None:
            st.stop()
        _remember(q_val, data, None, previous)
        # A clarification re-asks the same turn, so it keeps the same previous turn.
        st.session_state["last_ask"]["asked_with"] = previous

_history_sidebar()
last = st.session_state.get("last_ask")
if last:
    q = last["question"]
    data = last["data"]
    fp = _feedback_fingerprint(q, data)

    st.markdown("##### Wynik")
    entity_refs = last["previous"].get("entity_refs")
    if data.get("clarification"):
        _render_clarification(q, data)
        _render_person_links(data, entity_refs, fp)
        st.stop()
    _render_answer(data)
    _render_result_emotes(data)
    with st.container(border=True):
        st.dataframe(
            data["result"], use_container_width=True, hide_index=True, column_config=_code_columns(data["result"])
        )
        _csv_download(data["result"], q, key=f"csv_{fp}")
    _render_person_links(data, entity_refs, fp)
    if data.get("result"):
        _render_chart(data["result"])
        _render_map(data["result"])

    with st.expander("Co zrozumiał system", expanded=False):
        c1, c2 = st.columns(2)
        with c1:
            st.caption("Intencja")
            st.code(data["intent"], language="text")
        with c2:
            st.caption("Encja")
            st.write(data.get("entity") or "—")
        geo_src = data.get("candidate_geo_source")
        mand_src = data.get("mandate_extremes_source")
        if geo_src is not None or mand_src is not None:
            st.caption("Źródło danych (meta)")
            meta_lines = []
            if geo_src is not None:
                meta_lines.append(f"`candidate_geo_source` → **{geo_src}**")
            if mand_src is not None:
                meta_lines.append(f"`mandate_extremes_source` → **{mand_src}**")
            st.markdown(" · ".join(meta_lines))

    with st.expander("Zapytanie SQL i źródło danych", expanded=False):
        st.caption("Każda liczba w wyniku pochodzi z tego zapytania do bazy:")
        st.code(data["sql"], language="sql", wrap_lines=True)
        st.markdown(
            "Źródło: oficjalne wyniki [PKW](https://wybory.gov.pl) i "
            "[KBW — dane wyborcze](https://danewyborcze.kbw.gov.pl), wczytane do bazy bez zmian."
        )
        if "gus_" in (data.get("sql") or "").lower():
            # GUS Local Data Bank is CC BY 4.0: attribute whenever an answer reads it.
            st.markdown(
                "Dane statystyczne: [GUS, Bank Danych Lokalnych](https://bdl.stat.gov.pl) "
                "(licencja [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/deed.pl))."
            )
        st.caption("Parametry (diagnostyka)")
        st.json(data["params"])

    _intent = data.get("intent") or ""
    if _intent == "dynamic_sql":
        _gus = " · statystyki: GUS BDL (CC BY 4.0)" if "gus_" in (data.get("sql") or "").lower() else ""
        st.caption(f"Dane: PKW/KBW{_gus} · zapytanie SQL napisane do pytania i sprawdzone przed uruchomieniem (tylko odczyt).")
    elif _intent == "kbw_candidate_geo_votes_detail":
        st.caption(
            "Dane: KBW — głosy imienne/poziom gminy lub obwodu z plików kandydackich; "
            "`candidate_geo_source` w sekcji „Co zrozumiał system” mówi, czy użyto indeksu "
            "`kbw_candidate_geo_votes`, czy skanu `kbw_facts`."
        )
    else:
        st.caption(
            "Dane: KBW · Odpowiedzi mogą być przybliżone do poziomu okręgu sejmowego, nie do gminy."
        )

    st.markdown("###### Oceń odpowiedź")
    if st.session_state.get("_feedback_ack_fp") == fp:
        st.caption("Dziękujemy — ocena została zapisana.")
    else:
        if st.button("👍 Dobra odpowiedź", key=f"fb_up_{fp}", use_container_width=True):
            try:
                fb = requests.post(
                    f"{API_URL}/feedback",
                    headers=API_HEADERS,
                    json={"rating": "thumbs_up", "question": q, "ask_response": data},
                    timeout=30,
                )
                fb.raise_for_status()
                st.session_state["_feedback_ack_fp"] = fp
                st.rerun()
            except requests.RequestException as err:
                st.warning(f"Nie udało się zapisać oceny: {err}")
        st.caption(
            "Jeśli wynik jest błędny, niepełny albo nie na temat, wyślij go do poprawy. "
            "Zapiszemy pytanie, SQL i odpowiedź, żeby poprawić logikę systemu."
        )
        if st.button("⚠️ Zgłoś odpowiedź do poprawy", key=f"fb_down_{fp}", use_container_width=True):
            try:
                fb = requests.post(
                    f"{API_URL}/feedback",
                    headers=API_HEADERS,
                    json={
                        "rating": "thumbs_down",
                        "question": q,
                        "ask_response": data,
                    },
                    timeout=30,
                )
                fb.raise_for_status()
                st.session_state["_feedback_ack_fp"] = fp
                st.rerun()
            except requests.RequestException as err:
                st.warning(f"Nie udało się zapisać zgłoszenia: {err}")

st.divider()
