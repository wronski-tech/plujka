# Architektura analityczna (pytania złożone)

## Cel

Obsłużyć pytania wykraczające poza prosty ranking list (`total_votes_by_candidate`), przy **źródle prawdy w PostgreSQL** i deterministycznym routingu (SQL z szablonów / parametrów, opcjonalnie dynamic SQL z allowlistą).

## Stan implementacji (roadmapa)

| Etap | Element | Status |
|------|---------|--------|
| Krótkoterminowe intenty KBW | `kbw_max_turnout_precinct`, `kbw_committee_gap_by_district`, `kbw_coalition_candidate_vote_sum` | **Zrobione** (`api/services/kbw_analytics.py`, routing w `llm.py` / `router.py`) |
| Domyślny rok zapytań | `elections` lub fallback `kbw_election_runs` | **Zrobione** (`db.default_sejm_year_for_queries`) |
| Widok po okręgu | `kbw_v_sejm_district_list_agg` | **Zrobione** (`db.init_database`) — używany w zapytaniu PO vs PiS |
| Statystyki po imporcie | `ANALYZE` na tabelach KBW | **Zrobione** (`import_kbw_facts.py` → `db.analyze_kbw_tables`) |
| Ekstrema głosów „weszli / nie weszli” | `kbw_sejm_mandate_vote_extremes` | **Zrobione** — preferuje `elected_candidates` + `sejm_candidate_ballots`; **fallback KBW** (`kbw_national_*`, `kbw_district_*`) gdy PKW puste (`kbw_analytics.sql_sejm_mandate_vote_extremes_from_kbw_facts`) |
| Tabela pod osoby / lata | `kbw_person_election_fact` | **Backfill z faktów** — `db.backfill_kbw_person_election_facts`, skrypt `scripts/backfill_kbw_person_facts.py` (agregaty po nazwie z nagłówka; bez pełnej identyfikacji osoby) |
| Relacyjne tabele `kbw_candidates`, … | Pełny ETL z Parquet | **Schemat + sync z person facts** (`db.sync_kbw_candidates_from_person_facts`, `--sync-kbw-candidates` przy imporcie); pełny loader Parquet → [`ANALYTICS_ROADMAP_PHASE2.md`](./ANALYTICS_ROADMAP_PHASE2.md) |
| Głosy kandydata po jednostce (gmina/obwód) | `kbw_candidate_geo_votes` | **Backfill z `kbw_facts`** — `db.backfill_kbw_candidate_geo_votes_from_facts`, flaga `--backfill-candidate-geo-votes`; geografia w JOINie do `kbw_facts` |
| Zapytania NL o gminę + kandydata | intent **`kbw_candidate_geo_votes_detail`** | **Zrobione** — preferuje `kbw_candidate_geo_votes`; przy pustym wyniku **fallback** na skan `kbw_facts` (`sql_candidate_geo_votes_detail_from_facts`); pole odpowiedzi `candidate_geo_source` |
| Wspólne wyrażenie okręgu | `district_expr_sql` w `kbw_geo.py` | **Zrobione** — widok `kbw_v_sejm_district_list_agg` i zapytania analityczne używają tej samej definicji |

## Warstwy

| Warstwa | Opis | Przykład |
|--------|------|----------|
| **A — Lookup** | Jedna lista / jeden rok / filtr nazwy | „wynik PiS 2023” |
| **B — Agregat ranking** | MIN/MAX/TOP po jednostce geograficznej lub metryce | „komisja z najwyższą frekwencją” |
| **C — Porównanie dwóch szeregów** | Dwie partie / dwie listy w tej samej granularności | „różnica PO vs PiS po okręgach” |
| **D — Łączenie kandydat ↔ mandat** | Wyniki imienne + reguły mandatowe / protokoły | „kto wszedł / nie wszedł”, Dutkiewicz w latach |
| **E — Ścieżki czasowe / zmiana party** | Ta sama osoba w wielu wyborach | lista posłów zmieniających partię |

Warstwy **D–E** wymagają albo **wzbogaconego modelu** (tabele kandydatów, mandaty, kadencje), albo **dedykowanych plików KBW** i importu poza samym `kbw_facts` EAV.

## Mapowanie przykładów

### 1. Wynik PiS

- **Warstwa:** A  
- **Dane:** `kbw_facts` + filtr na `subject->>'column'` (lista), rok z pytania.  
- **Status:** routing aliasów komitetów (`llm.COMMITTEE_ALIASES`) + `votes_for_candidate`.

### 2. Komisja z najwyższą frekwencją

- **Warstwa:** B  
- **Dane:** wiersze z frekwencją jako **%** (`is_percentage = true`) lub kolumny z „frekw” w nazwie; geografia = obwód/komisja w `geography` JSON.  
- **Implementacja:** intent `kbw_max_turnout_precinct`.

### 3. Głosy na kandydatów razem na liście koalicyjnej

- **Warstwa:** B (suma po wielu kolumnach-kandydatach jednej listy)  
- **Dane:** pliki typu `wyniki_gl_na_kandydatow_*` — w `kbw_facts` każda kolumna-kandydat to osobny fakt.  
- **Implementacja:** intent `kbw_coalition_candidate_vote_sum` (heurystyka ścieżki + `ILIKE` na nagłówku kolumny).  
- **Docelowo:** wypełniać `kbw_person_election_fact` lub tabela mapowania kolumn → lista.

### 4. Okręg z najmniejszą / największą różnicą PO vs PiS

- **Warstwa:** C  
- **Implementacja:** intent `kbw_committee_gap_by_district` — agregacja z widoku `kbw_v_sejm_district_list_agg`.

### 5. Posłowie z najmniejszą / największą liczbą głosów — weszli / nie weszli

- **Warstwa:** D  
- **Implementacja:** intent `kbw_sejm_mandate_vote_extremes` — cztery wiersze: min/max w `elected_candidates`, min/max w `sejm_candidate_ballots` z wykluczeniem wpisów mandatowych.  
- **Uwaga:** przy samym imporcie KBW bez seedu PKW te tabele mogą być **puste** — trzeba zasilić z protokołów lub osobnego jobu.

### Przyszłość: Dutkiewicz; zmiana partii

- **Warstwa:** E  
- **Tabela:** `kbw_person_election_fact` — przygotowana pod loader (np. normalizacja `person_key`, lata, `party_list_label`).  
- Wymaga encji „osoba” i spójności między latami — poza samym EAV `kbw_facts`.

## Kierunek rozwoju modelu

1. ~~Krótkoterminowo: intenty + widok agregujący~~ (zrobione).  
2. ~~Średnioterminowo: widok po okręgu~~ → `kbw_v_sejm_district_list_agg`.  
3. Długoterminowo: osobne tabele relacyjne (`kbw_candidates`, `kbw_committee_results`, `kbw_mandates`) budowane loaderem z Parquet/CSV — szczegóły i kolejność prac: **[`ANALYTICS_ROADMAP_PHASE2.md`](./ANALYTICS_ROADMAP_PHASE2.md)**.

## Powiązanie z loaderem

Zmiana schematu PG lub sensu kolumn wymaga aktualizacji **`kbw_import.py`** i przebudowy obrazu **`loader`**. Po dużym imporcie uruchamiane jest **`ANALYZE`** na tabelach KBW.

Opcjonalnie ten sam przebieg co `scripts/import_kbw_facts.py` może od razu zasilić tabele pomocnicze: **`--backfill-person-facts`**, **`--backfill-candidate-geo-votes`**, **`--sync-kbw-candidates`**, albo skrót **`--all-kbw-backfills`** (wszystkie trzy, po `ANALYZE`).
