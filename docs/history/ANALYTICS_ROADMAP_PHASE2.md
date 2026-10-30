# Analytics — roadmap Phase 2 (po obecnym modelu KBW EAV)

Dokument uzupełnia [`ANALYTICS_ARCHITECTURE.md`](./ANALYTICS_ARCHITECTURE.md): co sensownie zrobić **po** już działających intentach, widoku okręgowym, fallbackach KBW i backfillu `kbw_person_election_fact`.

## 1. Relacyjny model kandydata (docelowy)

| Artefakt | Cel |
|----------|-----|
| `kbw_candidates` | Jedna encja kandydata na wybory (powiązanie z `election_run_id`, nazwa znormalizowana, opcjonalnie `person_id`) — **schemat + sync z person facts:** `db.sync_kbw_candidates_from_person_facts`, flaga importu `--sync-kbw-candidates`, skrypt `scripts/sync_kbw_candidates.py` |
| `kbw_committee_list` | Lista / komitet w danym runie |
| `kbw_candidate_results` | Głosy po jednostce (gmina / obwód) lub zdenormalizowany rollup — **częściowo:** `kbw_candidate_geo_votes` (PK = `kbw_facts.id`, JOIN po `geography`) |
| Źródło | Istniejący staging Parquet + [`kbw_import.py`](../api/services/kbw_import.py) |

**Kryterium ukończenia:** jednoznaczne powiązanie „ta sama osoba” między latami bez polegania wyłącznie na md5 nazwy z nagłówka CSV.

## 2. Mandaty i „weszli / nie weszli” bez PKW

| Opcja | Opis |
|-------|------|
| A | Import protokołów / oficjalnych tabel mandatowych do `elected_candidates` + `sejm_candidate_ballots` z tych samych źródeł co PKW |
| B | Obliczenie mandatu z danych KBW (D’Hondt po okręgu) — duży zakres; osobny moduł reguł wyborczych |

**Kryterium ukończenia:** intent `kbw_sejm_mandate_vote_extremes` zwraca sensowne `entered_*` / `not_entered_*` bez fallbacku `kbw_*` przy typowym imporcie mirror.

## 3. `kbw_person_election_fact` — jakość danych

- Rozróżnienie homonimów (ten sam string w nagłówku, dwie osoby).
- Mapowanie na zewnętrzny identyfikator (np. PESEL w protokołach — jeśli kiedyś dostępny tylko offline, nie w mirrorze).
- Pola `elected` i `party_list_label` wypełniane z mandatów / list, nie tylko NULL / `''`.

## 4. Testy integracyjne

- **`tests/test_integration_kbw_db.py`** — smoke na schemacie (widok, puste backfille); `PLUJKA_RUN_DB_TESTS=1`.
- **`tests/test_integration_kbw_fixture.py`** — jeden syntetyczny fakt kandydacki (rok 2099), asercje na backfillu, widoku okręgowym, `sql_candidate_geo_votes_detail_from_facts`, oraz **`route_question`** → `kbw_candidate_geo_votes_detail` z `candidate_geo_source=kbw_facts` (mock intentu, żeby uniknąć losowości heurystyki nazwiska).
- CI: job **integration** w `.github/workflows/ci.yml` (Postgres pgvector).

## 5. Observability

- Metryki: liczba faktów KBW, czas importu, rozmiar `kbw_person_election_fact` po backfillu.
- Log przy wyborze fallbacku mandate (PKW pusto) — już widoczny po stronie wyniku (`bucket` z prefiksem `kbw_`).

---

**Priorytety sugerowane:** (1) relacyjny model minimalny dla Sejmu, (2) test integracyjny na fixture, (3) mandaty z PKW lub heurystyka importu, (4) ulepszenia person_key.
