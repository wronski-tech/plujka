from __future__ import annotations

import hashlib
import json
import math
import os
from datetime import timedelta

import altair as alt
import pandas as pd
import pydeck as pdk
import requests
import streamlit as st

from map_units import boundaries, feature_key, is_osm, unit_values, visible_features

API_URL = os.getenv("API_URL", "http://localhost:8000")
# Sent as X-API-Key when the API requires keys (API_KEYS on the API side).
API_KEY = os.getenv("PLUJKA_API_KEY", "")
API_HEADERS = {"X-API-Key": API_KEY} if API_KEY else {}
# Result column that identifies a unit -> boundary file (gminy/powiaty by TERYT, voivodeships by name).
MAP_LEVELS = (("teryt_gmina", "gminy"), ("teryt_powiat", "powiaty"), ("wojewodztwo", "wojewodztwa"))
# Charts and maps are drawn only for results that fit on screen.
MAX_CHART_ROWS = 40

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


def _numeric_columns(rows: list[dict]) -> list[str]:
    """Columns with numbers that are values, not codes (years, rounds, numbers, ids)."""
    first = rows[0]
    return [
        name for name, value in first.items()
        if isinstance(value, (int, float)) and not isinstance(value, bool)
        and name not in ("year", "round", "prev_year") and not name.endswith(("_no", "_id"))
        and not name.startswith("teryt")
    ]


def _value_column(rows: list[dict]) -> str | None:
    """Column to chart: a percentage first, then votes, then any other number."""
    numeric = _numeric_columns(rows)
    for pattern in ("pct", "votes", "seats", "mandat"):
        for name in numeric:
            if pattern in name:
                return name
    return numeric[0] if numeric else None


def _render_chart(rows: list[dict]) -> None:
    """Horizontal bar chart when each row is one labelled unit with a number (e.g. results by party)."""
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
    # Old TERYT codes are mapped to current units; Warsaw, split gminas and changed powiats use their former
    # extent when the result describes the old layout.
    values, merged, skipped = unit_values(rows, column, value, level)
    years = {r["year"] for r in rows if isinstance(r.get("year"), int)}
    shown = visible_features(boundaries(level)["features"], set(values) | set(skipped), years)
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
    if data.get("clarification"):
        _render_clarification(q, data)
        st.stop()
    _render_answer(data)
    _render_result_emotes(data)
    with st.container(border=True):
        st.dataframe(
            data["result"], use_container_width=True, hide_index=True, column_config=_code_columns(data["result"])
        )
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
        st.caption("Parametry (diagnostyka)")
        st.json(data["params"])

    _intent = data.get("intent") or ""
    if _intent == "dynamic_sql":
        st.caption("Dane: PKW/KBW · zapytanie SQL napisane do pytania i sprawdzone przed uruchomieniem (tylko odczyt).")
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
