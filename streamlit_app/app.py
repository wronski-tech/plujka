from __future__ import annotations

import hashlib
import json
import os
from datetime import timedelta

import requests
import streamlit as st

API_URL = os.getenv("API_URL", "http://localhost:8000")

# How often the readiness fragment may refresh while data is still loading (avoid hammering /health).
_READINESS_POLL_INTERVAL = timedelta(seconds=15)


def _feedback_fingerprint(question: str, data: dict) -> str:
    blob = json.dumps(
        {
            "question": question,
            "intent": data.get("intent"),
            "sql": data.get("sql"),
            "params": data.get("params"),
        },
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(blob.encode()).hexdigest()[:24]


def _fetch_question_hints(question: str, exclude: str | None = None, *, limit: int = 8) -> dict:
    try:
        response = requests.post(
            f"{API_URL}/question-hints",
            json={"q": question, "limit": limit, "exclude_question": exclude},
            timeout=20,
        )
        response.raise_for_status()
        return response.json()
    except requests.RequestException:
        return {"text_hits": [], "semantic_hits": []}


def _hint_button_label(text: str, max_len: int = 52) -> str:
    stripped = text.strip()
    if len(stripped) <= max_len:
        return stripped
    return stripped[: max_len - 1] + "…"


def _render_hint_buttons(
    hits: list,
    key_prefix: str,
    *,
    empty_caption: str | None = None,
) -> None:
    if not hits:
        if empty_caption:
            st.caption(empty_caption)
        return
    for i, hit in enumerate(hits):
        qtext = hit.get("question") or ""
        if not qtext.strip():
            continue
        if st.button(
            _hint_button_label(qtext),
            key=f"{key_prefix}_{i}_{hashlib.md5(qtext.encode()).hexdigest()[:12]}",
            help=qtext,
            use_container_width=True,
        ):
            st.session_state["plujka_question_input"] = qtext
            st.rerun()


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
    if st.session_state.get("_plujka_api_data_ready"):
        st.success("**Baza gotowa** — możesz korzystać z pełnych danych PKW.")
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
        st.success("**Baza gotowa** — możesz korzystać z pełnych danych PKW.")
    else:
        st.warning("**Ładowanie danych** — trwa import (seed). Za chwilę odświeżę status…")


_api_data_readiness_banner()

with st.container(border=True):
    question = st.text_input(
        "Twoje pytanie",
        placeholder="np. Ile głosów miała KO w 2023? · posłowie PiS z Wrocławia",
        label_visibility="visible",
        key="plujka_question_input",
    )
    submitted = st.button("Wyślij zapytanie", type="primary", use_container_width=True)

q_val = (question or "").strip()

if len(q_val) >= 2:
    hc_key = f"h:{q_val}"
    if st.session_state.get("_typing_hints_key") != hc_key:
        st.session_state["_typing_hints_key"] = hc_key
        st.session_state["_typing_hints_val"] = _fetch_question_hints(q_val)
    typing_hints = st.session_state.get("_typing_hints_val") or {"text_hits": [], "semantic_hits": []}
    st.markdown("###### Podpowiedzi — podobne słowa (OpenSearch)")
    _render_hint_buttons(
        typing_hints.get("text_hits") or [],
        "th_txt",
        empty_caption="Brak dopasowań w historii zapytań — wpisz dłuższy fragment lub zadaj pierwsze pytanie.",
    )
    st.markdown("###### Podpowiedzi — podobne znaczeniowo (kNN)")
    _render_hint_buttons(
        typing_hints.get("semantic_hits") or [],
        "th_sem",
        empty_caption="Brak wyników kNN (krótka fraza lub mało podobnych pytań w historii).",
    )

if submitted:
    if not q_val:
        st.warning("Wpisz treść pytania.")
    else:
        with st.spinner("Szukam odpowiedzi…"):
            try:
                response = requests.post(f"{API_URL}/ask", json={"question": q_val}, timeout=60)
                response.raise_for_status()
                data = response.json()
            except requests.RequestException as error:
                st.error(f"Coś poszło nie tak z API: {error}")
                st.stop()

        st.session_state["last_ask"] = {"question": q_val, "data": data}
        st.session_state.pop("_feedback_ack_fp", None)

last = st.session_state.get("last_ask")
if last:
    q = last["question"]
    data = last["data"]
    fp = _feedback_fingerprint(q, data)

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

    rk = f"rel:{fp}"
    if st.session_state.get("_related_hist_key") != rk:
        st.session_state["_related_hist_key"] = rk
        st.session_state["_related_hist_val"] = _fetch_question_hints(q, exclude_question=q)
    related_hints = st.session_state.get("_related_hist_val") or {"text_hits": [], "semantic_hits": []}
    rel_text = related_hints.get("text_hits") or []
    rel_sem = related_hints.get("semantic_hits") or []
    if rel_text or rel_sem:
        st.markdown("###### Inne z historii zapytań (OpenSearch)")
        if rel_text:
            st.caption("Podobne słowa — możesz kliknąć i zmienić pytanie w polu powyżej.")
            _render_hint_buttons(rel_text, "rel_txt", empty_caption=None)
        if rel_sem:
            st.caption("Podobne znaczeniowo (kNN)")
            _render_hint_buttons(rel_sem, "rel_sem", empty_caption=None)

    st.markdown("###### Ocena odpowiedzi")
    if st.session_state.get("_feedback_ack_fp") == fp:
        st.caption("Dziękujemy za opinię.")
    else:
        up_c, down_c = st.columns(2)
        with up_c:
            if st.button("👍 Pomocna", key=f"fb_up_{fp}", use_container_width=True):
                try:
                    fb = requests.post(
                        f"{API_URL}/feedback",
                        json={"rating": "thumbs_up", "question": q},
                        timeout=15,
                    )
                    fb.raise_for_status()
                    st.session_state["_feedback_ack_fp"] = fp
                    st.rerun()
                except requests.RequestException as err:
                    st.warning(f"Nie udało się zapisać opinii: {err}")
        with down_c:
            if st.button("👎 Nie satysfakcjonuje — zapisz do poprawy", key=f"fb_down_{fp}", use_container_width=True):
                try:
                    fb = requests.post(
                        f"{API_URL}/feedback",
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
                    st.warning(f"Nie udało się zapisać opinii: {err}")

st.divider()
