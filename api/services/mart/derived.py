"""Relations derived from everything loaded (rebuilt in PostgreSQL after every load).

- ``mart.persons`` + ``person_id`` on candidates: the same person across elections.
- Result views with the domain logic defined once (thresholds, percentages, turnout, rollups to
  country / voivodeship / powiat / gmina), so SQL authors and the LLM reuse it:
  ``v_sejm_*``, ``v_prezydent_results``, ``v_pe_national_results``, ``v_referendum_results``,
  ``v_sejmik_results``, ``v_rada_results`` (councils), ``v_turnout`` (all elections).
- Cross-election views: ``v_party_results`` (parties grouped into families across Sejm, European
  Parliament and sejmik elections), ``v_party_change`` (change against the previous election of the
  same kind) and ``v_gmina_continuity`` (which gmina TERYT codes exist in which election).
- ``mart.v_person_history``: every candidacy of a person.
- ``mart.v_gus_units``: GUS statistics per unit and year in wide form (not elections).
- ``mart.entity_lexicon``: names used by entity resolution (``entities.py``).
"""

from __future__ import annotations

import csv
import json
import logging
from pathlib import Path

import psycopg

log = logging.getLogger("mart")

# Person matching. National candidacies (Sejm, Senate, President, European Parliament) with the
# same normalized full name and gender are one person, unless the name is shared by several
# candidates within one election event (Sejm+Senate of one year, one presidential round, one EP
# election); then residence must match too (namesakes of one event with the same residence text are
# a person each). A missing gender (2015 presidential files) is taken from other candidacies of the
# same name when they agree. Three more rules, measured against the ages in the 1991-1997 Sejm and
# Senate registers (see docs/MART.md, "Persons"):
# - A name with a single given name is split by voivodeship of residence when its candidacies live in
#   different voivodeships (Warsaw and unmapped residences count for any) and no occupations of the
#   two sides are alike (trigram similarity >= 0.3, or a student), no residences have the same place
#   text and no birth years are at most 1 apart. Candidacies linked to neither side join the part of
#   the nearest candidacy in time. Names with a presidential candidacy are not split (no residence
#   before 2020; such cases need overrides). Two given names make namesakes rare: there different
#   voivodeships nearly always mean a move, so they are never split.
# - Files before 2011 often give only the first given name ("Donald TUSK" in 1991, later "Donald
#   Franciszek TUSK"). The candidacies before 2011 of a person with one given name join the one person
#   with more given names, the same surname, gender and first given name, when no candidacies of the
#   two share an election event or live in different voivodeships (as above), and one pair lives in
#   the same powiat or place, or has birth years at most 1 apart (Sejm and Senate registers 1991-1997
#   publish the age); birth years more than 3 apart contradict. Registers from 2011 give all given
#   names, so a later candidacy with one given name is a namesake: it must share an election event
#   with the fuller person (then it stays apart), otherwise nothing is joined.
# - Birth years more than 3 apart split a person unless the candidacies share a powiat or place (see
#   below).
# This is a deliberate, explainable heuristic: PKW files carry no personal identifier, so namesakes
# can still be merged and changed surnames are not linked. Manual corrections come from
# PERSON_OVERRIDES (see below).
PERSONS_SQL = r"""
CREATE TEMP TABLE candidacy_raw ON COMMIT DROP AS
SELECT 'sejm' AS chamber, year, 1 AS round, candidate_id, full_name, last_name, first_names, gender, residence,
       residence_teryt, occupation, age
FROM mart.sejm_candidates
UNION ALL
SELECT 'senat', year, 1, candidate_id, full_name, last_name, first_names, gender, residence, residence_teryt, occupation,
       age
FROM mart.senat_candidates
UNION ALL
SELECT 'prezydent', year, round, candidate_id, full_name, last_name, first_names, gender, residence, residence_teryt,
       occupation, NULL::int
FROM mart.prezydent_candidates
UNION ALL
SELECT 'pe', year, 1, candidate_id, full_name, last_name, first_names, gender, residence, residence_teryt, occupation,
       NULL::int
FROM mart.pe_candidates
UNION ALL
-- Senate by-elections publish names and committees only. round: the district, so that every by-election
-- is its own election event (two in one year, e.g. 2013 and 2014, are separate events).
SELECT 'senat_uzup', year, district_no, candidate_id, full_name, last_name, first_names, NULL, NULL, NULL, NULL, NULL
FROM mart.senat_byelections;

-- Place name (folded) -> powiat, learned from the residence text and residence_teryt pairs published
-- since 2019: the powiat of at least 90% of the pairs of that place name. (Learned from 2019-2020, 98%
-- of the 2023-2025 places that map get the right powiat; requiring a single powiat gives 97% and maps
-- fewer places. Gmina names as a fallback reach only 86%, so they are not used.)
CREATE TEMP TABLE residence_place ON COMMIT DROP AS
SELECT place, powiat FROM (
  SELECT place, powiat, n, sum(n) OVER (PARTITION BY place) AS total,
         row_number() OVER (PARTITION BY place ORDER BY n DESC, powiat) AS rn
  FROM (SELECT mart.fold_name(residence) AS place, left(residence_teryt, 4) AS powiat, count(*) AS n
        FROM candidacy_raw WHERE residence_teryt ~ '^[0-9]{6}$' AND residence IS NOT NULL
        GROUP BY 1, 2) p
) p
WHERE rn = 1 AND n >= 0.9 * total;

-- place: the residence text without the gmina, house number or "k/" part ("Kleszewo, gm. Pułtusk",
-- "Rogoźnik gm.Bobrowniki", "Grodzisko Dolne 640/7"), folded, compared only with other residences;
-- powiat: residence_teryt where published, else the mapped residence text. (Mapping the place instead
-- would put villages into same-name towns: only 59% of such places agree with their gmina's powiat.)
CREATE TEMP TABLE candidacy ON COMMIT DROP AS
WITH keyed AS (
  SELECT c.*, upper(regexp_replace(full_name, '\s+', ' ', 'g')) AS name_key,
         CASE WHEN chamber IN ('sejm', 'senat') THEN 'parliament' ELSE chamber END || ' ' || year || '/' || round AS event,
         nullif(trim(mart.fold_name(regexp_replace(split_part(residence, ',', 1),
                                                   '\s+(gm\.|gmina\s|gm\s|k\.|k/|\d).*$', '', 'i'))), '') AS place
  FROM candidacy_raw c
), name_gender AS (
  SELECT name_key, min(gender) AS gender FROM keyed WHERE gender IS NOT NULL
  GROUP BY name_key HAVING count(DISTINCT gender) = 1
)
SELECT k.chamber, k.year, k.round, k.candidate_id, k.full_name, k.last_name, k.first_names,
       coalesce(k.gender, g.gender) AS gender, k.residence, k.occupation, k.name_key, k.event, k.place,
       coalesce(left(k.residence_teryt, 4), p.powiat) AS powiat, k.year - k.age AS birth_year
FROM keyed k
LEFT JOIN name_gender g USING (name_key)
LEFT JOIN residence_place p ON k.residence_teryt IS NULL AND p.place = mart.fold_name(k.residence);

CREATE TEMP TABLE shared_name ON COMMIT DROP AS
SELECT DISTINCT name_key, gender FROM candidacy
GROUP BY name_key, gender, event HAVING count(DISTINCT (chamber, candidate_id)) > 1;
CREATE TEMP TABLE shared_residence ON COMMIT DROP AS
SELECT DISTINCT name_key, gender, upper(coalesce(residence, '')) AS residence FROM candidacy
GROUP BY name_key, gender, upper(coalesce(residence, '')), event HAVING count(DISTINCT (chamber, candidate_id)) > 1;

-- region: voivodeship of the residence; none for Warsaw, where members of parliament often move.
CREATE TEMP TABLE candidacy_person ON COMMIT DROP AS
SELECT c.*,
       CASE WHEN h.grp IS NOT NULL THEN 'manual'
            WHEN s.name_key IS NULL THEN 'name+gender' ELSE 'name+gender+residence' END AS match_rule,
       CASE WHEN h.grp IS NOT NULL THEN 'override|' || h.grp
            ELSE c.name_key || '|' || coalesce(c.gender, '') ||
                 CASE WHEN s.name_key IS NULL THEN '' ELSE '|' || upper(coalesce(c.residence, '')) END ||
                 CASE WHEN r.name_key IS NULL THEN ''
                      ELSE '|' || c.chamber || ' ' || c.year || '/' || c.round || '/' || c.candidate_id END END AS person_key,
       CASE WHEN c.powiat <> '1465' THEN left(c.powiat, 2) END AS region
FROM candidacy c
LEFT JOIN shared_name s ON s.name_key = c.name_key AND s.gender IS NOT DISTINCT FROM c.gender
LEFT JOIN shared_residence r ON r.name_key = c.name_key AND r.gender IS NOT DISTINCT FROM c.gender
 AND r.residence = upper(coalesce(c.residence, ''))
LEFT JOIN override_hit h ON (h.chamber, h.year, h.round, h.candidate_id) = (c.chamber, c.year, c.round, c.candidate_id);

-- Single given name, residences in several voivodeships: candidacies in the same voivodeship or place,
-- with alike occupations or birth years at most 1 apart are one part (connected components). A part
-- without any voivodeship joins the part of the nearest candidacy in time; several parts with a
-- voivodeship are several persons.
CREATE TEMP TABLE region_candidacy ON COMMIT DROP AS
SELECT *, chamber || ' ' || year || '/' || round || '/' || candidate_id AS node FROM candidacy_person
WHERE person_key IN (SELECT person_key FROM candidacy_person WHERE match_rule = 'name+gender' AND first_names !~ ' '
                     GROUP BY person_key HAVING count(DISTINCT region) > 1 AND NOT bool_or(chamber = 'prezydent'));
CREATE TEMP TABLE region_link ON COMMIT DROP AS
SELECT a.person_key, a.node AS node_a, b.node AS node_b
FROM region_candidacy a JOIN region_candidacy b USING (person_key)
WHERE a.node = b.node OR a.region = b.region OR a.place = b.place
   OR similarity(lower(a.occupation), lower(b.occupation)) >= 0.3
   OR a.occupation ~* '^student' OR b.occupation ~* '^student' OR abs(a.birth_year - b.birth_year) <= 1;
CREATE TEMP TABLE region_part ON COMMIT DROP AS
WITH RECURSIVE reach AS (
  SELECT person_key, node_a, node_b FROM region_link
  UNION
  SELECT r.person_key, r.node_a, l.node_b FROM reach r JOIN region_link l ON l.person_key = r.person_key AND l.node_a = r.node_b
)
SELECT person_key, node_a AS node, min(node_b) AS part FROM reach GROUP BY 1, 2;
CREATE TEMP TABLE region_part_known ON COMMIT DROP AS
SELECT DISTINCT p.person_key, p.part FROM region_part p JOIN region_candidacy c USING (person_key, node)
WHERE c.region IS NOT NULL;

UPDATE candidacy_person c SET person_key = c.person_key || '|' || x.part, match_rule = 'name+gender+voivodeship'
FROM (
  SELECT r.chamber, r.year, r.round, r.candidate_id,
         CASE WHEN k.part IS NOT NULL THEN p.part
              ELSE (SELECT q.part FROM region_candidacy n JOIN region_part q USING (person_key, node)
                    JOIN region_part_known m ON m.person_key = q.person_key AND m.part = q.part
                    WHERE n.person_key = r.person_key ORDER BY abs(n.year - r.year), n.year LIMIT 1) END AS part
  FROM region_candidacy r JOIN region_part p USING (person_key, node)
  LEFT JOIN region_part_known k ON k.person_key = p.person_key AND k.part = p.part
  WHERE r.person_key IN (SELECT person_key FROM region_part_known GROUP BY 1 HAVING count(*) > 1)
) x
WHERE (c.chamber, c.year, c.round, c.candidate_id) = (x.chamber, x.year, x.round, x.candidate_id);

-- Birth years more than 3 apart (registers that publish the age): candidacies are one part when
-- their birth years are at most 3 apart or they live in the same powiat or place (the 1990s registers
-- are off by up to 3 years, sometimes more, for the same person); several parts are several persons.
-- A candidacy without an age joins the one part with candidacies in its powiat or place, else the
-- part of the nearest candidacy in time.
CREATE TEMP TABLE aged_candidacy ON COMMIT DROP AS
SELECT *, chamber || ' ' || year || '/' || round || '/' || candidate_id AS node FROM candidacy_person
WHERE person_key IN (SELECT person_key FROM candidacy_person WHERE match_rule <> 'manual' AND birth_year IS NOT NULL
                     GROUP BY person_key HAVING max(birth_year) - min(birth_year) > 3);
CREATE TEMP TABLE birth_link ON COMMIT DROP AS
SELECT a.person_key, a.node AS node_a, b.node AS node_b
FROM aged_candidacy a JOIN aged_candidacy b USING (person_key)
WHERE a.birth_year IS NOT NULL AND b.birth_year IS NOT NULL
  AND (abs(a.birth_year - b.birth_year) <= 3 OR a.powiat = b.powiat OR a.place = b.place);
CREATE TEMP TABLE birth_part ON COMMIT DROP AS
WITH RECURSIVE reach AS (
  SELECT person_key, node_a, node_b FROM birth_link
  UNION
  SELECT r.person_key, r.node_a, l.node_b FROM reach r JOIN birth_link l ON l.person_key = r.person_key AND l.node_a = r.node_b
)
SELECT person_key, node_a AS node, min(node_b) AS part FROM reach GROUP BY 1, 2;

UPDATE candidacy_person c SET person_key = c.person_key || '|' || x.part, match_rule = 'name+gender+birth year'
FROM (
  SELECT a.chamber, a.year, a.round, a.candidate_id,
         coalesce(p.part,
                  (SELECT min(q.part) FROM aged_candidacy n JOIN birth_part q USING (person_key, node)
                   WHERE n.person_key = a.person_key AND (n.powiat = a.powiat OR n.place = a.place)
                   HAVING count(DISTINCT q.part) = 1),
                  (SELECT q.part FROM aged_candidacy n JOIN birth_part q USING (person_key, node)
                   WHERE n.person_key = a.person_key ORDER BY abs(n.year - a.year), n.year DESC LIMIT 1)) AS part
  FROM aged_candidacy a LEFT JOIN birth_part p USING (person_key, node)
  WHERE a.person_key IN (SELECT person_key FROM birth_part GROUP BY 1 HAVING count(DISTINCT part) > 1)
) x
WHERE (c.chamber, c.year, c.round, c.candidate_id) = (x.chamber, x.year, x.round, x.candidate_id);

-- Abbreviated given names (files before 2011): see the rule above.
CREATE TEMP TABLE key_name ON COMMIT DROP AS
SELECT person_key, upper(min(last_name)) AS last_name, min(gender) AS gender,
       upper(split_part(min(first_names), ' ', 1)) AS first_name, bool_and(first_names !~ ' ') AS single_name
FROM candidacy_person WHERE match_rule <> 'manual' GROUP BY person_key;
CREATE TEMP TABLE short_name_link ON COMMIT DROP AS
SELECT m.short_key, m.target_key
FROM (
  SELECT s.person_key AS short_key, min(t.person_key) AS target_key
  FROM key_name s
  JOIN key_name t ON t.last_name = s.last_name AND t.gender IS NOT DISTINCT FROM s.gender
   AND t.first_name = s.first_name AND NOT t.single_name
  WHERE s.single_name
  GROUP BY s.person_key HAVING count(*) = 1
) m
JOIN candidacy_person a ON a.person_key = m.short_key AND a.year < 2011
JOIN candidacy_person b ON b.person_key = m.target_key
GROUP BY m.short_key, m.target_key
HAVING NOT bool_or(a.event = b.event)
   AND NOT EXISTS (SELECT 1 FROM candidacy_person l WHERE l.person_key = m.short_key AND l.year >= 2011
                   AND NOT EXISTS (SELECT 1 FROM candidacy_person t WHERE t.person_key = m.target_key AND t.event = l.event))
   AND NOT coalesce(bool_or(a.region <> b.region AND coalesce(similarity(lower(a.occupation), lower(b.occupation)), 0) < 0.3
                            AND coalesce(a.occupation !~* '^student' AND b.occupation !~* '^student', true)), false)
   AND NOT coalesce(bool_or(abs(a.birth_year - b.birth_year) > 3), false)
   AND coalesce(bool_or(a.powiat = b.powiat OR a.place = b.place OR abs(a.birth_year - b.birth_year) <= 1), false);

UPDATE candidacy_person c SET person_key = l.target_key, match_rule = 'surname+first given name+gender'
FROM short_name_link l WHERE c.person_key = l.short_key AND c.year < 2011;

CREATE TEMP TABLE person_ids ON COMMIT DROP AS
SELECT person_key, CAST(dense_rank() OVER (ORDER BY person_key) AS INT) AS person_id
FROM (SELECT DISTINCT person_key FROM candidacy_person) k;

CREATE TEMP TABLE candidacy_ids ON COMMIT DROP AS
SELECT c.chamber, c.year, c.round, c.candidate_id, c.name_key, c.gender, c.powiat, c.birth_year, i.person_id
FROM candidacy_person c JOIN person_ids i USING (person_key);

TRUNCATE mart.persons;
INSERT INTO mart.persons (person_id, full_name, last_name, first_names, gender, match_rule, candidacies)
SELECT i.person_id,
       (array_agg(c.full_name ORDER BY c.year DESC, c.first_names ~ ' ' DESC))[1],
       (array_agg(c.last_name ORDER BY c.year DESC, c.first_names ~ ' ' DESC))[1],
       (array_agg(c.first_names ORDER BY c.year DESC, c.first_names ~ ' ' DESC))[1],
       (array_agg(c.gender ORDER BY c.year DESC))[1],
       CASE WHEN bool_or(c.match_rule = 'manual') THEN 'manual'
            WHEN bool_or(c.match_rule = 'surname+first given name+gender') THEN 'surname+first given name+gender'
            ELSE min(c.match_rule) END,
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
UPDATE mart.senat_byelections k SET person_id = c.person_id FROM candidacy_ids c
WHERE c.chamber = 'senat_uzup' AND c.year = k.year AND c.candidate_id = k.candidate_id;
"""

# Local candidates (sejmik, mayors, councils) are linked to a national person only when the link is
# unambiguous: exactly one national person has that full name and gender, and the name occurs once
# in that local election (one sejmik candidate, mayor candidates of one gmina, or one council
# candidate among all council candidates of the year). Local candidacies never create persons:
# tens of thousands of local namesakes would merge strangers.
# When several national persons share the name, the one whose residence lies in the local candidacy's
# powiat (sejmik: voivodeship) is taken, if there is exactly one: residence_teryt where published
# (national registers from 2019), else the residence text mapped through place names seen with a TERYT.
# Local registers publish the candidate's age; national registers mostly do not. All local candidacies
# linked to one person must agree on the birth year (election year minus age; two candidacies of
# one person differ by at most 1, because age is counted on election day), the birth year must allow
# the person's national candidacies (minimum ages) and agree with national ages where published
# (Sejm and Senate 1991-1997). When it does not, the name belongs to several people and none of that
# person's local candidacies stays linked.
# Overrides (PERSON_OVERRIDES) win over every rule.
# Finally, local candidacies still unlinked become local persons (match_rule 'local:...'): same
# name, gender and powiat (sejmik: voivodeship) with birth years at most 1 apart. This links a mayor
# or councillor across 2014, 2018 and 2024 even without a national candidacy.
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

UPDATE mart.rada_candidates SET person_id = NULL;
UPDATE mart.rada_candidates k SET person_id = n.person_id
FROM (SELECT year, full_name FROM mart.rada_candidates GROUP BY year, full_name HAVING count(*) = 1) u, national_name n
WHERE u.year = k.year AND u.full_name = k.full_name
  AND n.name_key = upper(regexp_replace(k.full_name, '\s+', ' ', 'g')) AND n.gender IS NOT DISTINCT FROM k.gender;

-- Names shared by several national persons: a local candidacy joins the one whose residence is in
-- the same powiat (sejmik: voivodeship), when exactly one such person exists and the name occurs once
-- there in that local election. Residence is residence_teryt where published (registers from 2019);
-- before 2019 the residence text mapped to a powiat through residence_place (see PERSONS_SQL).
CREATE TEMP TABLE national_residence ON COMMIT DROP AS
SELECT DISTINCT i.name_key, i.gender, i.powiat, i.person_id
FROM candidacy_ids i
WHERE i.powiat IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM national_name n WHERE n.name_key = i.name_key AND n.gender IS NOT DISTINCT FROM i.gender);

CREATE TEMP TABLE residence_powiat ON COMMIT DROP AS
SELECT name_key, gender, powiat, min(person_id) AS person_id FROM national_residence
GROUP BY name_key, gender, powiat HAVING count(DISTINCT person_id) = 1;
CREATE TEMP TABLE residence_woj ON COMMIT DROP AS
SELECT name_key, gender, left(powiat, 2) AS code, min(person_id) AS person_id FROM national_residence
GROUP BY name_key, gender, left(powiat, 2) HAVING count(DISTINCT person_id) = 1;
-- Temp tables get no automatic statistics; without them the joins below can become nested loops.
ANALYZE residence_powiat;
ANALYZE residence_woj;

-- Statistics after the resets above (ANALYZE sees this transaction's rows); with the stale ones the
-- planner expects one unlinked candidacy and turns the joins below into nested loops.
ANALYZE mart.sejmik_candidates;
ANALYZE mart.wbp_candidates;
ANALYZE mart.rada_candidates;
UPDATE mart.wbp_candidates k SET person_id = r.person_id
FROM residence_powiat r
WHERE k.person_id IS NULL AND r.powiat = left(k.teryt_gmina, 4)
  AND r.name_key = upper(regexp_replace(k.full_name, '\s+', ' ', 'g')) AND r.gender IS NOT DISTINCT FROM k.gender
  AND NOT EXISTS (SELECT 1 FROM mart.wbp_candidates o WHERE o.year = k.year AND o.full_name = k.full_name
                  AND left(o.teryt_gmina, 4) = left(k.teryt_gmina, 4) AND o.teryt_gmina <> k.teryt_gmina);
UPDATE mart.rada_candidates k SET person_id = r.person_id
FROM residence_powiat r
WHERE k.person_id IS NULL AND r.powiat = left(k.teryt, 4)
  AND r.name_key = upper(regexp_replace(k.full_name, '\s+', ' ', 'g')) AND r.gender IS NOT DISTINCT FROM k.gender
  AND NOT EXISTS (SELECT 1 FROM mart.rada_candidates o WHERE o.year = k.year AND o.full_name = k.full_name
                  AND left(o.teryt, 4) = left(k.teryt, 4) AND o.candidate_id <> k.candidate_id);
UPDATE mart.sejmik_candidates k SET person_id = r.person_id
FROM (SELECT DISTINCT wojewodztwo, left(teryt_gmina, 2) AS code FROM mart.sejmik_gmina_protocols) w, residence_woj r
WHERE k.person_id IS NULL AND w.wojewodztwo = k.wojewodztwo AND r.code = w.code
  AND r.name_key = upper(regexp_replace(k.full_name, '\s+', ' ', 'g')) AND r.gender IS NOT DISTINCT FROM k.gender
  AND NOT EXISTS (SELECT 1 FROM mart.sejmik_candidates o WHERE o.year = k.year AND o.full_name = k.full_name
                  AND o.wojewodztwo = k.wojewodztwo AND o.candidate_id <> k.candidate_id);

CREATE TEMP TABLE local_birth ON COMMIT DROP AS
SELECT person_id, year - age AS birth_year FROM mart.sejmik_candidates WHERE person_id IS NOT NULL AND age IS NOT NULL
UNION ALL
SELECT person_id, year - age FROM mart.wbp_candidates WHERE person_id IS NOT NULL AND age IS NOT NULL
UNION ALL
SELECT person_id, year - age FROM mart.rada_candidates WHERE person_id IS NOT NULL AND age IS NOT NULL;

-- The local birth years must also allow every national candidacy of the person: candidates must be
-- at least 21 (Sejm, European Parliament, Senate before 2001), 30 (Senate from 2001) or 35 (President)
-- years old. The true birth year is year - age or one less, so the earliest is min(year - age) - 1.
-- Where national registers publish the age (Sejm and Senate 1991-1997), birth years more than 3 apart
-- contradict (those registers are off by up to 3 years for the same person).
CREATE TEMP TABLE birth_conflict ON COMMIT DROP AS
SELECT person_id FROM local_birth GROUP BY person_id HAVING max(birth_year) - min(birth_year) > 1
UNION
SELECT b.person_id
FROM (SELECT person_id, min(birth_year) AS birth_year FROM local_birth GROUP BY person_id) b
JOIN candidacy_ids c USING (person_id)
WHERE c.year - (b.birth_year - 1) < CASE WHEN c.chamber = 'prezydent' THEN 35
                                         WHEN c.chamber = 'senat' AND c.year >= 2001 THEN 30 ELSE 21 END
UNION
SELECT b.person_id FROM local_birth b JOIN candidacy_ids c USING (person_id) WHERE abs(b.birth_year - c.birth_year) > 3;

UPDATE mart.sejmik_candidates SET person_id = NULL WHERE person_id IN (SELECT person_id FROM birth_conflict);
UPDATE mart.wbp_candidates SET person_id = NULL WHERE person_id IN (SELECT person_id FROM birth_conflict);
UPDATE mart.rada_candidates SET person_id = NULL WHERE person_id IN (SELECT person_id FROM birth_conflict);

-- Overrides: a local candidacy joins the person of the national candidacies in its group, or is
-- unlinked when the group has none.
CREATE TEMP TABLE override_person ON COMMIT DROP AS
SELECT h.grp, min(i.person_id) AS person_id
FROM override_hit h JOIN candidacy_ids i USING (chamber, year, round, candidate_id) GROUP BY h.grp;

UPDATE mart.sejmik_candidates k SET person_id = (SELECT person_id FROM override_person o WHERE o.grp = h.grp)
FROM override_hit h WHERE h.chamber = 'sejmik' AND h.year = k.year AND h.candidate_id = k.candidate_id;
UPDATE mart.wbp_candidates k SET person_id = (SELECT person_id FROM override_person o WHERE o.grp = h.grp)
FROM override_hit h WHERE h.chamber = 'wbp' AND h.year = k.year AND h.round = k.round AND h.candidate_id = k.candidate_id;
UPDATE mart.rada_candidates k SET person_id = (SELECT person_id FROM override_person o WHERE o.grp = h.grp)
FROM override_hit h WHERE h.chamber = 'rada' AND h.year = k.year AND h.candidate_id = k.candidate_id;

-- Local persons. Local candidacies with an age that are still unlinked are grouped by name, gender
-- and powiat into runs of birth years at most 1 apart ("islands"). An island that contains
-- candidacies of exactly one national person joins that person; an island without one becomes a
-- new local person; an island with several national persons stays unlinked. Overridden candidacies
-- keep the override.
CREATE TEMP TABLE local_candidacy ON COMMIT DROP AS
SELECT 'wbp' AS chamber, k.year, k.round, k.candidate_id, k.full_name, k.last_name, k.first_names, k.gender,
       k.year - k.age AS birth_year, upper(regexp_replace(k.full_name, '\s+', ' ', 'g')) AS name_key,
       left(k.teryt_gmina, 4) AS area, k.person_id
FROM mart.wbp_candidates k
WHERE k.age IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM override_hit h WHERE h.chamber = 'wbp' AND (h.year, h.round, h.candidate_id) = (k.year, k.round, k.candidate_id))
UNION ALL
SELECT 'rada', k.year, 1, k.candidate_id, k.full_name, k.last_name, k.first_names, k.gender,
       k.year - k.age, upper(regexp_replace(k.full_name, '\s+', ' ', 'g')), left(k.teryt, 4), k.person_id
FROM mart.rada_candidates k
WHERE k.age IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM override_hit h WHERE h.chamber = 'rada' AND (h.year, h.candidate_id) = (k.year, k.candidate_id));

-- Some local files before 2010 give only the first given name ("Artur KOZIOŁ" as mayor in 2002 and
-- 2006, "Artur Stanisław KOZIOŁ" from 2010). Such a candidacy takes the full name of the one name with
-- more given names, the same surname, first given name, gender and powiat, and a birth year at most 1
-- apart, used in another election.
UPDATE local_candidacy c SET name_key = m.name_key
FROM (
  SELECT s.chamber, s.year, s.round, s.candidate_id, min(f.name_key) AS name_key
  FROM local_candidacy s
  JOIN local_candidacy f ON f.last_name = s.last_name AND f.area = s.area AND f.gender IS NOT DISTINCT FROM s.gender
   AND f.first_names LIKE '% %' AND split_part(f.first_names, ' ', 1) = s.first_names
   AND f.year <> s.year AND abs(f.birth_year - s.birth_year) <= 1
  WHERE s.year < 2010 AND s.first_names NOT LIKE '% %'
  GROUP BY s.chamber, s.year, s.round, s.candidate_id
  HAVING count(DISTINCT f.name_key) = 1
) m
WHERE (m.chamber, m.year, m.round, m.candidate_id) = (c.chamber, c.year, c.round, c.candidate_id);

CREATE TEMP TABLE local_island ON COMMIT DROP AS
SELECT c.*, name_key || '|' || coalesce(gender, '') || '|' || area || '|' ||
       sum(new_island) OVER (PARTITION BY name_key, gender, area ORDER BY birth_year, year, chamber, round, candidate_id) AS island
FROM (
  SELECT c.*, CASE WHEN birth_year - lag(birth_year) OVER (
                 PARTITION BY name_key, gender, area ORDER BY birth_year, year, chamber, round, candidate_id) > 1
              THEN 1 ELSE 0 END AS new_island
  FROM local_candidacy c
) c;

CREATE TEMP TABLE island_person ON COMMIT DROP AS
SELECT island, count(DISTINCT person_id) AS national_persons, min(person_id) AS person_id,
       min(birth_year) AS birth_min, max(birth_year) AS birth_max,
       min(name_key) AS name_key, min(gender) AS gender, min(area) AS area
FROM local_island GROUP BY island;

UPDATE island_person i SET person_id = n.next_id + n.rn
FROM (SELECT island, (SELECT coalesce(max(person_id), 0) FROM mart.persons) AS next_id,
             CAST(dense_rank() OVER (ORDER BY island) AS INT) AS rn
      FROM island_person WHERE national_persons = 0) n
WHERE n.island = i.island;

-- Sejmik candidacies (voivodeship-wide, no powiat) join the one local or national island of the same
-- name, gender and voivodeship whose birth years fit; otherwise they form their own islands.
CREATE TEMP TABLE woj_code ON COMMIT DROP AS
SELECT DISTINCT wojewodztwo, left(teryt_gmina, 2) AS code FROM mart.sejmik_gmina_protocols;

CREATE TEMP TABLE sejmik_base ON COMMIT DROP AS
SELECT k.year, k.candidate_id, k.full_name, k.last_name, k.first_names, k.gender, k.year - k.age AS birth_year,
       upper(regexp_replace(k.full_name, '\s+', ' ', 'g')) AS name_key, w.code
FROM mart.sejmik_candidates k JOIN woj_code w USING (wojewodztwo)
WHERE k.person_id IS NULL AND k.age IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM override_hit h WHERE h.chamber = 'sejmik' AND (h.year, h.candidate_id) = (k.year, k.candidate_id));

CREATE TEMP TABLE sejmik_local ON COMMIT DROP AS
SELECT s.*, m.islands_matched, CASE WHEN m.islands_matched = 1 THEN m.person_id END AS island_person_id
FROM sejmik_base s
LEFT JOIN (
  SELECT s.year, s.candidate_id, count(DISTINCT i.person_id) AS islands_matched, min(i.person_id) AS person_id
  FROM sejmik_base s
  JOIN island_person i ON i.name_key = s.name_key AND i.gender IS NOT DISTINCT FROM s.gender
   AND left(i.area, 2) = s.code AND s.birth_year BETWEEN i.birth_min - 1 AND i.birth_max + 1
   AND i.national_persons <= 1
  GROUP BY s.year, s.candidate_id
) m USING (year, candidate_id);

CREATE TEMP TABLE sejmik_island ON COMMIT DROP AS
SELECT s.*, name_key || '|' || coalesce(gender, '') || '|W' || code || '|' ||
       sum(new_island) OVER (PARTITION BY name_key, gender, code ORDER BY birth_year, year, candidate_id) AS island
FROM (
  SELECT s.*, CASE WHEN birth_year - lag(birth_year) OVER (PARTITION BY name_key, gender, code ORDER BY birth_year, year, candidate_id) > 1
                   THEN 1 ELSE 0 END AS new_island
  FROM sejmik_local s WHERE islands_matched IS NULL
) s;

CREATE TEMP TABLE sejmik_island_person ON COMMIT DROP AS
SELECT island, greatest((SELECT coalesce(max(person_id), 0) FROM island_person),
                       (SELECT coalesce(max(person_id), 0) FROM mart.persons))
              + CAST(dense_rank() OVER (ORDER BY island) AS INT) AS person_id
FROM (SELECT DISTINCT island FROM sejmik_island) i;

INSERT INTO mart.persons (person_id, full_name, last_name, first_names, gender, match_rule, candidacies)
SELECT i.person_id, (array_agg(c.full_name ORDER BY c.year DESC))[1], (array_agg(c.last_name ORDER BY c.year DESC))[1],
       (array_agg(c.first_names ORDER BY c.year DESC))[1], (array_agg(c.gender ORDER BY c.year DESC))[1],
       'local:name+gender+birth year+powiat', count(*)
FROM island_person i JOIN local_island c USING (island)
WHERE i.national_persons = 0
GROUP BY i.person_id
UNION ALL
SELECT i.person_id, (array_agg(c.full_name ORDER BY c.year DESC))[1], (array_agg(c.last_name ORDER BY c.year DESC))[1],
       (array_agg(c.first_names ORDER BY c.year DESC))[1], (array_agg(c.gender ORDER BY c.year DESC))[1],
       'local:name+gender+birth year+voivodeship', count(*)
FROM sejmik_island_person i JOIN sejmik_island c USING (island)
GROUP BY i.person_id;

UPDATE mart.wbp_candidates k SET person_id = i.person_id
FROM local_island c JOIN island_person i USING (island)
WHERE c.chamber = 'wbp' AND (c.year, c.round, c.candidate_id) = (k.year, k.round, k.candidate_id)
  AND k.person_id IS NULL AND i.national_persons <= 1;
UPDATE mart.rada_candidates k SET person_id = i.person_id
FROM local_island c JOIN island_person i USING (island)
WHERE c.chamber = 'rada' AND (c.year, c.candidate_id) = (k.year, k.candidate_id)
  AND k.person_id IS NULL AND i.national_persons <= 1;
UPDATE mart.sejmik_candidates k SET person_id = s.island_person_id
FROM sejmik_local s WHERE (s.year, s.candidate_id) = (k.year, k.candidate_id) AND s.island_person_id IS NOT NULL;
UPDATE mart.sejmik_candidates k SET person_id = i.person_id
FROM sejmik_island c JOIN sejmik_island_person i USING (island)
WHERE (c.year, c.candidate_id) = (k.year, k.candidate_id);

"""

# Manual person corrections, one row per candidacy (or set of candidacies): columns election, year,
# full_name, place, person, note. All candidacies with the same ``person`` label are one person; a
# label used by one candidacy splits it from its namesakes. ``place`` narrows the match: district
# number (sejm, senat, pe), voivodeship (sejmik), TERYT (wbp: gmina, rada: council unit); empty
# matches every candidacy of that name in that election and year. Every row must match at least one
# candidacy, otherwise the build fails.
PERSON_OVERRIDES = Path(__file__).with_name("person_overrides.csv")

OVERRIDE_HITS_SQL = r"""
CREATE TEMP TABLE override_hit ON COMMIT DROP AS
SELECT o.line, x.chamber, x.year, x.round, x.candidate_id, o.grp
FROM person_override o
JOIN (
  SELECT 'sejm' AS chamber, year, 1 AS round, candidate_id, full_name, district_no::text AS place FROM mart.sejm_candidates
  UNION ALL SELECT 'senat', year, 1, candidate_id, full_name, district_no::text FROM mart.senat_candidates
  UNION ALL SELECT 'prezydent', year, round, candidate_id, full_name, NULL FROM mart.prezydent_candidates
  UNION ALL SELECT 'pe', year, 1, candidate_id, full_name, district_no::text FROM mart.pe_candidates
  UNION ALL SELECT 'sejmik', year, 1, candidate_id, full_name, wojewodztwo FROM mart.sejmik_candidates
  UNION ALL SELECT 'wbp', year, round, candidate_id, full_name, teryt_gmina FROM mart.wbp_candidates
  UNION ALL SELECT 'rada', year, 1, candidate_id, full_name, teryt FROM mart.rada_candidates
) x ON x.chamber = o.election AND x.year = o.year AND x.full_name = o.full_name
   AND (o.place IS NULL OR x.place = o.place);
"""


def load_person_overrides(pg: psycopg.Connection, path: Path = PERSON_OVERRIDES) -> int:
    """Load ``path`` into the ``person_override`` / ``override_hit`` temp tables; return its row count.

    Raises ValueError when a row matches no candidacy (a typo would otherwise be ignored silently).
    """
    with path.open(encoding="utf-8", newline="") as fh:
        rows = [
            (line, r["election"].strip(), int(r["year"]), r["full_name"].strip(), r["place"].strip() or None, r["person"].strip())
            for line, r in enumerate(csv.DictReader(fh), start=2)
        ]
    pg.execute(
        "CREATE TEMP TABLE person_override (line INT, election TEXT, year INT, full_name TEXT, place TEXT, grp TEXT)"
        " ON COMMIT DROP"
    )
    with pg.cursor() as cur:
        cur.executemany("INSERT INTO person_override VALUES (%s, %s, %s, %s, %s, %s)", rows)
    pg.execute(OVERRIDE_HITS_SQL)
    missing = pg.execute(
        "SELECT line FROM person_override WHERE line NOT IN (SELECT line FROM override_hit) ORDER BY line"
    ).fetchall()
    if missing:
        raise ValueError(f"{path.name}: lines {[m[0] for m in missing]} match no candidacy")
    return len(rows)


# Key from the 1975-1998 gmina codes to 1999 and current TERYT, matched by name and type and
# documented code by code (sources and method inside the file); the UI maps use the same file.
GMINA_CODES_PRE1999 = Path(__file__).resolve().parents[3] / "streamlit_app" / "geo" / "gminy_pre1999.json"
VOIVODESHIPS_1999 = {
    "02": "dolnośląskie", "04": "kujawsko-pomorskie", "06": "lubelskie", "08": "lubuskie", "10": "łódzkie",
    "12": "małopolskie", "14": "mazowieckie", "16": "opolskie", "18": "podkarpackie", "20": "podlaskie",
    "22": "pomorskie", "24": "śląskie", "26": "świętokrzyskie", "28": "warmińsko-mazurskie", "30": "wielkopolskie",
    "32": "zachodniopomorskie",
}
GMINA_CODES_PRE1999_SQL = r"""
DROP TABLE IF EXISTS mart.gmina_codes_pre1999;
CREATE TABLE mart.gmina_codes_pre1999 (
    teryt_gmina_pre1999 TEXT PRIMARY KEY,
    wojewodztwo_1975 TEXT NOT NULL,
    gmina TEXT NOT NULL,
    years INT[] NOT NULL,
    teryt_1999 TEXT,
    wojewodztwo TEXT,
    teryt_powiat TEXT,
    powiat TEXT,
    teryt_gmina_current TEXT[] NOT NULL,
    match TEXT NOT NULL
);
COMMENT ON TABLE mart.gmina_codes_pre1999 IS 'Key from the gmina codes of the 1975-1998 division (teryt_gmina_pre1999 in precincts, prezydent_precinct_protocols and referendum_precinct_protocols, elections 1990-1997) to the 1999 TERYT gmina and today''s units. No official key exists; every code was matched by name and gmina type (GUS BDL units 1995-1998, GUS change notices). Join on teryt_gmina_pre1999 to sum 1990s results by today''s voivodeship or powiat. Before 1995 a parent gmina''s code also covers gminas split off later (e.g. Mysłowice with Imielin and Chełm Śląski until 1994), and the 1991 Sejm file has no row for 24 rural gminas (counted under the town of the same name), so powiat sums for those years are approximate.';
COMMENT ON COLUMN mart.gmina_codes_pre1999.teryt_gmina_pre1999 IS '5-digit code of the old division: 2 digits for the 1975-1998 voivodeship (odd numbers 01-97), 3 for the gmina.';
COMMENT ON COLUMN mart.gmina_codes_pre1999.wojewodztwo_1975 IS 'Voivodeship of the 1975-1998 division (one of 49, e.g. katowickie, bialskopodlaskie), as in the 1990s result tables.';
COMMENT ON COLUMN mart.gmina_codes_pre1999.gmina IS 'Gmina name as published by PKW.';
COMMENT ON COLUMN mart.gmina_codes_pre1999.years IS 'Election years in which PKW used this code.';
COMMENT ON COLUMN mart.gmina_codes_pre1999.teryt_1999 IS '6-digit TERYT of the same gmina on 1999-01-01; NULL for the 7 Warsaw dzielnice of 1990-1994, which cover several later districts (see teryt_gmina_current).';
COMMENT ON COLUMN mart.gmina_codes_pre1999.wojewodztwo IS 'Today''s voivodeship (one of 16) containing the gmina; every old gmina lies inside one.';
COMMENT ON COLUMN mart.gmina_codes_pre1999.teryt_powiat IS '4-digit powiat of teryt_1999 (the 1999 powiat; a few powiats were renumbered in 2002).';
COMMENT ON COLUMN mart.gmina_codes_pre1999.powiat IS 'Powiat name as in the 2000-2001 result tables (Warsaw: warszawski); NULL when neither election is loaded.';
COMMENT ON COLUMN mart.gmina_codes_pre1999.teryt_gmina_current IS 'Current TERYT gmina code(s) covering the gmina: one code, several for the Warsaw dzielnice of 1990-1994, none for Ostrowice (split in 2019).';
COMMENT ON COLUMN mart.gmina_codes_pre1999.match IS 'How the code was matched (name and type, name only, renamed with the act, Warsaw dzielnica).';
"""
GMINA_CODES_PRE1999_POWIAT_SQL = """
UPDATE mart.gmina_codes_pre1999 k SET powiat = n.powiat
FROM (
  SELECT teryt_powiat, mode() WITHIN GROUP (ORDER BY powiat) AS powiat
  FROM (SELECT left(teryt_gmina, 4) AS teryt_powiat, powiat FROM mart.precincts WHERE year = 2001
        UNION ALL SELECT left(teryt_gmina, 4), powiat FROM mart.prezydent_precinct_protocols WHERE year = 2000) x
  WHERE teryt_powiat IS NOT NULL AND powiat IS NOT NULL
  GROUP BY teryt_powiat
) n
WHERE n.teryt_powiat = {PRE1999_TERYT_POWIAT}
"""
GMINA_CODES_PRE1999_CHECK_SQL = r"""
SELECT d.teryt_gmina_pre1999, d.year, d.wojewodztwo, k.wojewodztwo_1975, k.years
FROM (
  SELECT DISTINCT teryt_gmina_pre1999, year, wojewodztwo FROM mart.precincts WHERE teryt_gmina_pre1999 IS NOT NULL
  UNION SELECT DISTINCT teryt_gmina_pre1999, year, wojewodztwo FROM mart.prezydent_precinct_protocols WHERE teryt_gmina_pre1999 IS NOT NULL
  UNION SELECT DISTINCT teryt_gmina_pre1999, year, wojewodztwo FROM mart.referendum_precinct_protocols WHERE teryt_gmina_pre1999 IS NOT NULL
) d
LEFT JOIN mart.gmina_codes_pre1999 k USING (teryt_gmina_pre1999)
WHERE k.teryt_gmina_pre1999 IS NULL OR k.wojewodztwo_1975 <> d.wojewodztwo OR NOT d.year = ANY (k.years)
ORDER BY 1, 2
"""


def load_gmina_codes_pre1999(pg: psycopg.Connection, path: Path = GMINA_CODES_PRE1999) -> int:
    """Create ``mart.gmina_codes_pre1999`` from ``path``; return its row count.

    Raises ValueError when a pre-1999 code in the result tables is missing from the key, or the key
    gives it another old voivodeship or does not list that election year.
    """
    codes = json.loads(path.read_text(encoding="utf-8"))["codes"]
    rows = []
    for code, c in codes.items():
        teryt = c.get("teryt_1999")
        current = c["current"]
        voivodeship = VOIVODESHIPS_1999[(teryt or current[0])[:2]]
        rows.append((code, c["wojewodztwo"], c["name"], c["years"], teryt, voivodeship, teryt[:4] if teryt else None,
                     current, c["match"]))
    pg.execute(GMINA_CODES_PRE1999_SQL)
    with pg.cursor() as cur:
        cur.executemany(
            "INSERT INTO mart.gmina_codes_pre1999 (teryt_gmina_pre1999, wojewodztwo_1975, gmina, years, teryt_1999,"
            " wojewodztwo, teryt_powiat, teryt_gmina_current, match) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            rows,
        )
    # 1999 powiat codes are the ones the 2000-2001 files use (renumbering came in 2002).
    pg.execute(GMINA_CODES_PRE1999_POWIAT_SQL.format(PRE1999_TERYT_POWIAT=PRE1999_TERYT_POWIAT))
    errors = pg.execute(GMINA_CODES_PRE1999_CHECK_SQL).fetchall()
    if errors:
        raise ValueError(f"{path.name}: {len(errors)} pre-1999 codes missing or inconsistent: {errors[:5]}")
    return len(rows)


def link_persons(pg: psycopg.Connection, overrides: Path = PERSON_OVERRIDES) -> None:
    """Rebuild ``mart.persons`` and every ``person_id`` from the candidate tables (caller's transaction)."""
    log.info("person overrides: %s rows", load_person_overrides(pg, overrides))
    pg.execute(PERSONS_SQL)
    pg.execute(LOCAL_PERSONS_SQL)

# Colloquial party families for comparing elections: the first alias of a committee
# (``COMMITTEE_ALIASES`` in build_common.py) -> (family, note shown in mart.party_families). Committees
# without aliases (local committees) fall into 'other'; first aliases not listed are their own family.
# Families group predecessor and successor committees (PO -> KO); v_party_change marks such pairs as
# not directly comparable. Notes are Polish because they are shown to users.
PARTY_FAMILIES: dict[str, tuple[str, str]] = {
    "PiS": ("PiS", "Prawo i Sprawiedliwość; od 2015 lista Zjednoczonej Prawicy."),
    "Solidarna Polska": ("PiS", "Solidarna Polska: osobna lista w 2014, od 2015 w Zjednoczonej Prawicy."),
    "Polska Razem": ("PiS", "Polska Razem: osobna lista w 2014, od 2015 w Zjednoczonej Prawicy."),
    "PO": ("KO", "Platforma Obywatelska; od 2018 trzon Koalicji Obywatelskiej."),
    "KO": ("KO", "Koalicja Obywatelska (od 2018: PO, Nowoczesna i inni)."),
    "Nowoczesna": ("KO", "Nowoczesna: osobna lista w 2015, od 2018 w Koalicji Obywatelskiej."),
    "KE": ("KE", "Koalicja Europejska (PE 2019: PO, PSL, SLD, Nowoczesna, Zieloni)."),
    "SLD": ("Lewica", "Sojusz Lewicy Demokratycznej (także SLD-UP)."),
    "LiD": ("Lewica", "Lewica i Demokraci (2007: SLD, SDPL, PD, UP)."),
    "ZL": ("Lewica", "Zjednoczona Lewica (2015: SLD, Twój Ruch i inni)."),
    "Lewica": ("Lewica", "Lewica (od 2019: SLD/Nowa Lewica, Wiosna, w 2019 i 2023 także Razem)."),
    "NL": ("Lewica", "Nowa Lewica (połączenie SLD i Wiosny)."),
    "Wiosna": ("Lewica", "Wiosna: osobna lista w PE 2019, od 2019 w Lewicy."),
    "Lewica Razem": ("Razem", "Lewica Razem (PE 2019: Razem, UP, RSS)."),
    "Razem": ("Razem", "Partia Razem."),
    "PSL": ("TD/PSL", "Polskie Stronnictwo Ludowe (2019: Koalicja Polska)."),
    "TD": ("TD/PSL", "Trzecia Droga (2023: PSL i Polska 2050)."),
    "Konfederacja": ("Konfederacja", "Konfederacja (od 2019: m.in. KORWiN, później Nowa Nadzieja, i Ruch Narodowy)."),
    "KORWiN": ("Konfederacja", "Komitety Janusza Korwin-Mikkego: Platforma JKM (2005), Ruch Wyborców JKM (sejmiki 2010), KORWiN (2015); od 2019 w Konfederacji."),
    "KNP": ("Konfederacja", "Kongres Nowej Prawicy / Nowa Prawica Janusza Korwin-Mikkego (2011-2014), poprzednik KORWiN."),
    "RN": ("Konfederacja", "Ruch Narodowy, od 2019 w Konfederacji."),
    "POPiS": ("POPiS", "Wspólne listy PO i PiS do sejmików w 2002 r."),
    "NSZZ Solidarność": ("AWS", "Listy NSZZ Solidarność (1991, 1993); związek założył AWS w 1996."),
    "AWS": ("AWS", "Akcja Wyborcza Solidarność (1996–2001)."),
    "POC": ("PC", "Porozumienie Obywatelskie Centrum (1991), lista Porozumienia Centrum."),
    "PC": ("PC", "Porozumienie Centrum."),
    "WAK": ("WAK", "Wyborcza Akcja Katolicka (1991, m.in. ZChN)."),
    "KKW Ojczyzna": ("WAK", "Katolicki Komitet Wyborczy Ojczyzna (1993), następca WAK."),
    "AWSP": ("AWS", "Akcja Wyborcza Solidarność Prawicy (Sejm 2001), następczyni AWS."),
    "PS": ("TD/PSL", "Przymierze Społeczne (sejmiki 1998: PSL, UP i KPEiR)."),
    "RPO": ("RPO", "Ruch Patriotyczny Ojczyzna, prawicowy blok w wyborach samorządowych 1998."),
    "Rodzina Polska": ("Rodzina Polska", "Stowarzyszenie Rodzina Polska (wybory samorządowe 1998)."),
    "UD": ("UW/PD", "Unia Demokratyczna (1991–1994), w 1994 połączona z KLD w Unię Wolności."),
    "KLD": ("UW/PD", "Kongres Liberalno-Demokratyczny (1991–1994), w 1994 połączony z UD w Unię Wolności."),
    "UW": ("UW/PD", "Unia Wolności; w 2005 przekształcona w Partię Demokratyczną."),
    "PD": ("UW/PD", "Partia Demokratyczna (następczyni Unii Wolności)."),
}


# Elections for which PKW computed the official turnout from ballots issued; later PKW uses valid
# ballots taken from the ballot box (both stored in the protocols). Checked against PKW's figures.
TURNOUT_FROM_BALLOTS_ISSUED = (("sejm", 2001), ("sejm", 2005), ("sejm", 2007), ("senat", 2001), ("senat", 2005),
                               ("senat", 2007), ("prezydent", 2000), ("prezydent", 2005), ("prezydent", 2010),
                               ("sejmik", 2002), ("sejmik", 2006), ("wbp", 2002), ("wbp", 2006))

TURNOUT_BASIS_COMMENT = (
    "Numerator of the official turnout, as PKW computed it: ''ballots_issued'' (ballot papers issued to "
    "voters; Sejm/Senate 2001, 2005 and 2007, president 2000, 2005 and 2010, sejmiki and mayors 2002 and 2006) or ''valid_ballots'' (valid ballot papers "
    "taken from the ballot box; every other election). Both counts are columns of this view."
)


def _turnout_basis_sql(election: str, year: str) -> str:
    """SQL expression: 'ballots_issued' or 'valid_ballots', PKW's turnout basis for that election-year."""
    pairs = ", ".join(f"('{e}', {y})" for e, y in TURNOUT_FROM_BALLOTS_ISSUED)
    return f"CASE WHEN ({election}, {year}) IN ({pairs}) THEN 'ballots_issued' ELSE 'valid_ballots' END"


def _turnout_pct_sql(basis: str, issued: str, valid: str, eligible: str) -> str:
    """SQL expression: turnout in percent on the given basis."""
    return (f"round(100.0 * CASE WHEN {basis} = 'ballots_issued' THEN {issued} ELSE {valid} END"
            f" / NULLIF({eligible}, 0), 2)")


def _party_family_sql(aliases: str) -> str:
    """SQL expression mapping a committee's aliases array to its party family."""
    cases = " ".join(f"WHEN '{alias}' THEN '{family}'" for alias, (family, _) in PARTY_FAMILIES.items())
    return f"coalesce(CASE ({aliases})[1] {cases} ELSE ({aliases})[1] END, 'other')"


LEVEL_CASE = """CASE WHEN GROUPING(teryt_gmina) = 0 THEN 'gmina'
            WHEN GROUPING(teryt_powiat) = 0 THEN 'powiat'
            WHEN GROUPING(wojewodztwo) = 0 THEN 'wojewodztwo'
            WHEN GROUPING(wojewodztwo_1975) = 0 THEN 'wojewodztwo_1975'
            ELSE 'country' END"""
# Old voivodeships (level wojewodztwo_1975) report their name in the wojewodztwo column.
WOJEWODZTWO_CASE = "CASE WHEN GROUPING(wojewodztwo_1975) = 0 THEN wojewodztwo_1975 ELSE wojewodztwo END"
# Grouping sets whose unit is unknown (abroad, ships, Warsaw dzielnice of 1990-1994 at gmina level) are dropped.
ROLLUP_HAVING = """(GROUPING(wojewodztwo) = 1 OR wojewodztwo IS NOT NULL)
   AND (GROUPING(wojewodztwo_1975) = 1 OR wojewodztwo_1975 IS NOT NULL)
   AND (GROUPING(teryt_powiat) = 1 OR teryt_powiat IS NOT NULL)
   AND (GROUPING(teryt_gmina) = 1 OR teryt_gmina IS NOT NULL)"""


def _rollup(source: str, keys: str, measures: str) -> str:
    """Aggregate ``source`` to country, voivodeship, powiat and gmina rows (column ``level``).

    ``source`` must provide wojewodztwo, teryt_powiat, powiat, teryt_gmina, gmina and
    wojewodztwo_1975 besides ``keys`` (``GEO`` or ``GEO_PRE1999``). Rows abroad and on ships (no
    TERYT) count only in the country row; pre-1999 rows also give level wojewodztwo_1975.
    """
    return f"""
SELECT {keys}, {LEVEL_CASE} AS level, {WOJEWODZTWO_CASE} AS wojewodztwo, teryt_powiat, powiat, teryt_gmina, gmina,
       {measures}
FROM ({source}) s
GROUP BY GROUPING SETS (
  ({keys}),
  ({keys}, wojewodztwo),
  ({keys}, wojewodztwo, teryt_powiat, powiat),
  ({keys}, wojewodztwo, teryt_powiat, powiat, teryt_gmina, gmina),
  ({keys}, wojewodztwo_1975)
)
HAVING {ROLLUP_HAVING}"""


UNIT_WINDOW = "PARTITION BY {keys}, a.level, a.wojewodztwo, a.teryt_powiat, a.teryt_gmina"
# Geography of a precinct/gmina row; abroad, ships and the pseudo codes some years use for ships
# (powiat part 98/99, e.g. 229801 'Statki') get NULL so they count only in the country row.
_DOMESTIC = "p.teryt_gmina IS NOT NULL AND substr(p.teryt_gmina, 3, 2) NOT IN ('98', '99')"
_GEO_COLUMNS = (
    ("p.wojewodztwo", "wojewodztwo"),
    ("left(p.teryt_gmina, 4)", "teryt_powiat"),
    ("p.powiat", "powiat"),
    ("p.teryt_gmina", "teryt_gmina"),
    ("p.gmina", "gmina"),
)
GEO = ", ".join(f"CASE WHEN {_DOMESTIC} THEN {expr} END AS {name}" for expr, name in _GEO_COLUMNS) + (
    ", NULL::text AS wojewodztwo_1975"
)
# Tables with pre-1999 rows (precincts, prezydent_precinct_protocols, referendum_precinct_protocols):
# 1990-1997 precincts get today's units through mart.gmina_codes_pre1999 (join PRE1999_JOIN) and keep
# their old voivodeship as wojewodztwo_1975. The Warsaw dzielnice of 1990-1994 have no single gmina.
# Warsaw's gminas (1999 powiat 1431) are keyed as their 1465xx districts, as in the 2000-2001 tables.
PRE1999_TERYT_GMINA = (
    "CASE WHEN k.teryt_1999 LIKE '1431%' AND cardinality(k.teryt_gmina_current) = 1"
    " THEN k.teryt_gmina_current[1] ELSE k.teryt_1999 END"
)
PRE1999_TERYT_POWIAT = "CASE WHEN k.teryt_1999 LIKE '1431%' OR k.teryt_1999 IS NULL THEN '1465' ELSE k.teryt_powiat END"
_PRE1999_COLUMNS = {
    "wojewodztwo": "k.wojewodztwo",
    "teryt_powiat": PRE1999_TERYT_POWIAT,
    "powiat": "k.powiat",
    "teryt_gmina": PRE1999_TERYT_GMINA,
    "gmina": "CASE WHEN k.teryt_1999 IS NOT NULL THEN p.gmina END",
}
GEO_PRE1999 = ", ".join(
    f"CASE WHEN {_DOMESTIC} THEN {expr} ELSE {_PRE1999_COLUMNS[name]} END AS {name}" for expr, name in _GEO_COLUMNS
) + ", CASE WHEN k.teryt_gmina_pre1999 IS NOT NULL THEN p.wojewodztwo END AS wojewodztwo_1975"
PRE1999_JOIN = "LEFT JOIN mart.gmina_codes_pre1999 k ON k.teryt_gmina_pre1999 = p.teryt_gmina_pre1999"
LEVEL_COMMENT = (
    "Aggregation level of the row: country, wojewodztwo, powiat or gmina (today''s units; 1990-1997 results through"
    " mart.gmina_codes_pre1999), or wojewodztwo_1975 for the 49 voivodeships of 1975-1998 (1990-1997 only; name in"
    " wojewodztwo). Abroad and ships count only in country."
)
PRE1999_UNITS_NOTE = (
    "1990-1997 precincts are placed in today''s gminas, powiats and voivodeships through mart.gmina_codes_pre1999"
    " (gmina = the 1999 gmina; before 1995 a parent gmina also covers gminas split off later, and in 1991 24 rural"
    " gminas are counted with the town of the same name; the Warsaw dzielnice of 1990-1994 have only powiat rows)"
    " and also in the 49 old voivodeships (level wojewodztwo_1975)."
)

DROP_SQL = r"""
DROP VIEW IF EXISTS mart.v_precinct_results;
DROP VIEW IF EXISTS mart.v_candidate_gmina_votes;
-- v_person_history was a plain view before it was materialized; drop either kind.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
               WHERE n.nspname = 'mart' AND c.relname = 'v_person_history' AND c.relkind = 'v') THEN
        DROP VIEW mart.v_person_history;
    END IF;
END $$;
DROP MATERIALIZED VIEW IF EXISTS mart.v_person_history;
DROP MATERIALIZED VIEW IF EXISTS mart.v_gmina_continuity;
DROP MATERIALIZED VIEW IF EXISTS mart.v_gus_units;
DROP MATERIALIZED VIEW IF EXISTS mart.v_seat_methods;
DROP MATERIALIZED VIEW IF EXISTS mart.v_party_change;
DROP MATERIALIZED VIEW IF EXISTS mart.v_party_results;
DROP TABLE IF EXISTS mart.party_families;
DROP MATERIALIZED VIEW IF EXISTS mart.v_turnout;
DROP MATERIALIZED VIEW IF EXISTS mart.v_sejmik_results;
DROP MATERIALIZED VIEW IF EXISTS mart.v_rada_results;
DROP MATERIALIZED VIEW IF EXISTS mart.v_referendum_results;
DROP MATERIALIZED VIEW IF EXISTS mart.v_pe_national_results;
DROP MATERIALIZED VIEW IF EXISTS mart.v_prezydent_results;
DROP MATERIALIZED VIEW IF EXISTS mart.v_sejm_national_results;
DROP MATERIALIZED VIEW IF EXISTS mart.v_sejm_list_results;
DROP MATERIALIZED VIEW IF EXISTS mart.v_sejm_turnout;
"""

SEJM_VIEWS_SQL = rf"""
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
       CASE WHEN c.is_minority OR t.year = 1991 THEN NULL WHEN c.is_coalition THEN 8 ELSE 5 END AS threshold_pct,
       c.is_minority OR t.year = 1991
         OR 100.0 * t.votes / a.votes >= CASE WHEN c.is_coalition THEN 8 ELSE 5 END AS passed_threshold,
       CAST(t.seats AS INT) AS seats
FROM totals t
JOIN all_votes a USING (year)
JOIN mart.sejm_committees c USING (year, committee_id);
COMMENT ON MATERIALIZED VIEW mart.v_sejm_national_results IS 'Nationwide Sejm result per committee and year: votes, percent of valid votes, threshold (5% committee, 8% coalition, none for national minorities; 1991 had no threshold), whether it was passed, and seats won (1991-1997 including the 69 national-list seats). Use for "wynik PiS", "kto przekroczył próg", "ile mandatów".';
COMMENT ON COLUMN mart.v_sejm_national_results.pct IS 'Percent of all valid votes in the country (0-100).';

CREATE MATERIALIZED VIEW mart.v_sejm_list_results AS
WITH unit_votes AS (
  SELECT p.year, {GEO_PRE1999}, p.sejm_district_no, v.committee_id, v.votes
  FROM mart.sejm_precinct_list_votes v JOIN mart.precincts p USING (year, precinct_id) {PRE1999_JOIN}
), agg AS (
  SELECT year,
         CASE WHEN GROUPING(sejm_district_no) = 0 THEN 'sejm_district' ELSE {LEVEL_CASE} END AS level,
         {WOJEWODZTWO_CASE} AS wojewodztwo, powiat, teryt_powiat, gmina, teryt_gmina, sejm_district_no, committee_id,
         sum(votes) AS votes
  FROM unit_votes
  GROUP BY GROUPING SETS (
    (year, committee_id),
    (year, committee_id, wojewodztwo),
    (year, committee_id, wojewodztwo, teryt_powiat, powiat),
    (year, committee_id, wojewodztwo, teryt_powiat, powiat, teryt_gmina, gmina),
    (year, committee_id, wojewodztwo_1975),
    (year, committee_id, sejm_district_no)
  )
  HAVING {ROLLUP_HAVING}
)
SELECT a.year, a.level, a.wojewodztwo, a.powiat, a.teryt_powiat, a.gmina, a.teryt_gmina, a.sejm_district_no,
       a.committee_id, c.committee_name, c.aliases, a.votes,
       sum(a.votes) OVER w AS unit_valid_votes,
       round(100.0 * a.votes / NULLIF(sum(a.votes) OVER w, 0), 2) AS pct
FROM agg a JOIN mart.sejm_committees c USING (year, committee_id)
WINDOW w AS (PARTITION BY a.year, a.level, a.wojewodztwo, a.teryt_powiat, a.teryt_gmina, a.sejm_district_no);
COMMENT ON MATERIALIZED VIEW mart.v_sejm_list_results IS 'Sejm list votes and support in percent per territorial unit. Filter by level: ''country'', ''wojewodztwo'', ''powiat'', ''gmina'', ''wojewodztwo_1975'' or ''sejm_district''. Precincts abroad and on ships count only in country and their district. {PRE1999_UNITS_NOTE} Use for "w której gminie PiS miał najwyższe poparcie".';
COMMENT ON COLUMN mart.v_sejm_list_results.level IS 'Aggregation level of the row: country, wojewodztwo, powiat, gmina, wojewodztwo_1975 (1991-1997, name in wojewodztwo), sejm_district.';
COMMENT ON COLUMN mart.v_sejm_list_results.teryt_powiat IS '4-digit powiat TERYT (powiat names are not unique; prefer this or wojewodztwo+powiat).';
COMMENT ON COLUMN mart.v_sejm_list_results.unit_valid_votes IS 'Valid votes for all lists in the unit.';
COMMENT ON COLUMN mart.v_sejm_list_results.pct IS 'votes / unit_valid_votes in percent (0-100).';
CREATE INDEX ON mart.v_sejm_list_results (year, level, committee_id);
CREATE INDEX ON mart.v_sejm_list_results (teryt_gmina);

CREATE MATERIALIZED VIEW mart.v_sejm_turnout AS
SELECT year,
       CASE WHEN GROUPING(sejm_district_no) = 0 THEN 'sejm_district' ELSE {LEVEL_CASE} END AS level,
       {WOJEWODZTWO_CASE} AS wojewodztwo, powiat, teryt_powiat, gmina, teryt_gmina, sejm_district_no,
       count(*) AS precincts,
       sum(eligible_voters) AS eligible_voters,
       sum(ballots_issued) AS ballots_issued,
       sum(valid_ballots) AS valid_ballots,
       {_turnout_basis_sql("'sejm'", "year")} AS turnout_basis,
       {_turnout_pct_sql(_turnout_basis_sql("'sejm'", "year"), "sum(ballots_issued)", "sum(valid_ballots)", "sum(eligible_voters)")} AS turnout_pct
FROM (
  SELECT p.year, {GEO_PRE1999}, p.sejm_district_no, r.eligible_voters, r.ballots_issued, r.valid_ballots
  FROM mart.precincts p JOIN mart.sejm_precinct_protocols r USING (year, precinct_id) {PRE1999_JOIN}
) s
GROUP BY GROUPING SETS (
  (year),
  (year, wojewodztwo),
  (year, wojewodztwo, teryt_powiat, powiat),
  (year, wojewodztwo, teryt_powiat, powiat, teryt_gmina, gmina),
  (year, wojewodztwo_1975),
  (year, sejm_district_no)
)
HAVING {ROLLUP_HAVING};
COMMENT ON MATERIALIZED VIEW mart.v_sejm_turnout IS 'Official Sejm turnout per territorial unit, computed as PKW did (see turnout_basis); level as in v_sejm_list_results. For single precincts use mart.sejm_precinct_protocols. For other elections use mart.v_turnout.';
COMMENT ON COLUMN mart.v_sejm_turnout.turnout_basis IS '{TURNOUT_BASIS_COMMENT}';
COMMENT ON COLUMN mart.v_sejm_turnout.turnout_pct IS 'Official turnout in percent (0-100): turnout_basis / eligible_voters.';
"""


def _results_views_sql() -> str:
    """DDL of the per-election result views (president, EP, referendum, sejmik, councils)."""
    prez_source = f"""
        SELECT p.year, p.round, {GEO_PRE1999}, v.candidate_id, v.votes
        FROM mart.prezydent_precinct_candidate_votes v
        JOIN mart.prezydent_precinct_protocols p USING (year, round, precinct_id) {PRE1999_JOIN}"""
    ref_source = f"""
        SELECT p.year, a.question_no, {GEO_PRE1999}, p.eligible_voters,
               coalesce(a.valid_ballots, p.valid_ballots) AS valid_ballots, a.valid_votes, a.invalid_votes, a.yes_votes, a.no_votes
        FROM mart.referendum_precinct_answers a JOIN mart.referendum_precinct_protocols p USING (year, precinct_id) {PRE1999_JOIN}"""
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
COMMENT ON MATERIALIZED VIEW mart.v_prezydent_results IS 'Presidential votes and percent per candidate, round and territorial unit, summed from precincts. Official national totals (including a few unpublished military precincts) are in mart.prezydent_candidates; use this view for gminas, powiats and voivodeships ("gdzie Duda miał najwyższe poparcie"). {PRE1999_UNITS_NOTE}';
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
COMMENT ON MATERIALIZED VIEW mart.v_referendum_results IS 'Referendum answers per question and territorial unit: turnout, yes/no votes and their percent of valid votes. Binding only when national turnout is above 50%. 1996: turnout per ballot paper (question 1 vs questions 2-5). {PRE1999_UNITS_NOTE}';
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
""" + RADA_VIEW_SQL


# Council results: one row per committee and council, plus voivodeship and country totals per
# council level. List votes unknown in a few districts (struck-off candidates, see build_rady) are
# replaced by the candidates' votes and flagged with votes_exact = false.
RADA_VIEW_SQL = r"""
CREATE MATERIALIZED VIEW mart.v_rada_results AS
WITH lists AS (
  SELECT l.year, l.council_level, d.wojewodztwo, l.teryt, l.committee_id, l.candidates, l.seats,
         coalesce(l.votes, l.candidate_votes) AS votes, l.votes IS NULL AND l.candidate_votes IS NOT NULL AS unknown
  FROM mart.rada_district_lists l JOIN mart.rada_districts d USING (year, council_level, teryt, district_no)
), agg AS MATERIALIZED (
  SELECT year, council_level,
         CASE WHEN GROUPING(teryt) = 0 THEN 'council' WHEN GROUPING(wojewodztwo) = 0 THEN 'wojewodztwo' ELSE 'country' END AS level,
         wojewodztwo, teryt, committee_id, sum(candidates) AS candidates, sum(seats) AS seats, sum(votes) AS votes,
         bool_or(unknown) AS incomplete
  FROM lists
  GROUP BY GROUPING SETS ((year, council_level, committee_id), (year, council_level, committee_id, wojewodztwo),
                          (year, council_level, committee_id, wojewodztwo, teryt))
), units AS MATERIALIZED (
  SELECT year, council_level,
         CASE WHEN GROUPING(teryt) = 0 THEN 'council' WHEN GROUPING(wojewodztwo) = 0 THEN 'wojewodztwo' ELSE 'country' END AS level,
         wojewodztwo, teryt, min(unit_name) AS unit_name, min(council_name) AS council_name, count(DISTINCT teryt) AS councils,
         sum(seats) AS seats, sum(valid_votes) AS valid_votes
  FROM mart.rada_districts
  GROUP BY GROUPING SETS ((year, council_level), (year, council_level, wojewodztwo), (year, council_level, wojewodztwo, teryt))
)
SELECT a.year, a.council_level, a.level, a.wojewodztwo, a.teryt,
       CASE WHEN a.level = 'council' THEN u.unit_name END AS unit_name,
       CASE WHEN a.level = 'council' THEN u.council_name END AS council_name,
       u.councils, a.committee_id, c.committee_name, c.aliases, CAST(a.candidates AS INT) AS candidates,
       CAST(a.seats AS INT) AS seats, CAST(u.seats AS INT) AS unit_seats, a.votes, NOT a.incomplete AS votes_exact,
       u.valid_votes AS unit_valid_votes, round(100.0 * a.votes / NULLIF(u.valid_votes, 0), 2) AS pct
FROM agg a
JOIN units u ON u.year = a.year AND u.council_level = a.council_level AND u.level = a.level
            AND coalesce(u.wojewodztwo, '') = coalesce(a.wojewodztwo, '') AND coalesce(u.teryt, '') = coalesce(a.teryt, '')
JOIN mart.rada_committees c ON c.year = a.year AND c.committee_id = a.committee_id;
COMMENT ON MATERIALIZED VIEW mart.v_rada_results IS 'Council results (rady gmin/miast, rady powiatów, rady dzielnic m.st. Warszawy) per committee: seats, votes and percent of valid votes, per council (level ''council'', teryt = council unit), voivodeship and country, separately for each council_level. Use for "ile mandatów PiS zdobył w radach powiatów", "kto wygrał wybory do rady miasta Krakowa". Committees are matched by name, so at wojewodztwo/country level local committees with identical names are added up; compare parties by aliases.';
COMMENT ON COLUMN mart.v_rada_results.council_level IS 'gmina, powiat or dzielnica (as in mart.rada_districts).';
COMMENT ON COLUMN mart.v_rada_results.level IS 'Aggregation level: council (one council, identified by teryt), wojewodztwo or country.';
COMMENT ON COLUMN mart.v_rada_results.teryt IS 'Council unit (level council only): 6-digit gmina / Warsaw district TERYT or 4-digit powiat TERYT.';
COMMENT ON COLUMN mart.v_rada_results.councils IS 'Number of councils in the unit.';
COMMENT ON COLUMN mart.v_rada_results.seats IS 'Seats won by the committee.';
COMMENT ON COLUMN mart.v_rada_results.unit_seats IS 'All seats in the unit (council, voivodeship or country) at this council level.';
COMMENT ON COLUMN mart.v_rada_results.votes IS 'Votes for the committee''s lists (in majoritarian councils: for its candidates).';
COMMENT ON COLUMN mart.v_rada_results.votes_exact IS 'False when some district list totals are unknown (struck-off candidates, see docs) and their candidates'' votes were used instead, so votes is a slight lower bound.';
COMMENT ON COLUMN mart.v_rada_results.unit_valid_votes IS 'Valid votes in all districts of the unit where a vote took place.';
COMMENT ON COLUMN mart.v_rada_results.pct IS 'votes / unit_valid_votes in percent (0-100).';
CREATE INDEX ON mart.v_rada_results (year, council_level, level);
CREATE INDEX ON mart.v_rada_results (teryt);
"""


def _cross_election_views_sql() -> str:
    """DDL of v_turnout, v_party_results, v_party_change and v_gmina_continuity."""
    turnout_source = f"""
        SELECT 'sejm' AS election, p.year, 1 AS round, {GEO_PRE1999}, r.eligible_voters, r.ballots_issued, r.valid_ballots
        FROM mart.precincts p JOIN mart.sejm_precinct_protocols r USING (year, precinct_id) {PRE1999_JOIN}
        UNION ALL
        SELECT 'senat', p.year, 1, {GEO_PRE1999}, r.eligible_voters, r.ballots_issued, r.valid_ballots
        FROM mart.precincts p JOIN mart.senat_precinct_protocols r USING (year, precinct_id) {PRE1999_JOIN}
        UNION ALL
        SELECT 'prezydent', p.year, p.round, {GEO_PRE1999}, p.eligible_voters, p.ballots_issued, p.valid_ballots
        FROM mart.prezydent_precinct_protocols p {PRE1999_JOIN}
        UNION ALL
        SELECT 'pe', p.year, 1, {GEO}, p.eligible_voters, p.ballots_issued, p.valid_ballots FROM mart.pe_precinct_protocols p
        UNION ALL
        SELECT 'referendum', p.year, 1, {GEO_PRE1999}, p.eligible_voters, p.ballots_issued, p.valid_ballots
        FROM mart.referendum_precinct_protocols p {PRE1999_JOIN}
        WHERE p.year <> 1996
        UNION ALL
        SELECT 'sejmik', p.year, 1, {GEO}, p.eligible_voters, p.ballots_issued, p.valid_ballots FROM mart.sejmik_gmina_protocols p
        UNION ALL
        SELECT 'wbp', p.year, p.round, {GEO}, p.eligible_voters, p.ballots_issued, p.valid_ballots FROM mart.wbp_gmina_protocols p"""
    party = _party_family_sql("c.aliases")
    party_source = f"""
        SELECT 'sejm' AS election, p.year, {GEO_PRE1999}, {party} AS party, c.aliases[1] AS lead_alias, c.committee_name, v.votes
        FROM mart.sejm_precinct_list_votes v JOIN mart.precincts p USING (year, precinct_id) {PRE1999_JOIN}
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
    families = ", ".join(f"{alias}→{family}" for alias, (family, _) in PARTY_FAMILIES.items())
    family_rows = ", ".join(
        "(" + ", ".join("'" + v.replace("'", "''") + "'" for v in (alias, family, note)) + ")"
        for alias, (family, note) in PARTY_FAMILIES.items()
    )
    return f"""
CREATE MATERIALIZED VIEW mart.v_turnout AS
SELECT election, year, round, level, wojewodztwo, teryt_powiat, powiat, teryt_gmina, gmina,
       eligible_voters, ballots_issued, valid_ballots, {_turnout_basis_sql("election", "year")} AS turnout_basis,
       {_turnout_pct_sql(_turnout_basis_sql("election", "year"), "ballots_issued", "valid_ballots", "eligible_voters")} AS turnout_pct
FROM ({_rollup(turnout_source, "election, year, round", "sum(eligible_voters) AS eligible_voters, sum(ballots_issued) AS ballots_issued, sum(valid_ballots) AS valid_ballots")}) t;
COMMENT ON MATERIALIZED VIEW mart.v_turnout IS 'Official turnout (computed as PKW did, see turnout_basis) of every loaded election per territorial unit: election in (sejm, senat, prezydent, pe, referendum, sejmik, wbp), year, round. Compare turnout across elections by joining on level + teryt_gmina / teryt_powiat / wojewodztwo. Mayor (wbp) round 2 covers only gminas with a run-off, so its country row is not a national turnout. Referendum 1996 is missing: its two ballots had different turnouts, see mart.v_referendum_results per question. {PRE1999_UNITS_NOTE}';
COMMENT ON COLUMN mart.v_turnout.level IS '{LEVEL_COMMENT}';
COMMENT ON COLUMN mart.v_turnout.turnout_basis IS '{TURNOUT_BASIS_COMMENT}';
COMMENT ON COLUMN mart.v_turnout.turnout_pct IS 'Official turnout in percent (0-100): turnout_basis / eligible_voters.';
CREATE INDEX ON mart.v_turnout (election, year, round, level);
CREATE INDEX ON mart.v_turnout (teryt_gmina);

CREATE TABLE mart.party_families (alias TEXT PRIMARY KEY, family TEXT NOT NULL, note TEXT NOT NULL);
INSERT INTO mart.party_families VALUES {family_rows};
COMMENT ON TABLE mart.party_families IS 'Party families used by v_party_results / v_party_change: the first alias of a committee (lead alias) -> family, with a Polish note on the lineage (which committees and years). First aliases not listed are their own family; committees without aliases are ''other''. Use for "jakie komitety należą do rodziny X", "z czego powstała Konfederacja".';
COMMENT ON COLUMN mart.party_families.alias IS 'Lead alias of a committee (first element of *_committees.aliases), e.g. PO, KO, SLD, KNP.';
COMMENT ON COLUMN mart.party_families.family IS 'Party family in v_party_results.party.';
COMMENT ON COLUMN mart.party_families.note IS 'Polish note on the committee and its place in the family (years, coalition members).';
CREATE MATERIALIZED VIEW mart.v_party_results AS
WITH agg AS ({_rollup(party_source, "election, year, party", party_measures)})
SELECT a.election, a.year, a.level, a.wojewodztwo, a.teryt_powiat, a.powiat, a.teryt_gmina, a.gmina,
       CASE a.level WHEN 'gmina' THEN a.teryt_gmina WHEN 'powiat' THEN a.teryt_powiat
                    WHEN 'wojewodztwo' THEN a.wojewodztwo WHEN 'wojewodztwo_1975' THEN a.wojewodztwo
                    ELSE 'country' END AS unit_key,
       a.party, a.lead_aliases, a.committees, a.votes,
       sum(a.votes) OVER w AS unit_valid_votes,
       round(100.0 * a.votes / NULLIF(sum(a.votes) OVER w, 0), 2) AS pct
FROM agg a
WINDOW w AS ({UNIT_WINDOW.format(keys="a.election, a.year")});
COMMENT ON MATERIALIZED VIEW mart.v_party_results IS 'Party support across Sejm, European Parliament (pe) and voivodeship assembly (sejmik) elections per territorial unit, with committees grouped into party families so elections can be compared ("jak zmieniło się poparcie PiS w gminie X między 2019 a 2023", "PiS w sejmiku vs w Sejmie"). Families ({families}; otherwise the first alias; committees without aliases = ''other''). For the change against the previous election of the same kind use mart.v_party_change.';
COMMENT ON COLUMN mart.v_party_results.election IS 'sejm, pe or sejmik.';
COMMENT ON COLUMN mart.v_party_results.level IS '{LEVEL_COMMENT}';
COMMENT ON COLUMN mart.v_party_results.unit_key IS 'teryt_gmina, teryt_powiat, wojewodztwo (also for wojewodztwo_1975) or ''country'' depending on level; join key across elections (with level).';
COMMENT ON COLUMN mart.v_party_results.party IS 'Party family, e.g. PiS, KO, Lewica, TD/PSL, Konfederacja, Razem, KE, other.';
COMMENT ON COLUMN mart.v_party_results.lead_aliases IS 'First aliases of the committees in the family (e.g. ''PO'' in 2015, ''KO'' later); differing values mean a different committee or coalition.';
COMMENT ON COLUMN mart.v_party_results.pct IS 'votes / unit_valid_votes in percent (0-100).';
CREATE INDEX ON mart.v_party_results (election, year, level, party);
CREATE INDEX ON mart.v_party_results (unit_key);
-- Statistics before the self-join below; without them the planner picks a nested loop (minutes).
ANALYZE mart.v_party_results;

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
CREATE MATERIALIZED VIEW mart.v_person_history AS
SELECT p.person_id, p.full_name, p.gender, p.match_rule, x.year, x.chamber, x.round, x.district_no, x.area,
       x.committee_name, x.committee_aliases, x.list_no, x.list_position, x.votes, x.elected, x.age
FROM mart.persons p
JOIN (
  SELECT k.person_id, k.year, 'sejm' AS chamber, 1 AS round, k.district_no, NULL::text AS area, c.committee_name,
         c.aliases AS committee_aliases, k.list_no, k.list_position, k.votes::bigint AS votes, k.elected,
         k.age AS age
  FROM mart.sejm_candidates k JOIN mart.sejm_committees c USING (year, committee_id)
  UNION ALL
  SELECT s.person_id, s.year, 'senat', 1, s.district_no, NULL, s.committee_name,
         (SELECT c.aliases FROM mart.sejm_committees c WHERE c.year = s.year AND c.committee_name = s.committee_name),
         NULL, NULL, s.votes, s.elected, s.age
  FROM mart.senat_candidates s
  UNION ALL
  SELECT k.person_id, k.year, 'senat_uzup', 1, k.district_no, k.wojewodztwo, k.committee_name, NULL, NULL, NULL, k.votes,
         k.elected, NULL
  FROM mart.senat_byelections k
  UNION ALL
  SELECT k.person_id, k.year, 'prezydent', k.round, NULL, NULL, k.committee_name, NULL, NULL, NULL, k.votes, k.elected,
         NULL
  FROM mart.prezydent_candidates k
  UNION ALL
  SELECT k.person_id, k.year, 'pe', 1, k.district_no, NULL, c.committee_name, c.aliases, k.list_no, k.list_position,
         k.votes, k.elected, NULL
  FROM mart.pe_candidates k JOIN mart.pe_committees c USING (year, committee_id)
  UNION ALL
  SELECT k.person_id, k.year, 'sejmik', 1, k.district_no, k.wojewodztwo, c.committee_name, c.aliases, k.list_no,
         k.list_position, k.votes, k.elected, k.age
  FROM mart.sejmik_candidates k JOIN mart.sejmik_committees c USING (year, committee_id)
  WHERE k.person_id IS NOT NULL
  UNION ALL
  SELECT k.person_id, k.year, 'wbp', k.round, NULL, g.gmina, k.committee_name,
         (SELECT c.aliases FROM mart.sejmik_committees c WHERE c.year = k.year AND c.committee_name = k.committee_name),
         NULL, NULL, k.votes, k.elected, k.age
  FROM mart.wbp_candidates k
  LEFT JOIN mart.wbp_gmina_protocols g USING (year, round, teryt_gmina)
  WHERE k.person_id IS NOT NULL
  UNION ALL
  SELECT k.person_id, k.year,
         CASE k.council_level WHEN 'gmina' THEN 'rada_gminy' WHEN 'powiat' THEN 'rada_powiatu' ELSE 'rada_dzielnicy' END,
         1, k.district_no, coalesce(d.council_name, d.unit_name), c.committee_name, c.aliases, k.list_no, k.list_position,
         k.votes, k.elected, k.age
  FROM mart.rada_candidates k JOIN mart.rada_committees c USING (year, committee_id)
  LEFT JOIN mart.rada_districts d USING (year, council_level, teryt, district_no)
  WHERE k.person_id IS NOT NULL
) x USING (person_id);
-- Materialized because the union scans every candidate table; indexed for person and name lookups.
CREATE INDEX ON mart.v_person_history (person_id);
CREATE INDEX ON mart.v_person_history USING gin (full_name gin_trgm_ops);
COMMENT ON MATERIALIZED VIEW mart.v_person_history IS 'Every candidacy of a person (one row per candidacy and round): Sejm, Senate, President, European Parliament, sejmik, mayor and council candidacies of the same person (national persons, or local persons matched by name, gender, birth year and powiat). Use for "w których wyborach startował X", "kto zmienił komitet", "kto przeszedł z Sejmu do Senatu".';
COMMENT ON COLUMN mart.v_person_history.age IS 'Age on election day as published: local registers (sejmik, wbp, councils) and the Sejm/Senate registers of 1991, 1993 and 1997; NULL for other national candidacies.';
COMMENT ON COLUMN mart.v_person_history.chamber IS 'Election: sejm, senat, senat_uzup (Senate by-election, mart.senat_byelections), prezydent, pe, sejmik, wbp (mayor), rada_gminy, rada_powiatu or rada_dzielnicy (councils).';
COMMENT ON COLUMN mart.v_person_history.area IS 'sejmik and senat_uzup: voivodeship; wbp: gmina; councils: council name (2014: unit name). NULL otherwise.';
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
GROUP BY p.person_id, p.full_name, p.last_name, p.match_rule
-- Local persons are hundreds of thousands of councillor candidates; only elected mayors and sejmik
-- councillors are names worth resolving in questions (the rest are found by SQL on full_name).
HAVING p.match_rule NOT LIKE 'local:%' OR bool_or(h.elected AND h.chamber IN ('wbp', 'sejmik'))
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
    "v_gus_units",
    "v_person_history",
    "entity_lexicon",
    "v_sejm_national_results",
    "v_sejm_list_results",
    "v_sejm_turnout",
    "v_prezydent_results",
    "v_pe_national_results",
    "v_referendum_results",
    "v_sejmik_results",
    "v_rada_results",
    "v_turnout",
    "v_party_results",
    "v_party_change",
    "v_gmina_continuity",
)


# Divisor method of each election-year where it is not D'Hondt (mart.allocate_seats). 1991: Hare-Niemeyer
# in districts, also inside district blocs (art. 93 and 96 of the 1991 Sejm electoral law), modified
# Sainte-Laguë for the national list (art. 101); 1993/1997 D'Hondt in both (art. 110 and 118 of the 1993 law).
SEAT_METHODS = {
    ("sejm", 2001): "sainte_lague_modified",
    ("sejm", 1991): "hare_niemeyer",
    ("sejm national list", 1991): "sainte_lague_modified",
}

# Official seats per district recomputed from the list votes, in two stages: seats go by the
# election's method (SEAT_METHODS, else D'Hondt) to the lists and to the list blocs (2006,
# ``list_group``), then inside a bloc by Sainte-Laguë (Sejm: the election's method) to its lists. Thresholds:
# - Sejm: lists in seat allocation (``in_seat_allocation``, from PKW); 1991 district blocs (``list_group``)
#   had no threshold and split their seats proportionally unless the bloc's declaration said otherwise
#   (art. 75 ust. 2 of the 1991 law). The declarations are not published; in districts 21, 33, 34, 36 and
#   37 the KPN blocs gave seats to small partner lists (Polski Związek Zachodni, list 58; in 33 Sojusz
#   Kobiet Przeciw Trudnościom Życia, list 67), so there only the bloc's total is checked;
# - Sejm national lists 1991-1997 (district 0, election 'sejm national list'): 69 seats between the
#   committees whose national list qualified, by their votes in all districts. A 1991 national bloc is
#   one contender: how it split its seats between its lists is not published (list 41 took all 8 seats
#   of its bloc, which no divisor method reproduces), so only the bloc's total is checked;
# - sejmik and proportional councils: 5% of the voivodeship's / council's valid votes for a list;
#   2002 gmina councils outside cities with powiat rights and Warsaw district councils had none;
# - 2006 blocs: 15% (sejmik) or 10% (councils) for the bloc's lists together, counting members below
#   5%; a bloc takes part in a district only if one of its lists there passed 5%; seats inside the
#   bloc go to its members that passed 5%.
# Councils with unknown list totals (build_rady.STRUCK_OFF_DISTRICTS) and uncontested districts are
# skipped. European Parliament: seats between committees with 5% nationally by D'Hondt (unit
# 'country'), then a committee's seats between its district lists by Hare-Niemeyer (unit
# 'committee <id>').
SEAT_CHECK_SQL = """
WITH lists AS (
  SELECT 'sejm' AS election, l.year, '' AS unit, l.district_no, l.committee_id, l.list_group, l.votes,
         l.seats, d.seats AS district_seats, l.in_seat_allocation AS passed, 0::numeric AS group_threshold
  FROM mart.sejm_district_lists l JOIN mart.sejm_districts d USING (year, district_no)
  WHERE l.district_no > 0 AND (l.year, l.district_no) NOT IN ((1991, 21), (1991, 33), (1991, 34), (1991, 36), (1991, 37))
  UNION ALL
  -- 1991 districts where a bloc split its seats by its declaration (KPN blocs; see above): one row per bloc.
  SELECT 'sejm', l.year, '', l.district_no, min(l.committee_id), NULL::int, sum(l.votes)::bigint, sum(l.seats)::int,
         max(d.seats), bool_or(l.in_seat_allocation), NULL::numeric
  FROM mart.sejm_district_lists l JOIN mart.sejm_districts d USING (year, district_no)
  WHERE (l.year, l.district_no) IN ((1991, 21), (1991, 33), (1991, 34), (1991, 36), (1991, 37))
  GROUP BY l.year, l.district_no, coalesce(l.list_group, -l.committee_id)
  UNION ALL
  SELECT 'sejm national list', l.year, '', 0, min(l.committee_id), NULL::int, sum(v.votes)::bigint, sum(l.seats)::int, max(d.seats),
         bool_or(l.in_seat_allocation), NULL::numeric
  FROM mart.sejm_district_lists l JOIN mart.sejm_districts d USING (year, district_no)
  JOIN (SELECT year, committee_id, sum(votes) AS votes FROM mart.sejm_district_lists WHERE district_no > 0
        GROUP BY year, committee_id) v USING (year, committee_id)
  WHERE l.district_no = 0
  GROUP BY l.year, coalesce(l.list_group, -l.committee_id)
  UNION ALL
  SELECT 'sejmik', l.year, l.wojewodztwo, l.district_no, l.committee_id, l.list_group, l.votes, l.seats,
         sum(l.seats) OVER (PARTITION BY l.year, l.wojewodztwo, l.district_no),
         sum(l.votes) OVER (PARTITION BY l.year, l.wojewodztwo, l.committee_id)
           >= 0.05 * sum(l.votes) OVER (PARTITION BY l.year, l.wojewodztwo), 15
  FROM mart.sejmik_district_lists l
  UNION ALL
  SELECT 'rada', l.year, l.council_level || ' ' || l.teryt, l.district_no, l.committee_id, l.list_group, l.votes, l.seats,
         d.seats,
         (l.year = 2002 AND (l.council_level = 'dzielnica' OR (l.council_level = 'gmina' AND substr(l.teryt, 3, 2) < '61')))
         -- 1998: 5% only in powiat councils and cities with powiat rights; Rada m.st. Warszawy had none
         OR (l.year = 1998 AND (l.council_level = 'dzielnica'
                                OR (l.council_level = 'gmina' AND (substr(l.teryt, 3, 2) < '61' OR l.teryt = '146501'))))
         OR sum(l.votes) OVER (PARTITION BY l.year, l.council_level, l.teryt, l.committee_id)
            >= 0.05 * sum(l.votes) OVER (PARTITION BY l.year, l.council_level, l.teryt), 10
  FROM mart.rada_district_lists l JOIN mart.rada_districts d USING (year, council_level, teryt, district_no)
  WHERE d.voting_system = 'proportional' AND d.contested
    AND NOT EXISTS (SELECT 1 FROM mart.rada_district_lists u
                    WHERE (u.year, u.council_level, u.teryt) = (l.year, l.council_level, l.teryt) AND u.votes IS NULL)
), blocs AS (
  SELECT *, list_group IS NOT NULL
            AND 100 * sum(votes) OVER (PARTITION BY election, year, unit, list_group)
                >= group_threshold * sum(votes) OVER (PARTITION BY election, year, unit) AS in_bloc
  FROM lists
), contenders AS (
  SELECT election, year, unit, district_no, CASE WHEN in_bloc THEN 'B' || list_group ELSE 'L' || committee_id END AS contender,
         sum(votes) AS votes, max(district_seats) AS district_seats
  FROM blocs WHERE passed OR in_bloc
  GROUP BY 1, 2, 3, 4, 5 HAVING bool_or(passed)
), stage1 AS (
  SELECT election, year, unit, district_no, unnest(array_agg(contender ORDER BY contender)) AS contender,
         unnest(mart.allocate_seats(array_agg(votes ORDER BY contender), max(district_seats),
                                    CASE (election, year) {methods} ELSE 'dhondt' END)) AS seats
  FROM contenders GROUP BY 1, 2, 3, 4
), stage2 AS (
  SELECT b.election, b.year, b.unit, b.district_no, unnest(array_agg(b.committee_id ORDER BY b.committee_id)) AS committee_id,
         unnest(mart.allocate_seats(array_agg(b.votes ORDER BY b.committee_id), max(s.seats),
                                    CASE WHEN b.election IN ('sejm', 'sejm national list') THEN CASE (b.election, b.year) {methods} ELSE 'dhondt' END
                                         ELSE 'sainte_lague' END)) AS seats
  FROM blocs b JOIN stage1 s ON (s.election, s.year, s.unit, s.district_no) = (b.election, b.year, b.unit, b.district_no)
   AND s.contender = CASE WHEN b.in_bloc THEN 'B' || b.list_group ELSE 'L' || b.committee_id END
  WHERE b.passed
  GROUP BY 1, 2, 3, 4, s.contender
), keyed AS (
  SELECT b.*, CASE WHEN in_bloc THEN 'B' || list_group ELSE 'L' || committee_id END AS contender,
         sum(votes) OVER (PARTITION BY election, year, unit, district_no,
                          CASE WHEN in_bloc THEN 'B' || list_group ELSE 'L' || committee_id END) AS contender_votes
  FROM blocs b
), pe_committees AS (
  SELECT year, committee_id, sum(votes) AS votes, sum(seats) AS seats,
         sum(votes) >= 0.05 * sum(sum(votes)) OVER (PARTITION BY year) AS passed
  FROM mart.pe_district_lists GROUP BY year, committee_id
), pe_national AS (
  -- European Parliament: seats between committees with 5% nationally by D'Hondt ...
  SELECT year, unnest(array_agg(committee_id ORDER BY committee_id)) AS committee_id,
         unnest(mart.allocate_seats(array_agg(votes ORDER BY committee_id), sum(seats)::bigint, 'dhondt')) AS seats
  FROM pe_committees WHERE passed GROUP BY year
), pe_districts AS (
  -- ... then a committee's seats between its district lists by Hare-Niemeyer.
  SELECT l.year, l.committee_id, unnest(array_agg(l.district_no ORDER BY l.district_no)) AS district_no,
         unnest(mart.allocate_seats(array_agg(l.votes ORDER BY l.district_no), max(n.seats), 'hare_niemeyer')) AS seats
  FROM mart.pe_district_lists l JOIN pe_national n USING (year, committee_id)
  GROUP BY l.year, l.committee_id
), allocated AS (
  SELECT k.election, k.year, k.unit, k.district_no, array_agg(k.votes ORDER BY k.committee_id) AS votes,
         array_agg(k.contender ORDER BY k.committee_id) AS contenders,
         array_agg(k.contender_votes ORDER BY k.committee_id) AS contender_votes,
         array_agg(k.seats ORDER BY k.committee_id) AS official, array_agg(coalesce(s.seats, 0) ORDER BY k.committee_id) AS computed
  FROM keyed k LEFT JOIN stage2 s USING (election, year, unit, district_no, committee_id)
  GROUP BY 1, 2, 3, 4
  UNION ALL
  SELECT 'pe', c.year, 'country', 0, array_agg(c.votes::bigint ORDER BY c.committee_id),
         array_agg(c.committee_id::text ORDER BY c.committee_id), array_agg(c.votes ORDER BY c.committee_id),
         array_agg(c.seats::int ORDER BY c.committee_id), array_agg(coalesce(n.seats, 0) ORDER BY c.committee_id)
  FROM pe_committees c LEFT JOIN pe_national n USING (year, committee_id)
  GROUP BY c.year
  UNION ALL
  SELECT 'pe', l.year, 'committee ' || l.committee_id, 0, array_agg(l.votes ORDER BY l.district_no),
         array_agg(l.district_no::text ORDER BY l.district_no), array_agg(l.votes ORDER BY l.district_no),
         array_agg(l.seats ORDER BY l.district_no), array_agg(coalesce(d.seats, 0) ORDER BY l.district_no)
  FROM mart.pe_district_lists l JOIN pe_national n USING (year, committee_id)
  LEFT JOIN pe_districts d USING (year, committee_id, district_no)
  GROUP BY l.year, l.committee_id
)
SELECT election, year, unit, district_no, official, computed,
       -- An exact tie for the last seat (equal votes of the competing lists or blocs; inside a bloc,
       -- equal list votes) is decided by data not loaded (precincts won, lot).
       (SELECT count(*) FILTER (WHERE official[i] <> computed[i]) >= 2 AND sum(official[i]) = sum(computed[i])
               AND count(DISTINCT contender_votes[i]) FILTER (WHERE official[i] <> computed[i]) = 1
               AND (count(DISTINCT contenders[i]) FILTER (WHERE official[i] <> computed[i]) > 1
                    OR count(DISTINCT votes[i]) FILTER (WHERE official[i] <> computed[i]) = 1)
        FROM generate_subscripts(official, 1) AS i) AS tie
FROM allocated WHERE official <> computed
ORDER BY election, year, unit, district_no
"""


# National seats per committee under each divisor method, with the official thresholds and district
# magnitudes (Sejm: lists in seat allocation; sejmik: 5% of the voivodeship's valid votes).
GUS_VIEW_SQL = r"""
CREATE MATERIALIZED VIEW mart.v_gus_units AS
SELECT u.year, u.level, u.unit_key, u.teryt, u.name, u.wojewodztwo, u.teryt_powiat, u.gmina_type,
       max(i.value) FILTER (WHERE i.indicator = 'population')::bigint AS population,
       max(i.value) FILTER (WHERE i.indicator = 'population_urban')::bigint AS population_urban,
       max(i.value) FILTER (WHERE i.indicator = 'population_rural')::bigint AS population_rural,
       round(100 * max(i.value) FILTER (WHERE i.indicator = 'population_urban')
             / nullif(max(i.value) FILTER (WHERE i.indicator = 'population'), 0), 2) AS urban_pct,
       round(max(i.value) FILTER (WHERE i.indicator = 'area_ha') / 100, 2) AS area_km2,
       max(i.value) FILTER (WHERE i.indicator = 'population_density') AS density,
       max(i.value) FILTER (WHERE i.indicator = 'unemployed_registered')::bigint AS unemployed_registered,
       max(i.value) FILTER (WHERE i.indicator = 'unemployed_share_working_age') AS unemployed_share_working_age,
       max(i.value) FILTER (WHERE i.indicator = 'unemployment_rate') AS unemployment_rate,
       max(i.value) FILTER (WHERE i.indicator = 'avg_gross_salary') AS avg_salary
FROM mart.gus_units u
LEFT JOIN mart.gus_indicators i USING (year, level, unit_key)
GROUP BY u.year, u.level, u.unit_key, u.teryt, u.name, u.wojewodztwo, u.teryt_powiat, u.gmina_type;
COMMENT ON MATERIALIZED VIEW mart.v_gus_units IS 'Official GUS statistics (Bank Danych Lokalnych, 1995-2024), one row per territorial unit and year: the wide form of mart.gus_indicators (population, urban share, area, density, unemployment, salary). Not election data: join v_turnout, v_party_results or result views on level + unit_key, or on teryt = teryt_gmina / teryt_powiat, using the GUS year closest to the election (statistics refer to 31 December: October 2023 -> 2023, May 2025 -> 2024). At gmina level Warsaw is both the city (146501) and its districts (1465xx): do not sum both. Before 2002 Warsaw''s gminas are 1431xx.';
COMMENT ON COLUMN mart.v_gus_units.level IS 'country, wojewodztwo, powiat or gmina (gmina level also holds Warsaw''s districts).';
COMMENT ON COLUMN mart.v_gus_units.unit_key IS 'Join key like v_party_results.unit_key: gmina or powiat TERYT, voivodeship name, or country.';
COMMENT ON COLUMN mart.v_gus_units.teryt IS 'TERYT of that year: 6 digits for gminas and Warsaw districts, 4 for powiats, 2 for voivodeships; NULL for Poland.';
COMMENT ON COLUMN mart.v_gus_units.gmina_type IS 'miejska, wiejska, miejsko-wiejska or dzielnica Warszawy (gmina level only); use for "gminy wiejskie / miejskie".';
COMMENT ON COLUMN mart.v_gus_units.population IS 'Residents on 31 December.';
COMMENT ON COLUMN mart.v_gus_units.urban_pct IS 'population_urban / population in percent (0-100).';
COMMENT ON COLUMN mart.v_gus_units.area_km2 IS 'Area in km2 (GUS publishes hectares; NULL for Warsaw districts in 2006-2007).';
COMMENT ON COLUMN mart.v_gus_units.density IS 'Persons per km2 as published by GUS (from 2002).';
COMMENT ON COLUMN mart.v_gus_units.unemployed_registered IS 'Registered unemployed on 31 December (from 2003; not published for Warsaw districts).';
COMMENT ON COLUMN mart.v_gus_units.unemployed_share_working_age IS 'Registered unemployed as percent of the working-age population (from 2003; gminas too, not Warsaw districts).';
COMMENT ON COLUMN mart.v_gus_units.unemployment_rate IS 'Registered unemployment rate in percent (from 2004); powiat level and above only, NULL for gminas.';
COMMENT ON COLUMN mart.v_gus_units.avg_salary IS 'Average monthly gross salary in PLN, firms with more than 9 employees (from 2002); powiat level and above only.';
CREATE INDEX ON mart.v_gus_units (teryt, year);
CREATE INDEX ON mart.v_gus_units (year, level);
"""


def _precinct_results_sql() -> str:
    """Plain view: votes per list or candidate in every precinct (Sejm, president, EP) with official turnout."""
    basis = _turnout_basis_sql("election", "year")
    return f"""
CREATE VIEW mart.v_precinct_results AS
SELECT election, year, round, precinct_id, teryt_gmina, teryt_gmina_pre1999, gmina, powiat, wojewodztwo,
       precinct_no, address, eligible_voters, ballots_issued, valid_ballots, valid_votes, {basis} AS turnout_basis,
       {_turnout_pct_sql(basis, "ballots_issued", "valid_ballots", "eligible_voters")} AS turnout_pct,
       name, short_name, votes, round(100.0 * votes / NULLIF(valid_votes, 0), 2) AS pct
FROM (
  SELECT 'sejm' AS election, p.year, 1 AS round, p.precinct_id, p.teryt_gmina, p.teryt_gmina_pre1999, p.gmina, p.powiat, p.wojewodztwo,
         p.precinct_no, p.address, r.eligible_voters, r.ballots_issued, r.valid_ballots, r.valid_votes,
         c.committee_name AS name, coalesce(c.aliases[1], c.committee_name) AS short_name, v.votes
  FROM mart.precincts p JOIN mart.sejm_precinct_protocols r USING (year, precinct_id)
  JOIN mart.sejm_precinct_list_votes v USING (year, precinct_id) JOIN mart.sejm_committees c USING (year, committee_id)
  UNION ALL
  SELECT 'prezydent', r.year, r.round, r.precinct_id, r.teryt_gmina, r.teryt_gmina_pre1999, r.gmina, r.powiat, r.wojewodztwo,
         r.precinct_no, r.address, r.eligible_voters, r.ballots_issued, r.valid_ballots, r.valid_votes, c.full_name, c.full_name, v.votes
  FROM mart.prezydent_precinct_protocols r JOIN mart.prezydent_precinct_candidate_votes v USING (year, round, precinct_id)
  JOIN mart.prezydent_candidates c USING (year, round, candidate_id)
  UNION ALL
  SELECT 'pe', r.year, 1, r.precinct_id, r.teryt_gmina, NULL, r.gmina, r.powiat, r.wojewodztwo,
         r.precinct_no, r.address, r.eligible_voters, r.ballots_issued, r.valid_ballots, r.valid_votes, c.committee_name, coalesce(c.aliases[1], c.committee_name), v.votes
  FROM mart.pe_precinct_protocols r JOIN mart.pe_precinct_list_votes v USING (year, precinct_id)
  JOIN mart.pe_committees c USING (year, committee_id)
) x;
COMMENT ON VIEW mart.v_precinct_results IS 'Votes of every list (Sejm, European Parliament) or candidate (president, both rounds) in one polling station (obwód głosowania), with the precinct''s protocol counts and official turnout. One row per election, year, round, precinct and list or candidate. Always filter by election, year and teryt_gmina (or precinct_id): the view is not materialized. teryt_gmina is the gmina code of that year (NULL abroad and on ships); Sejm 1991-1997 and president 1990/1995 precincts have only teryt_gmina_pre1999 (join mart.gmina_codes_pre1999 for today''s units). European Parliament 2004 and 2009 were published per gmina: one row per gmina with precinct_no 0. For territorial totals use mart.v_turnout and the result views instead of summing precincts.';
COMMENT ON COLUMN mart.v_precinct_results.precinct_no IS 'Precinct number within its gmina (numer obwodu), as on the protocol.';
COMMENT ON COLUMN mart.v_precinct_results.name IS 'Committee name (Sejm, EP) or candidate full name (president).';
COMMENT ON COLUMN mart.v_precinct_results.short_name IS 'Committee short name (its first alias, e.g. KO, PiS) or candidate full name.';
COMMENT ON COLUMN mart.v_precinct_results.pct IS 'votes / valid_votes of the precinct in percent (0-100).';
COMMENT ON COLUMN mart.v_precinct_results.turnout_basis IS '{TURNOUT_BASIS_COMMENT}';
COMMENT ON COLUMN mart.v_precinct_results.turnout_pct IS 'Official turnout of the precinct in percent (0-100): turnout_basis / eligible_voters.';
"""


CANDIDATE_GMINA_VOTES_SQL = r"""
CREATE VIEW mart.v_candidate_gmina_votes AS
SELECT election, year, candidate_id, person_id, full_name, district_no, wojewodztwo, left(teryt_gmina, 4) AS teryt_powiat,
       powiat, teryt_gmina, gmina, sum(votes) AS votes, sum(valid_votes) AS valid_votes,
       round(100.0 * sum(votes) / NULLIF(sum(valid_votes), 0), 2) AS pct
FROM (
  SELECT 'sejm' AS election, v.year, v.candidate_id, c.person_id, c.full_name, c.district_no, p.wojewodztwo, p.powiat,
         p.teryt_gmina, p.gmina, v.votes, r.valid_votes
  FROM mart.sejm_precinct_candidate_votes v JOIN mart.sejm_candidates c USING (year, candidate_id)
  JOIN mart.precincts p USING (year, precinct_id) JOIN mart.sejm_precinct_protocols r USING (year, precinct_id)
  UNION ALL
  SELECT 'senat', v.year, v.candidate_id, c.person_id, c.full_name, c.district_no, p.wojewodztwo, p.powiat,
         p.teryt_gmina, p.gmina, v.votes, r.valid_votes
  FROM mart.senat_precinct_candidate_votes v JOIN mart.senat_candidates c USING (year, candidate_id)
  JOIN mart.precincts p USING (year, precinct_id) JOIN mart.senat_precinct_protocols r USING (year, precinct_id)
  UNION ALL
  SELECT 'pe', v.year, v.candidate_id, c.person_id, c.full_name, c.district_no, r.wojewodztwo, r.powiat,
         r.teryt_gmina, r.gmina, v.votes, r.valid_votes
  FROM mart.pe_precinct_candidate_votes v JOIN mart.pe_candidates c USING (year, candidate_id)
  JOIN mart.pe_precinct_protocols r USING (year, precinct_id)
) x
WHERE teryt_gmina IS NOT NULL
GROUP BY election, year, candidate_id, person_id, full_name, district_no, wojewodztwo, powiat, teryt_gmina, gmina;
COMMENT ON VIEW mart.v_candidate_gmina_votes IS 'Votes of one Sejm, Senate or European Parliament candidate per gmina, summed from the precinct files: one row per election, year, candidate and gmina of the candidate''s district. Not materialized: always filter by election, year and person_id or candidate_id given as a value or a scalar subquery (candidate_id = (SELECT candidate_id FROM mart.sejm_candidates WHERE ...)); a join on names aggregates every candidate and times out. Years with precinct candidate results: Sejm 2007-2023, Senate 2001-2023 (1990s precincts carry no current gmina code), EP 2014-2024. Abroad and ships are left out, so the gmina rows of a candidate may sum to less than the candidate''s total.';
COMMENT ON COLUMN mart.v_candidate_gmina_votes.valid_votes IS 'Valid votes cast in the gmina''s precincts of that district (for Sejm and EP: valid votes for all lists).';
COMMENT ON COLUMN mart.v_candidate_gmina_votes.pct IS 'votes / valid_votes in percent (0-100): the candidate''s share of all valid votes in the gmina.';
"""


SEAT_METHODS_VIEW_SQL = r"""
CREATE MATERIALIZED VIEW mart.v_seat_methods AS
WITH sejmik_share AS (
  SELECT year, wojewodztwo, committee_id,
         sum(votes) >= 0.05 * sum(sum(votes)) OVER (PARTITION BY year, wojewodztwo) AS passed
  FROM mart.sejmik_district_lists GROUP BY year, wojewodztwo, committee_id
), lists AS (
  SELECT 'sejm' AS election, l.year, l.district_no::text AS district, l.committee_id, l.votes, l.seats, d.seats AS district_seats
  FROM mart.sejm_district_lists l JOIN mart.sejm_districts d USING (year, district_no)
  WHERE l.in_seat_allocation AND l.year >= 2001  -- 1991-1997 also had a national list (district 0)
  UNION ALL
  SELECT 'sejmik', l.year, l.wojewodztwo || ' ' || l.district_no, l.committee_id, l.votes, l.seats,
         sum(l.seats) OVER (PARTITION BY l.year, l.wojewodztwo, l.district_no)
  FROM mart.sejmik_district_lists l JOIN sejmik_share s USING (year, wojewodztwo, committee_id)
  WHERE s.passed AND l.year <> 2006  -- 2006 list blocs are not modelled here
), districts AS (
  SELECT election, year, array_agg(committee_id ORDER BY committee_id) AS ids, array_agg(votes ORDER BY committee_id) AS votes,
         max(district_seats) AS seats
  FROM lists GROUP BY election, year, district
), allocated AS (
  SELECT election, year, unnest(ids) AS committee_id,
         unnest(mart.allocate_seats(votes, seats, 'dhondt')) AS dhondt,
         unnest(mart.allocate_seats(votes, seats, 'sainte_lague')) AS sainte_lague,
         unnest(mart.allocate_seats(votes, seats, 'sainte_lague_modified')) AS sainte_lague_modified
  FROM districts
), official AS (
  SELECT election, year, committee_id, sum(votes) AS votes, sum(seats) AS seats FROM lists GROUP BY election, year, committee_id
)
SELECT a.election, a.year, a.committee_id, coalesce(sc.committee_name, kc.committee_name) AS committee_name,
       coalesce(sc.aliases, kc.aliases) AS aliases, o.votes, CAST(o.seats AS INT) AS seats,
       CAST(sum(a.dhondt) AS INT) AS seats_dhondt, CAST(sum(a.sainte_lague) AS INT) AS seats_sainte_lague,
       CAST(sum(a.sainte_lague_modified) AS INT) AS seats_sainte_lague_modified
FROM allocated a
JOIN official o USING (election, year, committee_id)
LEFT JOIN mart.sejm_committees sc ON a.election = 'sejm' AND sc.year = a.year AND sc.committee_id = a.committee_id
LEFT JOIN mart.sejmik_committees kc ON a.election = 'sejmik' AND kc.year = a.year AND kc.committee_id = a.committee_id
GROUP BY a.election, a.year, a.committee_id, sc.committee_name, kc.committee_name, sc.aliases, kc.aliases, o.votes, o.seats;
COMMENT ON MATERIALIZED VIEW mart.v_seat_methods IS 'Seats per committee (whole country) in Sejm and sejmik (voivodeship assembly) elections under three divisor methods, with the official thresholds and district seat numbers: official seats, D''Hondt, Sainte-Laguë and modified Sainte-Laguë (first divisor 1.4). Use for "ile mandatów miałby PiS metodą Sainte-Laguë". Only committees that took part in seat allocation; Sejm before 2001 (national lists) and sejmik 2006 (list blocs) are not included. For other thresholds or units call mart.allocate_seats(votes_array, seats, method) per district.';
COMMENT ON COLUMN mart.v_seat_methods.election IS 'sejm or sejmik.';
COMMENT ON COLUMN mart.v_seat_methods.votes IS 'Votes of the committee in the districts where it took part in seat allocation.';
COMMENT ON COLUMN mart.v_seat_methods.seats IS 'Official seats.';
COMMENT ON COLUMN mart.v_seat_methods.seats_dhondt IS 'Seats under D''Hondt (the official method for Sejm since 2005 and for sejmiki since 2002); ties for the last seat are broken by votes, so a few districts may differ from the official seats.';
COMMENT ON COLUMN mart.v_seat_methods.seats_sainte_lague IS 'Seats under Sainte-Laguë (divisors 1, 3, 5, ...).';
COMMENT ON COLUMN mart.v_seat_methods.seats_sainte_lague_modified IS 'Seats under modified Sainte-Laguë (divisors 1.4, 3, 5, ...; the official method of Sejm 2001).';
"""


def check_seat_allocation(pg: psycopg.Connection) -> None:
    """Recompute official district seats with mart.allocate_seats; raise unless they match (ties aside)."""
    methods = " ".join(f"WHEN ('{e}'::text, {y}) THEN '{m}'" for (e, y), m in SEAT_METHODS.items())
    rows = pg.execute(SEAT_CHECK_SQL.format(methods=methods)).fetchall()
    districts = pg.execute(
        "SELECT (SELECT count(*) FROM mart.sejm_districts), (SELECT count(DISTINCT (year, wojewodztwo, district_no))"
        " FROM mart.sejmik_district_lists), (SELECT count(*) FROM mart.rada_districts WHERE voting_system = 'proportional'),"
        " (SELECT count(DISTINCT year) FROM mart.pe_district_lists)"
    ).fetchone()
    ties = [r for r in rows if r[6]]
    for election, year, unit, district_no, official, computed, _ in ties:
        log.info("seat check: tie for the last seat in %s %s %s district %s (official %s, recomputed %s)",
                 election, year, unit, district_no, official, computed)
    errors = [r for r in rows if not r[6]]
    log.info("seat check: sejm %s, sejmik %s, proportional council districts %s, EP elections %s; %s ties, %s mismatches",
             *districts, len(ties), len(errors))
    if errors:
        raise ValueError(f"official seats differ from the recomputed allocation in {len(errors)} districts: {errors[:5]}")


# Senate by-elections from PKW's result announcements (scripts/scrape_senat_byelections.py).
SENAT_BYELECTIONS = Path(__file__).resolve().parents[3] / "data" / "pkw_html" / "senat_byelections.csv"

SENAT_BYELECTIONS_SQL = r"""
-- Built before persons are linked; views over it are rebuilt afterwards.
DROP TABLE IF EXISTS mart.senat_byelections CASCADE;
CREATE TABLE mart.senat_byelections AS
SELECT (row_number() OVER (ORDER BY election_date, district_no, candidate_no))::int AS candidate_id, NULL::int AS person_id,
       election_date, extract(year FROM election_date)::int AS year, district_no, wojewodztwo, eligible_voters,
       ballots_issued, valid_votes, round(100.0 * ballots_issued / eligible_voters, 2) AS turnout_pct,
       candidate_no, first_names || ' ' || last_name AS full_name, last_name, first_names, committee_name, votes,
       round(100.0 * votes / NULLIF(valid_votes, 0), 2) AS pct, elected, source_url
FROM senat_byelection_src;
COMMENT ON TABLE mart.senat_byelections IS 'Senate by-elections (wybory uzupełniające do Senatu, 2003-2025): one vacant seat filled in one district between general elections, one row per candidate. District totals repeat on every row of a by-election. Not part of senat_candidates or v_turnout; the district numbers are those of the term (1-40 until 2011, then 1-100). Source: PKW result announcements (source_url).';
COMMENT ON COLUMN mart.senat_byelections.candidate_id IS 'Candidacy id, unique in the table.';
COMMENT ON COLUMN mart.senat_byelections.person_id IS 'mart.persons id (same matching as other national candidacies; v_person_history chamber ''senat_uzup'').';
COMMENT ON COLUMN mart.senat_byelections.election_date IS 'Voting day; a by-election is identified by (election_date, district_no).';
COMMENT ON COLUMN mart.senat_byelections.year IS 'Year of election_date (the term began earlier).';
COMMENT ON COLUMN mart.senat_byelections.wojewodztwo IS 'Voivodeship of the district, e.g. ''śląskie''.';
COMMENT ON COLUMN mart.senat_byelections.ballots_issued IS 'Voters who were issued ballots (in person and by post).';
COMMENT ON COLUMN mart.senat_byelections.turnout_pct IS 'Turnout in percent (0-100): ballots_issued / eligible_voters. 2010 by-elections were held together with the presidential election.';
COMMENT ON COLUMN mart.senat_byelections.candidate_no IS 'Ballot order of the candidate (alphabetical).';
COMMENT ON COLUMN mart.senat_byelections.full_name IS 'Given names and surname in capitals, e.g. ''Anna Maria ANDERS'' (as in senat_candidates).';
COMMENT ON COLUMN mart.senat_byelections.committee_name IS 'Nominating committee as written in the announcement.';
COMMENT ON COLUMN mart.senat_byelections.pct IS 'votes / valid_votes in percent (0-100).';
COMMENT ON COLUMN mart.senat_byelections.elected IS 'The winner (most votes); 2010 district 21 had a single candidate.';
"""


def load_senat_byelections(pg: psycopg.Connection, path: Path = SENAT_BYELECTIONS) -> int:
    """Create ``mart.senat_byelections`` from ``path``; return its number of by-elections.

    Raises ValueError when a district's candidate votes do not sum to its valid votes, or when it has
    no single winner with the most votes (a mistyped number would otherwise go unnoticed).
    """
    with path.open(encoding="utf-8", newline="") as fh:
        rows = [
            (r["election_date"], int(r["district_no"]), r["wojewodztwo"], int(r["eligible_voters"]),
             int(r["ballots_issued"]), int(r["valid_votes"]), int(r["candidate_no"]), r["last_name"], r["first_names"],
             r["committee_name"] or None, int(r["votes"]), r["elected"] == "True", r["source_url"])
            for r in csv.DictReader(fh)
        ]
    pg.execute(
        "CREATE TEMP TABLE senat_byelection_src (election_date DATE, district_no INT, wojewodztwo TEXT, eligible_voters INT,"
        " ballots_issued INT, valid_votes INT, candidate_no INT, last_name TEXT, first_names TEXT, committee_name TEXT,"
        " votes INT, elected BOOLEAN, source_url TEXT) ON COMMIT DROP"
    )
    with pg.cursor() as cur:
        cur.executemany("INSERT INTO senat_byelection_src VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)", rows)
    errors = pg.execute(
        """SELECT election_date, district_no FROM senat_byelection_src GROUP BY 1, 2
           HAVING sum(votes) <> min(valid_votes) OR count(*) FILTER (WHERE elected) <> 1
               OR max(votes) <> max(votes) FILTER (WHERE elected)"""
    ).fetchall()
    if errors:
        raise ValueError(f"Senate by-elections whose votes or winner do not reconcile: {errors}")
    pg.execute(SENAT_BYELECTIONS_SQL)
    return pg.execute("SELECT count(DISTINCT (election_date, district_no)) FROM mart.senat_byelections").fetchone()[0]


def rebuild(pg: psycopg.Connection) -> None:
    """Rebuild persons, views and the entity lexicon from everything loaded (caller's transaction)."""
    log.info("Senate by-elections: %s", load_senat_byelections(pg))
    link_persons(pg)
    persons = pg.execute(
        "SELECT count(*) FILTER (WHERE match_rule NOT LIKE 'local:%'), count(*) FILTER (WHERE match_rule LIKE 'local:%')"
        " FROM mart.persons"
    ).fetchone()
    log.info("persons: %s national, %s local", *persons)
    pg.execute(DROP_SQL)
    log.info("pre-1999 gmina codes: %s", load_gmina_codes_pre1999(pg))
    pg.execute(SEJM_VIEWS_SQL)
    pg.execute(_results_views_sql())
    pg.execute(_cross_election_views_sql())
    pg.execute(PERSON_HISTORY_SQL)
    pg.execute(SEAT_METHODS_VIEW_SQL)
    pg.execute(_precinct_results_sql())
    pg.execute(CANDIDATE_GMINA_VOTES_SQL)
    pg.execute(GUS_VIEW_SQL)
    linked = pg.execute(
        "SELECT (SELECT count(*) FROM mart.sejmik_candidates WHERE person_id IS NOT NULL),"
        " (SELECT count(*) FROM mart.wbp_candidates WHERE person_id IS NOT NULL)"
    ).fetchone()
    log.info("local candidacies linked to persons: sejmik %s, wbp %s", *linked)
    log.info(
        "council candidacies linked to persons: %s",
        pg.execute("SELECT count(*) FROM mart.rada_candidates WHERE person_id IS NOT NULL").fetchone()[0],
    )
    pg.execute(LEXICON_SQL)
    log.info("entity lexicon: %s names", pg.execute("SELECT count(*) FROM mart.entity_lexicon").fetchone()[0])
    check_seat_allocation(pg)
    for relation in ANALYZED:
        pg.execute(f"ANALYZE mart.{relation}")
