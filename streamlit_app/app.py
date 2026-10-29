from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import os
import re
import unicodedata
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


def _render_map(rows: list[dict]) -> None:
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
    features, matched_points = [], []
    for feature in shown:
        props = feature["properties"]
        found = values.get(feature_key(props, level))
        if found is None:
            fill = [226, 232, 240, 90]
        else:
            share = (found - low) / (high - low) if high > low else 1.0
            # Light indigo (#e0e7ff) for the lowest value to dark indigo (#4338ca) for the highest.
            fill = [int(224 - 157 * share), int(231 - 175 * share), int(255 - 53 * share), 230]
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
    st.caption(f"Kolor: {value} (jaśniej {low:g} → ciemniej {high:g}). Szare jednostki nie występują w wyniku.")
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


def _search_persons(name: str) -> list[dict] | None:
    """``GET /persons?name=...``; shows a message and returns None on failure."""
    try:
        response = requests.get(
            f"{API_URL}/persons", params={"name": name, "limit": PERSON_SEARCH_LIMIT}, headers=API_HEADERS, timeout=30
        )
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
        st.info("Wpisz imię lub nazwisko (co najmniej 2 litery, bez cyfr).")
        return None
    if not response.ok:
        st.error(f"Coś poszło nie tak z API: HTTP {response.status_code}")
        return None
    return response.json().get("persons") or []


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
