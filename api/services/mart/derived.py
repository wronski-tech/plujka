"""Relations derived from all loaded years (run in PostgreSQL after every load).

- ``mart.persons`` + ``person_id`` on candidates: the same person across years and chambers.
- ``mart.v_sejm_national_results``, ``mart.v_sejm_list_results``, ``mart.v_sejm_turnout``: domain
  logic (thresholds, percentages, turnout per unit) defined once so SQL authors and the LLM reuse it.
- ``mart.v_person_history``: every candidacy of a person.
- ``mart.entity_lexicon``: names used by entity resolution (``entities.py``).
"""

from __future__ import annotations

import logging

import psycopg

log = logging.getLogger("mart")

# Person matching. Candidacies with the same normalized full name and gender are one person, unless
# the name is shared by several candidates within one election (Sejm+Senate of one year); then
# residence must match too. This is a deliberate, explainable heuristic: PKW files carry no personal
# identifier, so namesakes in different years can be merged and changed surnames are not linked.
PERSONS_SQL = r"""
CREATE TEMP TABLE candidacy ON COMMIT DROP AS
SELECT 'sejm' AS chamber, year, candidate_id, full_name, last_name, first_names, gender, residence,
       upper(regexp_replace(full_name, '\s+', ' ', 'g')) AS name_key
FROM mart.sejm_candidates
UNION ALL
SELECT 'senat', year, candidate_id, full_name, last_name, first_names, gender, residence,
       upper(regexp_replace(full_name, '\s+', ' ', 'g'))
FROM mart.senat_candidates;

CREATE TEMP TABLE shared_name ON COMMIT DROP AS
SELECT DISTINCT name_key, gender FROM candidacy GROUP BY name_key, gender, year HAVING count(*) > 1;

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

UPDATE mart.sejm_candidates k SET person_id = i.person_id
FROM candidacy_person c JOIN person_ids i USING (person_key)
WHERE c.chamber = 'sejm' AND c.year = k.year AND c.candidate_id = k.candidate_id;

UPDATE mart.senat_candidates k SET person_id = i.person_id
FROM candidacy_person c JOIN person_ids i USING (person_key)
WHERE c.chamber = 'senat' AND c.year = k.year AND c.candidate_id = k.candidate_id;
"""

VIEWS_SQL = r"""
DROP VIEW IF EXISTS mart.v_person_history;
DROP MATERIALIZED VIEW IF EXISTS mart.v_sejm_national_results;
DROP MATERIALIZED VIEW IF EXISTS mart.v_sejm_list_results;
DROP MATERIALIZED VIEW IF EXISTS mart.v_sejm_turnout;

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
COMMENT ON MATERIALIZED VIEW mart.v_sejm_turnout IS 'Official Sejm turnout (valid ballots / eligible voters) per territorial unit; level as in v_sejm_list_results. For single precincts use mart.sejm_precinct_protocols.';
COMMENT ON COLUMN mart.v_sejm_turnout.turnout_pct IS 'Turnout in percent (0-100).';

CREATE VIEW mart.v_person_history AS
SELECT p.person_id, p.full_name, p.gender, p.match_rule, x.year, x.chamber, x.district_no,
       x.committee_name, x.committee_aliases, x.list_no, x.list_position, x.votes, x.elected
FROM mart.persons p
JOIN (
  SELECT k.person_id, k.year, 'sejm' AS chamber, k.district_no, c.committee_name, c.aliases AS committee_aliases,
         k.list_no, k.list_position, k.votes, k.elected
  FROM mart.sejm_candidates k JOIN mart.sejm_committees c USING (year, committee_id)
  UNION ALL
  SELECT s.person_id, s.year, 'senat', s.district_no, s.committee_name,
         (SELECT c.aliases FROM mart.sejm_committees c WHERE c.year = s.year AND c.committee_name = s.committee_name),
         NULL, NULL, s.votes, s.elected
  FROM mart.senat_candidates s
) x USING (person_id);
COMMENT ON VIEW mart.v_person_history IS 'Every Sejm/Senate candidacy of a person (one row per candidacy). Use for "w których wyborach startował X", "kto zmienił komitet", "kto przeszedł z Sejmu do Senatu".';
COMMENT ON COLUMN mart.v_person_history.chamber IS '''sejm'' or ''senat''.';
COMMENT ON COLUMN mart.v_person_history.committee_aliases IS 'Aliases of the committee (Senate rows: only when a Sejm committee of that year has the same name). Compare aliases, not committee_name, to find party switches across years.';
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
       string_agg(DISTINCT h.chamber || ' ' || h.year || ' (district ' || h.district_no || CASE WHEN h.elected THEN ', elected' ELSE '' END || ')', '; '),
       array_agg(DISTINCT h.year ORDER BY h.year)
FROM mart.persons p JOIN mart.v_person_history h USING (person_id)
GROUP BY p.person_id, p.full_name, p.last_name
UNION ALL
SELECT 'committee', alias, mart.fold_name(alias), committee_name, NULL, array_agg(DISTINCT year ORDER BY year)
FROM mart.sejm_committees, unnest(aliases) AS alias GROUP BY alias, committee_name;
"""


def rebuild(pg: psycopg.Connection) -> None:
    """Rebuild persons, views and the entity lexicon from all loaded years (caller's transaction)."""
    pg.execute(PERSONS_SQL)
    persons = pg.execute("SELECT count(*), count(*) FILTER (WHERE candidacies > 1) FROM mart.persons").fetchone()
    log.info("persons: %s (with more than one candidacy: %s)", *persons)
    pg.execute(VIEWS_SQL)
    pg.execute(LEXICON_SQL)
    log.info("entity lexicon: %s names", pg.execute("SELECT count(*) FROM mart.entity_lexicon").fetchone()[0])
    for relation in ("persons", "entity_lexicon", "v_sejm_national_results", "v_sejm_list_results", "v_sejm_turnout"):
        pg.execute(f"ANALYZE mart.{relation}")
