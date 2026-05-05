from __future__ import annotations

import os

import requests
import streamlit as st

API_URL = os.getenv("API_URL", "http://localhost:8000")

st.set_page_config(page_title="Plujka PKW AI", layout="wide")
st.title("PKW AI: Pytanie -> Intencja -> SQL -> PostgreSQL")

question = st.text_input("Pytanie", placeholder="Ile głosów ma KO?")

if st.button("Zapytaj") and question.strip():
    with st.spinner("Wysyłam zapytanie do API..."):
        try:
            response = requests.post(f"{API_URL}/ask", json={"question": question}, timeout=60)
            response.raise_for_status()
            data = response.json()
        except requests.RequestException as error:
            st.error(f"API jest chwilowo niedostępne lub zwróciło błąd: {error}")
            st.stop()

    st.subheader("Wykryta intencja")
    st.code(data["intent"])

    st.subheader("Dopasowana encja")
    st.write(data.get("entity"))

    st.subheader("Szablon SQL")
    st.code(data["sql"], language="sql")

    st.subheader("Parametry SQL")
    st.json(data["params"])

    st.subheader("Wynik")
    st.dataframe(data["result"], use_container_width=True)
