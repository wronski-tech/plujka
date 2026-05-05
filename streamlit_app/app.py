from __future__ import annotations

import os
from datetime import timedelta

import requests
import streamlit as st

API_URL = os.getenv("API_URL", "http://localhost:8000")

st.set_page_config(
    page_title="Plujka PKW",
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
        div[data-testid="stForm"] {
            border: 1px solid var(--plujka-line);
            border-radius: 14px;
            padding: 1rem 1.1rem 1.15rem;
            background: #ffffffcc;
            box-shadow: 0 4px 24px rgba(15, 23, 42, 0.04);
        }
        /* Primary submit: softer indigo */
        div[data-testid="stForm"] button[kind="primary"] {
            background: linear-gradient(180deg, #6366f1 0%, #4f46e5 100%);
            border: none;
            font-weight: 600;
            border-radius: 10px;
        }
        div[data-testid="stForm"] button[kind="primary"]:hover {
            background: linear-gradient(180deg, #7c3aed 0%, #5b21b6 100%);
        }
    </style>
    """,
    unsafe_allow_html=True,
)

st.markdown(
    """
    <div class="plujka-hero">
        <h2>Pluj faktami</h2>
        <p>Pytania o wyniki wyborów → intencja → SQL → PostgreSQL. Zadaj pytanie po polsku.</p>
    </div>
    """,
    unsafe_allow_html=True,
)


@st.fragment(run_every=timedelta(seconds=3))
def _api_data_readiness_banner() -> None:
    try:
        response = requests.get(f"{API_URL}/health", timeout=5)
        response.raise_for_status()
        payload = response.json()
    except requests.RequestException:
        st.error("Nie mogę połączyć się z API. Sprawdź, czy backend działa (`/health`).")
        return
    if payload.get("data_ready"):
        st.success("**Baza gotowa** — możesz korzystać z pełnych danych PKW.")
    else:
        st.warning("**Ładowanie danych** — trwa import (seed). Za chwilę odświeżę status…")


_api_data_readiness_banner()

with st.form("ask_form", clear_on_submit=False):
    question = st.text_input(
        "Twoje pytanie",
        placeholder="np. Ile głosów miała KO w 2023? · posłowie PiS z Wrocławia",
        label_visibility="visible",
    )
    submitted = st.form_submit_button("Wyślij zapytanie", type="primary", use_container_width=True)

if submitted and question.strip():
    with st.spinner("Szukam odpowiedzi…"):
        try:
            response = requests.post(f"{API_URL}/ask", json={"question": question}, timeout=60)
            response.raise_for_status()
            data = response.json()
        except requests.RequestException as error:
            st.error(f"Coś poszło nie tak z API: {error}")
            st.stop()

    st.markdown("##### Wynik")
    with st.container(border=True):
        st.dataframe(data["result"], use_container_width=True, hide_index=True)

    with st.expander("Co zrozumiał system", expanded=False):
        c1, c2 = st.columns(2)
        with c1:
            st.caption("Intencja")
            st.code(data["intent"], language="text")
        with c2:
            st.caption("Encja")
            st.write(data.get("entity") or "—")

    with st.expander("Szczegóły SQL (debug)", expanded=False):
        st.code(data["sql"], language="sql")
        st.caption("Parametry")
        st.json(data["params"])

    st.caption("Dane: PKW · Odpowiedzi mogą być przybliżone do poziomu okręgu sejmowego, nie do gminy.")

st.divider()
st.caption("Plujka · Streamlit + FastAPI + PostgreSQL")
