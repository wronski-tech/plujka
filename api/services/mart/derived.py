"""Relations derived from everything loaded (rebuilt in PostgreSQL after every load).

- ``mart.persons`` + ``person_id`` on candidates: the same person across elections.
- Result views with the domain logic defined once (thresholds, percentages, turnout, rollups to
  country / voivodeship / powiat / gmina), so SQL authors and the LLM reuse it:
  ``v_sejm_*``, ``v_prezydent_results``, ``v_pe_national_results``, ``v_referendum_results``,
  ``v_sejmik_results``, ``v_turnout`` (all elections).
- Cross-election views: ``v_party_results`` (parties grouped into families across Sejm, European
  Parliament and sejmik elections), ``v_party_change`` (change against the previous election of the
  same kind) and ``v_gmina_continuity`` (which gmina TERYT codes exist in which election).
- ``mart.v_person_history``: every candidacy of a person.
- ``mart.entity_lexicon``: names used by entity resolution (``entities.py``).
"""

from __future__ import annotations

import logging

import psycopg

log = logging.getLogger("mart")

# Person matching. National candidacies (Sejm, Senate, President, European Parliament) with the
# same normalized full name and gender are one person, unless the name is shared by several
# candidates within one election event (Sejm+Senate of one year, one presidential round, one EP
# election); then residence must match too. A missing gender (2015 presidential files) is taken
# from other candidacies of the same name when they agree. This is a deliberate, explainable
# heuristic: PKW files carry no personal identifier, so namesakes in different years can be merged
# and changed surnames are not linked.
PERSONS_SQL = r"""
CREATE TEMP TABLE candidacy_raw ON COMMIT DROP AS
SELECT 'sejm' AS chamber, year, 1 AS round, candidate_id, full_name, last_name, first_names, gender, residence
FROM mart.sejm_candidates
UNION ALL
SELECT 'senat', year, 1, candidate_id, full_name, last_name, first_names, gender, residence FROM mart.senat_candidates
UNION ALL
SELECT 'prezydent', year, round, candidate_id, full_name, last_name, first_names, gender, residence FROM mart.prezydent_candidates
UNION ALL
SELECT 'pe', year, 1, candidate_id, full_name, last_name, first_names, gender, residence FROM mart.pe_candidates;

CREATE TEMP TABLE candidacy ON COMMIT DROP AS
WITH keyed AS (
  SELECT c.*, upper(regexp_replace(full_name, '\s+', ' ', 'g')) AS name_key,
         CASE WHEN chamber IN ('sejm', 'senat') THEN 'parliament' ELSE chamber END || ' ' || year || '/' || round AS event
  FROM candidacy_raw c
), name_gender AS (
  SELECT name_key, min(gender) AS gender FROM keyed WHERE gender IS NOT NULL
  GROUP BY name_key HAVING count(DISTINCT gender) = 1
)
SELECT k.chamber, k.year, k.round, k.candidate_id, k.full_name, k.last_name, k.first_names,
       coalesce(k.gender, g.gender) AS gender, k.residence, k.name_key, k.event
FROM keyed k LEFT JOIN name_gender g USING (name_key);

CREATE TEMP TABLE shared_name ON COMMIT DROP AS
SELECT DISTINCT name_key, gender FROM candidacy
GROUP BY name_key, gender, event HAVING count(DISTINCT (chamber, candidate_id)) > 1;

CREATE TEMP TABLE candidacy_person ON COMMIT DROP AS
SELECT c.*,
       CASE WHEN s.name_key IS NULL THEN 'name+gender' ELSE 'name+gender+residence' END AS match_rule,
       c.name_key || '|' || coalesce(c.gender, '') ||
         CASE WHEN s.name_key IS NULL THEN '' ELSE '|' || upper(coalesce(c.residence, '')) END AS person_key
FROM candidacy c
LEFT JOIN shared_name s ON s.name_key = c.name_key AND s.gender IS NOT DISTINCT FROM c.gender;

CREATE TEMP TABLE person_ids ON COMMIT DROP AS
SELECT person_key, CAST(dense_rank() OVER (ORDER BY person_key) AS INT) AS person_id
FROM (SELECT DISTINCT person_key FROM candidacy_person) k;

CREATE TEMP TABLE candidacy_ids ON COMMIT DROP AS
SELECT c.chamber, c.year, c.round, c.candidate_id, c.name_key, c.gender, i.person_id
FROM candidacy_person c JOIN person_ids i USING (person_key);

TRUNCATE mart.persons;
INSERT INTO mart.persons (person_id, full_name, last_name, first_names, gender, match_rule, candidacies)
SELECT i.person_id,
       (array_agg(c.full_name ORDER BY c.year DESC))[1],
       (array_agg(c.last_name ORDER BY c.year DESC))[1],
       (array_agg(c.first_names ORDER BY c.year DESC))[1],
       (array_agg(c.gender ORDER BY c.year DESC))[1],
       min(c.match_rule),
       count(*)
FROM candidacy_person c JOIN person_ids i USING (person_key)
GROUP BY i.person_id;

UPDATE mart.sejm_candidates k SET person_id = c.person_id FROM candidacy_ids c
WHERE c.chamber = 'sejm' AND c.year = k.year AND c.candidate_id = k.candidate_id;
UPDATE mart.senat_candidates k SET person_id = c.person_id FROM candidacy_ids c
WHERE c.chamber = 'senat' AND c.year = k.year AND c.candidate_id = k.candidate_id;
UPDATE mart.prezydent_candidates k SET person_id = c.person_id FROM candidacy_ids c
WHERE c.chamber = 'prezydent' AND c.year = k.year AND c.round = k.round AND c.candidate_id = k.candidate_id;
UPDATE mart.pe_candidates k SET person_id = c.person_id FROM candidacy_ids c
WHERE c.chamber = 'pe' AND c.year = k.year AND c.candidate_id = k.candidate_id;
"""

# Local candidates (sejmik, mayors) are linked to a national person only when the link is
# unambiguous: exactly one national person has that full name and gender, and the name occurs once
# in that local election (one sejmik candidate, or mayor candidates of one gmina). Local
# candidacies never create persons: tens of thousands of local namesakes would merge strangers.
LOCAL_PERSONS_SQL = r"""
CREATE TEMP TABLE national_name ON COMMIT DROP AS
SELECT name_key, gender, min(person_id) AS person_id FROM candidacy_ids
GROUP BY name_key, gender HAVING count(DISTINCT person_id) = 1;

UPDATE mart.sejmik_candidates SET person_id = NULL;
UPDATE mart.sejmik_candidates k SET person_id = n.person_id
FROM (SELECT year, full_name FROM mart.sejmik_candidates GROUP BY year, full_name HAVING count(*) = 1) u, national_name n
WHERE u.year = k.year AND u.full_name = k.full_name
  AND n.name_key = upper(regexp_replace(k.full_name, '\s+', ' ', 'g')) AND n.gender IS NOT DISTINCT FROM k.gender;

UPDATE mart.wbp_candidates SET person_id = NULL;
UPDATE mart.wbp_candidates k SET person_id = n.person_id
FROM (SELECT year, full_name FROM mart.wbp_candidates GROUP BY year, full_name HAVING count(DISTINCT teryt_gmina) = 1) u,
     national_name n
WHERE u.year = k.year AND u.full_name = k.full_name
  AND n.name_key = upper(regexp_replace(k.full_name, '\s+', ' ', 'g')) AND n.gender IS NOT DISTINCT FROM k.gender;
"""

# Colloquial party families for comparing elections: the first alias of a committee
# (``COMMITTEE_ALIASES`` in build_common.py) mapped to a family. Committees without aliases (local
# committees) fall into 'other'. Families group predecessor and successor committees (PO -> KO);
# v_party_change marks such pairs as not directly comparable.
PARTY_FAMILIES = {
    "PiS": "PiS",
    "KO": "KO",
    "PO": "KO",
    "KE": "KE",
    "Lewica": "Lewica",
    "SLD": "Lewica",
    "ZL": "Lewica",
    "NL": "Lewica",
    "Lewica Razem": "Razem",
    "Razem": "Razem",
    "TD": "TD/PSL",
    "PSL": "TD/PSL",
    "Konfederacja": "Konfederacja",
}


def _party_family_sql(aliases: str) -> str:
    """SQL expression mapping a committee's aliases array to its party family."""
    cases = " ".join(f"WHEN '{alias}' THEN '{family}'" for alias, family in PARTY_FAMILIES.items())
    return f"coalesce(CASE ({aliases})[1] {cases} ELSE ({aliases})[1] END, 'other')"


LEVEL_CASE = """CASE WHEN GROUPING(teryt_gmina) = 0 THEN 'gmina'
            WHEN GROUPING(teryt_powiat) = 0 THEN 'powiat'
            WHEN GROUPING(wojewodztwo) = 0 THEN 'wojewodztwo'
            ELSE 'country' END"""


def _rollup(source: str, keys: str, measures: str) -> str:
    """Aggregate ``source`` to country, voivodeship, powiat and gmina rows (column ``level``).

    ``source`` must provide wojewodztwo, teryt_powiat, powiat, teryt_gmina and gmina besides
    ``keys``. Rows abroad and on ships (no TERYT) count only in the country row.
    """
    return f"""
SELECT {keys}, {LEVEL_CASE} AS level, wojewodztwo, teryt_powiat, powiat, teryt_gmina, gmina, {measures}
FROM ({source}) s
GROUP BY GROUPING SETS (
  ({keys}),
  ({keys}, wojewodztwo),
  ({keys}, wojewodztwo, teryt_powiat, powiat),
  ({keys}, wojewodztwo, teryt_powiat, powiat, teryt_gmina, gmina)
)
HAVING GROUPING(wojewodztwo) = 1 OR wojewodztwo IS NOT NULL"""


UNIT_WINDOW = "PARTITION BY {keys}, a.level, a.wojewodztwo, a.teryt_powiat, a.teryt_gmina"
# Geography of a precinct/gmina row; abroad, ships and the pseudo codes some years use for ships
# (powiat part 98/99, e.g. 229801 'Statki') get NULL so they count only in the country row.
_DOMESTIC = "p.teryt_gmina IS NOT NULL AND substr(p.teryt_gmina, 3, 2) NOT IN ('98', '99')"
GEO = ", ".join(
    f"CASE WHEN {_DOMESTIC} THEN {expr} END AS {name}"
    for expr, name in (
        ("p.wojewodztwo", "wojewodztwo"),
        ("left(p.teryt_gmina, 4)", "teryt_powiat"),
        ("p.powiat", "powiat"),
        ("p.teryt_gmina", "teryt_gmina"),
        ("p.gmina", "gmina"),
    )
)
LEVEL_COMMENT = "Aggregation level of the row: country, wojewodztwo, powiat or gmina. Abroad and ships count only in country."

DROP_SQL = r"""
DROP VIEW IF EXISTS mart.v_person_history;
DROP MATERIALIZED VIEW IF EXISTS mart.v_gmina_continuity;
DROP MATERIALIZED VIEW IF EXISTS mart.v_party_change;
DROP MATERIALIZED VIEW IF EXISTS mart.v_party_results;
DROP MATERIALIZED VIEW IF EXISTS mart.v_turnout;
DROP MATERIALIZED VIEW IF EXISTS mart.v_sejmik_results;
DROP MATERIALIZED VIEW IF EXISTS mart.v_referendum_results;
DROP MATERIALIZED VIEW IF EXISTS mart.v_pe_national_results;
DROP MATERIALIZED VIEW IF EXISTS mart.v_prezydent_results;
DROP MATERIALIZED VIEW IF EXISTS mart.v_sejm_national_results;
DROP MATERIALIZED VIEW IF EXISTS mart.v_sejm_list_results;
DROP MATERIALIZED VIEW IF EXISTS mart.v_sejm_turnout;
"""

SEJM_VIEWS_SQL = r"""
CREATE MATERIALIZED VIEW mart.v_sejm_national_results AS
WITH totals AS (
  SELECT year, committee_id, sum(votes) AS votes, sum(seats) AS seats
  FROM mart.sejm_district_lists GROUP BY year, committee_id
), all_votes AS (
  SELECT year, sum(votes) AS votes FROM mart.sejm_district_lists GROUP BY year
)
SELECT t.year, c.committee_id, c.committee_name, c.aliases, c.is_coalition, c.is_minority,
       t.votes,
       round(100.0 * t.votes / a.votes, 2) AS pct,
       CASE WHEN c.is_minority THEN NULL WHEN c.is_coalition THEN 8 ELSE 5 END AS threshold_pct,
       c.is_minority OR 100.0 * t.votes / a.votes >= CASE WHEN c.is_coalition THEN 8 ELSE 5 END AS passed_threshold,
       CAST(t.seats AS INT) AS seats
FROM totals t
JOIN all_votes a USING (year)
JOIN mart.sejm_committees c USING (year, committee_id);
COMMENT ON MATERIALIZED VIEW mart.v_sejm_national_results IS 'Nationwide Sejm result per committee and year: votes, percent of valid votes, threshold (5% committee, 8% coalition, none for national minorities), whether it was passed, and seats won. Use for "wynik PiS", "kto przekroczył próg", "ile mandatów".';
COMMENT ON COLUMN mart.v_sejm_national_results.pct IS 'Percent of all valid votes in the country (0-100).';

CREATE MATERIALIZED VIEW mart.v_sejm_list_results AS
WITH unit_votes AS (
  SELECT p.year, p.wojewodztwo, p.powiat, left(p.teryt_gmina, 4) AS teryt_powiat, p.gmina, p.teryt_gmina,
         p.sejm_district_no, v.committee_id, v.votes
  FROM mart.sejm_precinct_list_votes v JOIN mart.precincts p USING (year, precinct_id)
), agg AS (
  SELECT year,
         CASE WHEN GROUPING(sejm_district_no) = 0 THEN 'sejm_district'
              WHEN GROUPING(teryt_gmina) = 0 THEN 'gmina'
              WHEN GROUPING(teryt_powiat) = 0 THEN 'powiat'
              WHEN GROUPING(wojewodztwo) = 0 THEN 'wojewodztwo'
              ELSE 'country' END AS level,
         wojewodztwo, powiat, teryt_powiat, gmina, teryt_gmina, sejm_district_no, committee_id,
         sum(votes) AS votes
  FROM unit_votes
  GROUP BY GROUPING SETS (
    (year, committee_id),
    (year, committee_id, wojewodztwo),
    (year, committee_id, wojewodztwo, teryt_powiat, powiat),
    (year, committee_id, wojewodztwo, teryt_powiat, powiat, teryt_gmina, gmina),
    (year, committee_id, sejm_district_no)
  )
)
SELECT a.year, a.level, a.wojewodztwo, a.powiat, a.teryt_powiat, a.gmina, a.teryt_gmina, a.sejm_district_no,
       a.committee_id, c.committee_name, c.aliases, a.votes,
       sum(a.votes) OVER w AS unit_valid_votes,
       round(100.0 * a.votes / NULLIF(sum(a.votes) OVER w, 0), 2) AS pct
FROM agg a JOIN mart.sejm_committees c USING (year, committee_id)
WINDOW w AS (PARTITION BY a.year, a.level, a.wojewodztwo, a.teryt_powiat, a.teryt_gmina, a.sejm_district_no);
COMMENT ON MATERIALIZED VIEW mart.v_sejm_list_results IS 'Sejm list votes and support in percent per territorial unit. Filter by level: ''country'', ''wojewodztwo'', ''powiat'', ''gmina'' or ''sejm_district''. Precincts abroad and on ships have NULL wojewodztwo/powiat/gmina (they count in country and district 19). Use for "w której gminie PiS miał najwyższe poparcie".';
COMMENT ON COLUMN mart.v_sejm_list_results.level IS 'Aggregation level of the row: country, wojewodztwo, powiat, gmina, sejm_district.';
COMMENT ON COLUMN mart.v_sejm_list_results.teryt_powiat IS '4-digit powiat TERYT (powiat names are not unique; prefer this or wojewodztwo+powiat).';
COMMENT ON COLUMN mart.v_sejm_list_results.unit_valid_votes IS 'Valid votes for all lists in the unit.';
COMMENT ON COLUMN mart.v_sejm_list_results.pct IS 'votes / unit_valid_votes in percent (0-100).';
CREATE INDEX ON mart.v_sejm_list_results (year, level, committee_id);
CREATE INDEX ON mart.v_sejm_list_results (teryt_gmina);

CREATE MATERIALIZED VIEW mart.v_sejm_turnout AS
SELECT p.year,
       CASE WHEN GROUPING(p.sejm_district_no) = 0 THEN 'sejm_district'
            WHEN GROUPING(p.teryt_gmina) = 0 THEN 'gmina'
            WHEN GROUPING(left(p.teryt_gmina, 4)) = 0 THEN 'powiat'
            WHEN GROUPING(p.wojewodztwo) = 0 THEN 'wojewodztwo'
            ELSE 'country' END AS level,
       p.wojewodztwo, p.powiat, left(p.teryt_gmina, 4) AS teryt_powiat, p.gmina, p.teryt_gmina, p.sejm_district_no,
       count(*) AS precincts,
       sum(r.eligible_voters) AS eligible_voters,
       sum(r.valid_ballots) AS valid_ballots,
       round(100.0 * sum(r.valid_ballots) / NULLIF(sum(r.eligible_voters), 0), 2) AS turnout_pct
FROM mart.precincts p JOIN mart.sejm_precinct_protocols r USING (year, precinct_id)
GROUP BY GROUPING SETS (
  (p.year),
  (p.year, p.wojewodztwo),
  (p.year, p.wojewodztwo, left(p.teryt_gmina, 4), p.powiat),
  (p.year, p.wojewodztwo, left(p.teryt_gmina, 4), p.powiat, p.teryt_gmina, p.gmina),
  (p.year, p.sejm_district_no)
);
COMMENT ON MATERIALIZED VIEW mart.v_sejm_turnout IS 'Official Sejm turnout (valid ballots / eligible voters) per territorial unit; level as in v_sejm_list_results. For single precincts use mart.sejm_precinct_protocols. For other elections use mart.v_turnout.';
COMMENT ON COLUMN mart.v_sejm_turnout.turnout_pct IS 'Turnout in percent (0-100).';
"""


def _results_views_sql() -> str:
    """DDL of the per-election result views (president, EP, referendum, sejmik)."""
    prez_source = f"""
        SELECT p.year, p.round, {GEO}, v.candidate_id, v.votes
        FROM mart.prezydent_precinct_candidate_votes v
        JOIN mart.prezydent_precinct_protocols p USING (year, round, precinct_id)"""
    ref_source = f"""
        SELECT p.year, a.question_no, {GEO}, p.eligible_voters, p.valid_ballots,
               a.valid_votes, a.invalid_votes, a.yes_votes, a.no_votes
        FROM mart.referendum_precinct_answers a JOIN mart.referendum_precinct_protocols p USING (year, precinct_id)"""
    sejmik_source = f"""
        SELECT p.year, {GEO}, v.committee_id, v.votes
        FROM mart.sejmik_gmina_list_votes v JOIN mart.sejmik_gmina_protocols p USING (year, teryt_gmina)"""
    return f"""
CREATE MATERIALIZED VIEW mart.v_prezydent_results AS
WITH agg AS ({_rollup(prez_source, "year, round, candidate_id", "sum(votes) AS votes")})
SELECT a.year, a.round, a.level, a.wojewodztwo, a.teryt_powiat, a.powiat, a.teryt_gmina, a.gmina,
       a.candidate_id, c.full_name, c.last_name, c.person_id, a.votes,
       sum(a.votes) OVER w AS unit_valid_votes,
       round(100.0 * a.votes / NULLIF(sum(a.votes) OVER w, 0), 2) AS pct
FROM agg a JOIN mart.prezydent_candidates c USING (year, round, candidate_id)
WINDOW w AS ({UNIT_WINDOW.format(keys="a.year, a.round")});
COMMENT ON MATERIALIZED VIEW mart.v_prezydent_results IS 'Presidential votes and percent per candidate, round and territorial unit, summed from precincts. Official national totals (including a few unpublished military precincts) are in mart.prezydent_candidates; use this view for gminas, powiats and voivodeships ("gdzie Duda miał najwyższe poparcie").';
COMMENT ON COLUMN mart.v_prezydent_results.level IS '{LEVEL_COMMENT}';
COMMENT ON COLUMN mart.v_prezydent_results.teryt_powiat IS '4-digit powiat TERYT.';
COMMENT ON COLUMN mart.v_prezydent_results.pct IS 'votes / unit_valid_votes in percent (0-100).';
CREATE INDEX ON mart.v_prezydent_results (year, round, level);
CREATE INDEX ON mart.v_prezydent_results (teryt_gmina);

CREATE MATERIALIZED VIEW mart.v_pe_national_results AS
WITH totals AS (
  SELECT year, committee_id, sum(votes) AS votes, sum(seats) AS seats, bool_or(in_seat_allocation) AS in_seat_allocation
  FROM mart.pe_district_lists GROUP BY year, committee_id
), all_votes AS (SELECT year, sum(votes) AS votes FROM mart.pe_district_lists GROUP BY year)
SELECT t.year, c.committee_id, c.committee_name, c.aliases, c.is_coalition, t.votes,
       round(100.0 * t.votes / a.votes, 2) AS pct, 5 AS threshold_pct, t.in_seat_allocation AS passed_threshold,
       CAST(t.seats AS INT) AS seats
FROM totals t JOIN all_votes a USING (year) JOIN mart.pe_committees c USING (year, committee_id);
COMMENT ON MATERIALIZED VIEW mart.v_pe_national_results IS 'Nationwide European Parliament result per committee: votes, percent, 5% threshold (the same for coalitions) and seats (MEPs). Territorial results: mart.v_party_results with election = ''pe''.';
COMMENT ON COLUMN mart.v_pe_national_results.pct IS 'Percent of all valid votes in the country (0-100).';

CREATE MATERIALIZED VIEW mart.v_referendum_results AS
WITH agg AS ({_rollup(ref_source, "year, question_no", '''sum(eligible_voters) AS eligible_voters,
       sum(valid_ballots) AS valid_ballots, sum(valid_votes) AS valid_votes, sum(invalid_votes) AS invalid_votes,
       sum(yes_votes) AS yes_votes, sum(no_votes) AS no_votes''')})
SELECT a.year, a.question_no, q.topic, a.level, a.wojewodztwo, a.teryt_powiat, a.powiat, a.teryt_gmina, a.gmina,
       a.eligible_voters, a.valid_ballots,
       round(100.0 * a.valid_ballots / NULLIF(a.eligible_voters, 0), 2) AS turnout_pct,
       a.valid_votes, a.invalid_votes, a.yes_votes, a.no_votes,
       round(100.0 * a.yes_votes / NULLIF(a.valid_votes, 0), 2) AS yes_pct,
       round(100.0 * a.no_votes / NULLIF(a.valid_votes, 0), 2) AS no_pct
FROM agg a JOIN mart.referendum_questions q USING (year, question_no);
COMMENT ON MATERIALIZED VIEW mart.v_referendum_results IS 'Referendum answers per question and territorial unit: turnout, yes/no votes and their percent of valid votes. Binding only when national turnout is above 50%.';
COMMENT ON COLUMN mart.v_referendum_results.level IS '{LEVEL_COMMENT}';
COMMENT ON COLUMN mart.v_referendum_results.turnout_pct IS 'valid_ballots / eligible_voters in percent (0-100).';
COMMENT ON COLUMN mart.v_referendum_results.yes_pct IS 'yes_votes / valid_votes in percent (0-100).';

CREATE MATERIALIZED VIEW mart.v_sejmik_results AS
WITH agg AS ({_rollup(sejmik_source, "year, committee_id", "sum(votes) AS votes")}),
seats AS (
  SELECT year, committee_id, CASE WHEN GROUPING(wojewodztwo) = 0 THEN 'wojewodztwo' ELSE 'country' END AS level,
         wojewodztwo, sum(seats) AS seats
  FROM mart.sejmik_district_lists
  GROUP BY GROUPING SETS ((year, committee_id), (year, committee_id, wojewodztwo))
)
SELECT a.year, a.level, a.wojewodztwo, a.teryt_powiat, a.powiat, a.teryt_gmina, a.gmina,
       a.committee_id, c.committee_name, c.aliases, a.votes,
       sum(a.votes) OVER w AS unit_valid_votes,
       round(100.0 * a.votes / NULLIF(sum(a.votes) OVER w, 0), 2) AS pct,
       CAST(s.seats AS INT) AS seats
FROM agg a
JOIN mart.sejmik_committees c USING (year, committee_id)
LEFT JOIN seats s ON s.year = a.year AND s.committee_id = a.committee_id AND s.level = a.level
                 AND s.wojewodztwo IS NOT DISTINCT FROM a.wojewodztwo
WINDOW w AS ({UNIT_WINDOW.format(keys="a.year")});
COMMENT ON MATERIALIZED VIEW mart.v_sejmik_results IS 'Voivodeship assembly (sejmik) results per committee and territorial unit: votes, percent and, at country and wojewodztwo level, seats (radni sejmiku).';
COMMENT ON COLUMN mart.v_sejmik_results.level IS '{LEVEL_COMMENT}';
COMMENT ON COLUMN mart.v_sejmik_results.seats IS 'Seats won; only at level country and wojewodztwo (NULL below).';
CREATE INDEX ON mart.v_sejmik_results (year, level);
"""


def _cross_election_views_sql() -> str:
    """DDL of v_turnout, v_party_results, v_party_change and v_gmina_continuity."""
    turnout_source = f"""
        SELECT 'sejm' AS election, p.year, 1 AS round, {GEO}, r.eligible_voters, r.valid_ballots
        FROM mart.precincts p JOIN mart.sejm_precinct_protocols r USING (year, precinct_id)
        UNION ALL
        SELECT 'senat', p.year, 1, {GEO}, r.eligible_voters, r.valid_ballots
        FROM mart.precincts p JOIN mart.senat_precinct_protocols r USING (year, precinct_id)
        UNION ALL
        SELECT 'prezydent', p.year, p.round, {GEO}, p.eligible_voters, p.valid_ballots FROM mart.prezydent_precinct_protocols p
        UNION ALL
        SELECT 'pe', p.year, 1, {GEO}, p.eligible_voters, p.valid_ballots FROM mart.pe_precinct_protocols p
        UNION ALL
        SELECT 'referendum', p.year, 1, {GEO}, p.eligible_voters, p.valid_ballots FROM mart.referendum_precinct_protocols p
        UNION ALL
        SELECT 'sejmik', p.year, 1, {GEO}, p.eligible_voters, p.valid_ballots FROM mart.sejmik_gmina_protocols p
        UNION ALL
        SELECT 'wbp', p.year, p.round, {GEO}, p.eligible_voters, p.valid_ballots FROM mart.wbp_gmina_protocols p"""
    party = _party_family_sql("c.aliases")
    party_source = f"""
        SELECT 'sejm' AS election, p.year, {GEO}, {party} AS party, c.aliases[1] AS lead_alias, c.committee_name, v.votes
        FROM mart.sejm_precinct_list_votes v JOIN mart.precincts p USING (year, precinct_id)
        JOIN mart.sejm_committees c USING (year, committee_id)
        UNION ALL
        SELECT 'pe', p.year, {GEO}, {party}, c.aliases[1], c.committee_name, v.votes
        FROM mart.pe_precinct_list_votes v JOIN mart.pe_precinct_protocols p USING (year, precinct_id)
        JOIN mart.pe_committees c USING (year, committee_id)
        UNION ALL
        SELECT 'sejmik', p.year, {GEO}, {party}, c.aliases[1], c.committee_name, v.votes
        FROM mart.sejmik_gmina_list_votes v JOIN mart.sejmik_gmina_protocols p USING (year, teryt_gmina)
        JOIN mart.sejmik_committees c USING (year, committee_id)"""
    party_measures = """sum(votes) AS votes,
       array_to_string(array_agg(DISTINCT lead_alias ORDER BY lead_alias), '+') AS lead_aliases,
       count(DISTINCT committee_name) AS committees"""
    families = ", ".join(f"{alias}→{family}" for alias, family in PARTY_FAMILIES.items())
    return f"""
CREATE MATERIALIZED VIEW mart.v_turnout AS
SELECT election, year, round, level, wojewodztwo, teryt_powiat, powiat, teryt_gmina, gmina,
       eligible_voters, valid_ballots, round(100.0 * valid_ballots / NULLIF(eligible_voters, 0), 2) AS turnout_pct
FROM ({_rollup(turnout_source, "election, year, round", "sum(eligible_voters) AS eligible_voters, sum(valid_ballots) AS valid_ballots")}) t;
COMMENT ON MATERIALIZED VIEW mart.v_turnout IS 'Turnout (valid ballots / eligible voters) of every loaded election per territorial unit: election in (sejm, senat, prezydent, pe, referendum, sejmik, wbp), year, round. Compare turnout across elections by joining on level + teryt_gmina / teryt_powiat / wojewodztwo. Mayor (wbp) round 2 covers only gminas with a run-off, so its country row is not a national turnout.';
COMMENT ON COLUMN mart.v_turnout.level IS '{LEVEL_COMMENT}';
COMMENT ON COLUMN mart.v_turnout.turnout_pct IS 'Turnout in percent (0-100).';
CREATE INDEX ON mart.v_turnout (election, year, round, level);
CREATE INDEX ON mart.v_turnout (teryt_gmina);

CREATE MATERIALIZED VIEW mart.v_party_results AS
WITH agg AS ({_rollup(party_source, "election, year, party", party_measures)})
SELECT a.election, a.year, a.level, a.wojewodztwo, a.teryt_powiat, a.powiat, a.teryt_gmina, a.gmina,
       CASE a.level WHEN 'gmina' THEN a.teryt_gmina WHEN 'powiat' THEN a.teryt_powiat
                    WHEN 'wojewodztwo' THEN a.wojewodztwo ELSE 'country' END AS unit_key,
       a.party, a.lead_aliases, a.committees, a.votes,
       sum(a.votes) OVER w AS unit_valid_votes,
       round(100.0 * a.votes / NULLIF(sum(a.votes) OVER w, 0), 2) AS pct
FROM agg a
WINDOW w AS ({UNIT_WINDOW.format(keys="a.election, a.year")});
COMMENT ON MATERIALIZED VIEW mart.v_party_results IS 'Party support across Sejm, European Parliament (pe) and voivodeship assembly (sejmik) elections per territorial unit, with committees grouped into party families so elections can be compared ("jak zmieniło się poparcie PiS w gminie X między 2019 a 2023", "PiS w sejmiku vs w Sejmie"). Families ({families}; otherwise the first alias; committees without aliases = ''other''). For the change against the previous election of the same kind use mart.v_party_change.';
COMMENT ON COLUMN mart.v_party_results.election IS 'sejm, pe or sejmik.';
COMMENT ON COLUMN mart.v_party_results.level IS '{LEVEL_COMMENT}';
COMMENT ON COLUMN mart.v_party_results.unit_key IS 'teryt_gmina, teryt_powiat, wojewodztwo or ''country'' depending on level; join key across elections.';
COMMENT ON COLUMN mart.v_party_results.party IS 'Party family, e.g. PiS, KO, Lewica, TD/PSL, Konfederacja, Razem, KE, other.';
COMMENT ON COLUMN mart.v_party_results.lead_aliases IS 'First aliases of the committees in the family (e.g. ''PO'' in 2015, ''KO'' later); differing values mean a different committee or coalition.';
COMMENT ON COLUMN mart.v_party_results.pct IS 'votes / unit_valid_votes in percent (0-100).';
CREATE INDEX ON mart.v_party_results (election, year, level, party);
CREATE INDEX ON mart.v_party_results (unit_key);

CREATE MATERIALIZED VIEW mart.v_party_change AS
WITH kinds AS (SELECT DISTINCT election, year FROM mart.v_party_results),
prev AS (
  SELECT k.election, k.year, (SELECT max(o.year) FROM kinds o WHERE o.election = k.election AND o.year < k.year) AS prev_year
  FROM kinds k
)
SELECT r.election, r.year, p.prev_year, r.level, r.wojewodztwo, r.teryt_powiat, r.powiat, r.teryt_gmina, r.gmina,
       r.unit_key, r.party, r.votes, o.votes AS prev_votes, r.pct, o.pct AS prev_pct,
       r.pct - o.pct AS change_pp,
       r.lead_aliases = o.lead_aliases AS comparable,
       r.lead_aliases, o.lead_aliases AS prev_lead_aliases
FROM mart.v_party_results r
JOIN prev p USING (election, year)
JOIN mart.v_party_results o ON o.election = r.election AND o.year = p.prev_year AND o.level = r.level
                           AND o.unit_key = r.unit_key AND o.party = r.party;
COMMENT ON MATERIALIZED VIEW mart.v_party_change IS 'Change of party-family support against the previous loaded election of the same kind (sejm 2023 vs 2019, pe 2024 vs 2019, sejmik 2024 vs 2018) in the same territorial unit (same TERYT; units missing in either election are absent, see mart.v_gmina_continuity). Use for "gdzie PiS stracił najwięcej".';
COMMENT ON COLUMN mart.v_party_change.change_pp IS 'pct - prev_pct in percentage points.';
COMMENT ON COLUMN mart.v_party_change.comparable IS 'True when the family ran as the same committee both times (same lead aliases); false e.g. for PO 2015 -> KO 2019 or PSL 2019 -> TD 2023. Say so when reporting a change that is not comparable.';
CREATE INDEX ON mart.v_party_change (election, year, level, party);

CREATE MATERIALIZED VIEW mart.v_gmina_continuity AS
WITH presence AS (
  SELECT DISTINCT teryt_gmina, gmina, powiat, wojewodztwo, election, year
  FROM mart.v_turnout WHERE level = 'gmina' AND round = 1 AND election <> 'wbp'
), all_elections AS (SELECT count(DISTINCT (election, year)) AS n FROM presence)
SELECT teryt_gmina,
       (array_agg(gmina ORDER BY year DESC))[1] AS gmina,
       (array_agg(powiat ORDER BY year DESC))[1] AS powiat,
       (array_agg(wojewodztwo ORDER BY year DESC))[1] AS wojewodztwo,
       array_agg(DISTINCT gmina) AS names,
       array_agg(DISTINCT election || ' ' || year) AS elections,
       min(year) AS first_year, max(year) AS last_year,
       count(DISTINCT (election, year)) = (SELECT n FROM all_elections) AS in_all_elections
FROM presence GROUP BY teryt_gmina;
COMMENT ON MATERIALIZED VIEW mart.v_gmina_continuity IS 'Gmina TERYT codes and the loaded elections they appear in, except mayor (wbp) elections (Warsaw is split into its 18 districts, 1465xx; its mayor is elected city-wide as 146501). in_all_elections = false marks gminas created, merged or recoded in the period; comparisons across elections by TERYT do not cover them. names lists the spellings used.';
COMMENT ON COLUMN mart.v_gmina_continuity.elections IS 'Elections with results for this TERYT, e.g. {{"sejm 2015","sejmik 2018"}}.';
"""


PERSON_HISTORY_SQL = r"""
CREATE VIEW mart.v_person_history AS
SELECT p.person_id, p.full_name, p.gender, p.match_rule, x.year, x.chamber, x.round, x.district_no, x.area,
       x.committee_name, x.committee_aliases, x.list_no, x.list_position, x.votes, x.elected
FROM mart.persons p
JOIN (
  SELECT k.person_id, k.year, 'sejm' AS chamber, 1 AS round, k.district_no, NULL::text AS area, c.committee_name,
         c.aliases AS committee_aliases, k.list_no, k.list_position, k.votes::bigint AS votes, k.elected
  FROM mart.sejm_candidates k JOIN mart.sejm_committees c USING (year, committee_id)
  UNION ALL
  SELECT s.person_id, s.year, 'senat', 1, s.district_no, NULL, s.committee_name,
         (SELECT c.aliases FROM mart.sejm_committees c WHERE c.year = s.year AND c.committee_name = s.committee_name),
         NULL, NULL, s.votes, s.elected
  FROM mart.senat_candidates s
  UNION ALL
  SELECT k.person_id, k.year, 'prezydent', k.round, NULL, NULL, k.committee_name, NULL, NULL, NULL, k.votes, k.elected
  FROM mart.prezydent_candidates k
  UNION ALL
  SELECT k.person_id, k.year, 'pe', 1, k.district_no, NULL, c.committee_name, c.aliases, k.list_no, k.list_position,
         k.votes, k.elected
  FROM mart.pe_candidates k JOIN mart.pe_committees c USING (year, committee_id)
  UNION ALL
  SELECT k.person_id, k.year, 'sejmik', 1, k.district_no, k.wojewodztwo, c.committee_name, c.aliases, k.list_no,
         k.list_position, k.votes, k.elected
  FROM mart.sejmik_candidates k JOIN mart.sejmik_committees c USING (year, committee_id)
  WHERE k.person_id IS NOT NULL
  UNION ALL
  SELECT k.person_id, k.year, 'wbp', k.round, NULL, g.gmina, k.committee_name,
         (SELECT c.aliases FROM mart.sejmik_committees c WHERE c.year = k.year AND c.committee_name = k.committee_name),
         NULL, NULL, k.votes, k.elected
  FROM mart.wbp_candidates k
  LEFT JOIN mart.wbp_gmina_protocols g USING (year, round, teryt_gmina)
  WHERE k.person_id IS NOT NULL
) x USING (person_id);
COMMENT ON VIEW mart.v_person_history IS 'Every candidacy of a person (one row per candidacy and round): Sejm, Senate, President, European Parliament, and sejmik/mayor candidacies linked to the same person. Use for "w których wyborach startował X", "kto zmienił komitet", "kto przeszedł z Sejmu do Senatu".';
COMMENT ON COLUMN mart.v_person_history.chamber IS 'Election: sejm, senat, prezydent, pe, sejmik or wbp (mayor).';
COMMENT ON COLUMN mart.v_person_history.area IS 'sejmik: voivodeship; wbp: gmina. NULL otherwise.';
COMMENT ON COLUMN mart.v_person_history.committee_aliases IS 'Aliases of the committee (Senate and mayor rows: only when a Sejm / sejmik committee of that year has the same name). Compare aliases, not committee_name, to find party switches across years.';

UPDATE mart.persons p SET candidacies = h.n
FROM (SELECT person_id, count(*) AS n FROM mart.v_person_history GROUP BY person_id) h
WHERE h.person_id = p.person_id;
"""

LEXICON_SQL = r"""
TRUNCATE mart.entity_lexicon;
INSERT INTO mart.entity_lexicon (kind, name, folded, ref, detail, years)
SELECT 'gmina', gmina, mart.fold_name(gmina), teryt_gmina,
       'powiat ' || coalesce(min(powiat), '?') || ', ' || coalesce(min(wojewodztwo), '?'),
       array_agg(DISTINCT year ORDER BY year)
FROM mart.precincts WHERE teryt_gmina IS NOT NULL GROUP BY gmina, teryt_gmina
UNION ALL
SELECT 'powiat', powiat, mart.fold_name(powiat), left(teryt_gmina, 4), 'województwo ' || min(wojewodztwo),
       array_agg(DISTINCT year ORDER BY year)
FROM mart.precincts WHERE teryt_gmina IS NOT NULL AND powiat IS NOT NULL GROUP BY powiat, left(teryt_gmina, 4)
UNION ALL
SELECT 'wojewodztwo', wojewodztwo, mart.fold_name(wojewodztwo), wojewodztwo, 'TERYT ' || left(min(teryt_gmina), 2),
       array_agg(DISTINCT year ORDER BY year)
FROM mart.precincts WHERE wojewodztwo IS NOT NULL GROUP BY wojewodztwo
UNION ALL
SELECT 'sejm_district', seat_city, mart.fold_name(seat_city), district_no::text, 'Sejm district ' || district_no,
       array_agg(DISTINCT year ORDER BY year)
FROM mart.sejm_districts WHERE seat_city IS NOT NULL GROUP BY seat_city, district_no
UNION ALL
SELECT 'person', p.full_name, mart.fold_name(p.last_name), p.person_id::text,
       string_agg(DISTINCT h.chamber || ' ' || h.year || coalesce(' (' || NULLIF(concat_ws(', ',
           CASE WHEN h.chamber IN ('prezydent', 'wbp') THEN 'round ' || h.round END,
           CASE WHEN h.district_no IS NOT NULL THEN 'district ' || h.district_no END,
           h.area,
           CASE WHEN h.elected THEN 'elected' END), '') || ')', ''), '; '),
       array_agg(DISTINCT h.year ORDER BY h.year)
FROM mart.persons p JOIN mart.v_person_history h USING (person_id)
GROUP BY p.person_id, p.full_name, p.last_name
UNION ALL
SELECT 'committee', alias, mart.fold_name(alias), committee_name, string_agg(DISTINCT source, ', '),
       array_agg(DISTINCT year ORDER BY year)
FROM (
  SELECT 'sejm' AS source, year, committee_name, aliases FROM mart.sejm_committees
  UNION ALL SELECT 'pe', year, committee_name, aliases FROM mart.pe_committees
  UNION ALL SELECT 'sejmik', year, committee_name, aliases FROM mart.sejmik_committees
) c, unnest(c.aliases) AS alias
GROUP BY alias, committee_name;
"""

ANALYZED = (
    "persons",
    "entity_lexicon",
    "v_sejm_national_results",
    "v_sejm_list_results",
    "v_sejm_turnout",
    "v_prezydent_results",
    "v_pe_national_results",
    "v_referendum_results",
    "v_sejmik_results",
    "v_turnout",
    "v_party_results",
    "v_party_change",
    "v_gmina_continuity",
)


def rebuild(pg: psycopg.Connection) -> None:
    """Rebuild persons, views and the entity lexicon from everything loaded (caller's transaction)."""
    pg.execute(PERSONS_SQL)
    pg.execute(LOCAL_PERSONS_SQL)
    persons = pg.execute("SELECT count(*), count(*) FILTER (WHERE candidacies > 1) FROM mart.persons").fetchone()
    log.info("persons: %s (with more than one national candidacy: %s)", *persons)
    pg.execute(DROP_SQL)
    pg.execute(SEJM_VIEWS_SQL)
    pg.execute(_results_views_sql())
    pg.execute(_cross_election_views_sql())
    pg.execute(PERSON_HISTORY_SQL)
    linked = pg.execute(
        "SELECT (SELECT count(*) FROM mart.sejmik_candidates WHERE person_id IS NOT NULL),"
        " (SELECT count(*) FROM mart.wbp_candidates WHERE person_id IS NOT NULL)"
    ).fetchone()
    log.info("local candidacies linked to persons: sejmik %s, wbp %s", *linked)
    pg.execute(LEXICON_SQL)
    log.info("entity lexicon: %s names", pg.execute("SELECT count(*) FROM mart.entity_lexicon").fetchone()[0])
    for relation in ANALYZED:
        pg.execute(f"ANALYZE mart.{relation}")
