from __future__ import annotations

import functools
import hashlib
import json
import logging
import pathlib
import re
import secrets
import time
from contextlib import asynccontextmanager
from datetime import date, datetime
from decimal import Decimal
from email.utils import parsedate_to_datetime
from typing import Any, Literal

import psycopg
from fastapi import Depends, FastAPI, Header, HTTPException, Path, Query, Request
from psycopg.conninfo import conninfo_to_dict
from pydantic import BaseModel, ConfigDict, Field

from api.services import answer, config, db, feedback_store, opensearch_store, router
from api.services.mart import derived as mart_derived
from api.services.mart import examples as sql_examples
from api.services.mart import safe_sql
from api.services.mart import schema as mart_schema
from api.services.rate_limit import make_limiter, make_token_budget

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("plujka.api")
ask_limiter = make_limiter(config.ASK_RATE_LIMIT_PER_MINUTE, config.REDIS_URL)
token_budget = make_token_budget(config.DAILY_TOKEN_LIMIT_PER_CLIENT, config.REDIS_URL)

OPENAPI_TAGS: list[dict[str, str]] = [
    {"name": "health", "description": "Liveness probe and approximate KBW table stats"},
    {"name": "catalog", "description": "KBW mirror file inventory (kbw_dane_files)"},
    {"name": "ask", "description": "Natural language → routed intent + SQL + rows"},
    {"name": "feedback", "description": "Thumbs up/down for answer quality"},
    {"name": "hints", "description": "OpenSearch suggestions over logged questions"},
    {"name": "persons", "description": "Person timeline: every linked candidacy of one person (fixed read-only SQL over mart)"},
    {"name": "browse", "description": "Results, turnout and territorial units without the LLM (fixed read-only SQL over mart)"},
]

# Fixed, parameterized queries for the person endpoints, run as the read-only mart role.
PERSON_SQL = """
SELECT person_id, full_name, last_name, first_names, gender, match_rule, candidacies
FROM mart.persons WHERE person_id = %(person_id)s
"""
# Councils are one election ('rada') in mart.elections; the date orders rounds and same-year elections.
PERSON_CANDIDACIES_SQL = """
SELECT h.year, e.election_date, h.chamber, h.round, h.district_no, h.area, h.committee_name, h.list_no,
       h.list_position, h.votes, h.elected, h.age
FROM mart.v_person_history h
LEFT JOIN mart.elections e
  ON e.election = CASE WHEN left(h.chamber, 5) = 'rada_' THEN 'rada' ELSE h.chamber END
 AND e.year = h.year AND e.round = h.round
WHERE h.person_id = %(person_id)s
ORDER BY e.election_date NULLS LAST, h.year, h.chamber, h.round, h.district_no, h.area
"""
# Every word of the query must start a word of the name, and one of them must start the surname.
# Searches entity_lexicon: national persons plus elected mayors and sejmik councillors among local persons.
PERSON_SEARCH_SQL = """
WITH q AS (SELECT array_agg(mart.fold_name(t)) AS toks FROM unnest(%(tokens)s::text[]) AS t)
SELECT p.person_id, p.full_name, p.gender, p.match_rule, p.candidacies, l.detail, l.years
FROM mart.entity_lexicon l
CROSS JOIN q
JOIN mart.persons p ON p.person_id = l.ref::int
WHERE l.kind = 'person'
  AND EXISTS (SELECT 1 FROM unnest(q.toks) AS t WHERE l.folded LIKE t || '%%')
  AND NOT EXISTS (SELECT 1 FROM unnest(q.toks) AS t WHERE ' ' || mart.fold_name(l.name) NOT LIKE '%% ' || t || '%%')
ORDER BY l.folded = ANY(q.toks) DESC, p.candidacies DESC, l.name, p.person_id
LIMIT %(limit)s
"""
PERSON_SEARCH_MAX_ROWS = 50

# Fixed queries for the browse endpoints (no LLM). Optional filters are NULL when not given.
ELECTIONS_SQL = """
SELECT election, year, round, election_date, description FROM mart.elections
ORDER BY election_date, election, round
"""
# Candidacies per chamber and year from v_person_history: candidates count once (round 1), women
# share among those with a published gender, median age where the files give it.
CANDIDATE_STATS_SQL = """
SELECT year,
       count(*) FILTER (WHERE round = 1) AS candidates,
       round(100.0 * count(*) FILTER (WHERE round = 1 AND gender = 'Kobieta')
             / NULLIF(count(gender) FILTER (WHERE round = 1), 0), 1) AS women_pct,
       count(*) FILTER (WHERE elected) AS elected,
       round(100.0 * count(*) FILTER (WHERE elected AND gender = 'Kobieta')
             / NULLIF(count(gender) FILTER (WHERE elected), 0), 1) AS elected_women_pct,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY age) FILTER (WHERE round = 1) AS median_age,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY age) FILTER (WHERE elected) AS elected_median_age
FROM mart.v_person_history
WHERE chamber = %(chamber)s
GROUP BY year ORDER BY year
"""
CANDIDATE_AGES_SQL = """
(SELECT 'youngest' AS end_, person_id, full_name, gender, age, area, committee_name, elected
 FROM mart.v_person_history
 WHERE chamber = %(chamber)s AND year = %(year)s AND round = 1 AND age IS NOT NULL AND (NOT %(elected)s OR elected)
 ORDER BY age, full_name LIMIT 10)
UNION ALL
(SELECT 'oldest', person_id, full_name, gender, age, area, committee_name, elected
 FROM mart.v_person_history
 WHERE chamber = %(chamber)s AND year = %(year)s AND round = 1 AND age IS NOT NULL AND (NOT %(elected)s OR elected)
 ORDER BY age DESC, full_name LIMIT 10)
"""
# Mayors (wójt / burmistrz / prezydent miasta) elected in regular elections since 2002; one elected
# candidacy per gmina and year (round 1 or the run-off). By-elections are not in the mart.
_MAYORS = """
  SELECT DISTINCT ON (c.year, c.teryt_gmina) c.year, c.round, c.teryt_gmina, g.gmina, g.wojewodztwo,
         c.person_id, c.full_name, c.committee_name, c.pct
  FROM mart.wbp_candidates c
  JOIN mart.wbp_gmina_protocols g ON g.year = c.year AND g.round = c.round AND g.teryt_gmina = c.teryt_gmina
  WHERE c.elected AND (%(wojewodztwo)s::text IS NULL OR g.wojewodztwo = %(wojewodztwo)s)
  ORDER BY c.year, c.teryt_gmina, c.round DESC
"""
MAYOR_TERMS_SQL = f"""
WITH m AS ({_MAYORS}), terms AS (
  SELECT person_id, (array_agg(full_name ORDER BY year DESC))[1] AS full_name, count(*) AS terms,
         array_agg(year ORDER BY year) AS years, array_agg(DISTINCT gmina) AS gminas,
         array_agg(DISTINCT wojewodztwo) AS wojewodztwa
  FROM m GROUP BY person_id
)
SELECT person_id, full_name, terms, years, gminas, wojewodztwa, count(*) OVER (PARTITION BY terms) AS persons_with_terms
FROM terms ORDER BY terms DESC, full_name
"""
MAYOR_GMINA_SQL = f"""
WITH m AS ({_MAYORS})
SELECT year, round, gmina, wojewodztwo, person_id, full_name, committee_name, pct,
       person_id IS NOT DISTINCT FROM lag(person_id) OVER (ORDER BY year) AS same_as_before
FROM m WHERE teryt_gmina = %(teryt_gmina)s ORDER BY year
"""
# Persons with at least one national candidacy (Sejm, Senate, EP, President), ranked over all their
# candidacies (national and local); with a chamber, only that election's candidacies count.
PERSON_RANKING_SQL = """
SELECT h.person_id, min(h.full_name) AS full_name, count(*) FILTER (WHERE h.round = 1) AS candidacies,
       count(*) FILTER (WHERE h.elected) AS elected, min(h.year) AS first_year, max(h.year) AS last_year,
       max(h.year) - min(h.year) AS career_years, sum(h.votes) AS votes,
       array_agg(DISTINCT h.chamber) AS chambers
FROM mart.v_person_history h JOIN mart.persons p USING (person_id)
WHERE (%(chamber)s::text IS NULL AND p.match_rule NOT LIKE 'local:%%') OR h.chamber = %(chamber)s
GROUP BY h.person_id
ORDER BY CASE %(by)s WHEN 'candidacies' THEN count(*) FILTER (WHERE h.round = 1)
                     WHEN 'elected' THEN count(*) FILTER (WHERE h.elected)
                     WHEN 'career' THEN max(h.year) - min(h.year)
                     ELSE coalesce(sum(h.votes), 0) END DESC, count(*) DESC, min(h.full_name)
LIMIT 50
"""
# Consecutive Sejm, Senate and EP candidacies of one person under committees of different party
# families (party_families; a lead alias not listed is its own family), e.g. PO -> PiS. Committees
# without an alias (small or one-off) are left out.
PARTY_SWITCH_SQL = """
WITH c AS (
  SELECT DISTINCT ON (h.person_id, h.year, h.chamber) h.person_id, h.full_name, h.year, h.chamber,
         coalesce(f.family, h.committee_aliases[1]) AS family
  FROM mart.v_person_history h LEFT JOIN mart.party_families f ON f.alias = h.committee_aliases[1]
  WHERE h.chamber IN ('sejm', 'senat', 'pe') AND h.year >= %(since)s AND h.committee_aliases[1] IS NOT NULL
  ORDER BY h.person_id, h.year, h.chamber
), s AS (
  SELECT person_id, full_name, year, family, lag(family) OVER w AS prev_family, lag(year) OVER w AS prev_year
  FROM c WINDOW w AS (PARTITION BY person_id ORDER BY year, chamber)
)
SELECT prev_family AS from_family, family AS to_family, count(DISTINCT person_id) AS persons,
       (array_agg(full_name || ' (' || prev_year || '→' || year || ')' ORDER BY year DESC, full_name))[1:5] AS examples
FROM s WHERE prev_family IS NOT NULL AND prev_family <> family
GROUP BY prev_family, family ORDER BY persons DESC, from_family, to_family LIMIT 50
"""
ELECTED_SQL = """
SELECT district_no, area, coalesce(committee_aliases[1], committee_name) AS party, committee_name, person_id, full_name,
       list_position, votes
FROM mart.v_person_history
WHERE chamber = %(chamber)s AND year = %(year)s AND elected
  AND (%(district_no)s::int IS NULL OR district_no = %(district_no)s)
  AND (%(party)s::text IS NULL OR committee_aliases[1] = %(party)s)
ORDER BY area NULLS FIRST, district_no, votes DESC NULLS LAST
"""
# Election card: fixed SQL per election for the number of precincts (sejmik, wbp: gminas) and of
# districts and seats. Keys are the allowed election kinds, never user text.
ELECTION_TURNOUT_SQL = """
SELECT eligible_voters, ballots_issued, valid_ballots, turnout_pct, turnout_basis
FROM mart.v_turnout WHERE election = %(election)s AND year = %(year)s AND round = %(round)s AND level = 'country'
"""
ELECTION_UNITS_SQL = {
    "sejm": "SELECT (SELECT count(*) FROM mart.sejm_precinct_protocols WHERE year = %(year)s) AS precincts,"
            " count(*) FILTER (WHERE district_no > 0) AS districts, sum(seats) AS seats FROM mart.sejm_districts WHERE year = %(year)s",
    "senat": "SELECT (SELECT count(*) FROM mart.senat_precinct_protocols WHERE year = %(year)s) AS precincts,"
             " count(DISTINCT district_no) AS districts, count(*) FILTER (WHERE elected) AS seats"
             " FROM mart.senat_candidates WHERE year = %(year)s",
    "pe": "SELECT (SELECT count(*) FROM mart.pe_precinct_protocols WHERE year = %(year)s) AS precincts,"
          " count(DISTINCT district_no) AS districts, sum(seats) AS seats FROM mart.pe_district_lists WHERE year = %(year)s",
    "prezydent": "SELECT count(*) AS precincts, NULL::bigint AS districts, NULL::bigint AS seats"
                 " FROM mart.prezydent_precinct_protocols WHERE year = %(year)s AND round = %(round)s",
    "referendum": "SELECT count(DISTINCT precinct_id) AS precincts, NULL::bigint AS districts, NULL::bigint AS seats"
                  " FROM mart.referendum_precinct_protocols WHERE year = %(year)s",
    "sejmik": "SELECT (SELECT count(*) FROM mart.sejmik_gmina_protocols WHERE year = %(year)s) AS gminas,"
              " count(DISTINCT (wojewodztwo, district_no)) AS districts, sum(seats) AS seats"
              " FROM mart.sejmik_district_lists WHERE year = %(year)s",
    "wbp": "SELECT count(*) AS gminas, NULL::bigint AS districts, count(*) AS seats"
           " FROM mart.wbp_gmina_protocols WHERE year = %(year)s AND round = %(round)s",
}
SEJM_THRESHOLDS_SQL = """
SELECT array_agg(DISTINCT threshold_pct ORDER BY threshold_pct) AS thresholds
FROM mart.v_sejm_national_results WHERE year = %(year)s AND threshold_pct IS NOT NULL
"""
SEAT_METHOD_LABELS = {"dhondt": "D'Hondt", "sainte_lague": "Sainte-Laguë", "sainte_lague_modified": "zmodyfikowana Sainte-Laguë",
                      "hare_niemeyer": "Hare-Niemeyer"}
DATA_VERSION_SQL = """
SELECT max(loaded_at) AS mart_loaded_at, count(*) AS elections, max(election_date) AS latest_election_date
FROM mart.elections
"""
SOURCE_MANIFEST = pathlib.Path(__file__).resolve().parents[1] / "data" / "source_manifest.json"
_UNIT_FILTER = """
  AND level = %(level)s
  AND (%(wojewodztwo)s::text IS NULL OR wojewodztwo = %(wojewodztwo)s)
  AND (%(teryt_powiat)s::text IS NULL OR teryt_powiat = %(teryt_powiat)s)
  AND (%(teryt_gmina)s::text IS NULL OR teryt_gmina = %(teryt_gmina)s)
"""
TURNOUT_SQL = f"""
SELECT election, year, round, level, wojewodztwo, teryt_powiat, powiat, teryt_gmina, gmina,
       eligible_voters, valid_ballots, turnout_pct
FROM mart.v_turnout
WHERE election = %(election)s AND year = %(year)s AND round = %(round)s {_UNIT_FILTER}
ORDER BY wojewodztwo, teryt_powiat, teryt_gmina
"""
# Party results (Sejm, sejmik, EP: v_party_results, lists grouped by lead alias) or candidates
# (president: v_prezydent_results), as one shape: the unit, name, votes, pct.
RESULTS_SQL = f"""
SELECT * FROM (
  SELECT election, year, 1 AS round, level, wojewodztwo, teryt_powiat, powiat, teryt_gmina, gmina,
         party AS name, votes, pct
  FROM mart.v_party_results
  WHERE election = %(election)s AND %(election)s <> 'prezydent'
  UNION ALL
  SELECT 'prezydent', year, round, level, wojewodztwo, teryt_powiat, powiat, teryt_gmina, gmina,
         full_name, votes, pct
  FROM mart.v_prezydent_results
  WHERE %(election)s = 'prezydent'
) r
WHERE year = %(year)s AND round = %(round)s {_UNIT_FILTER}
ORDER BY wojewodztwo, teryt_powiat, teryt_gmina, votes DESC
LIMIT 50000
"""
# Presidential run-off finalists per unit: their round 1 and round 2 votes and the gain between rounds
# (candidate ids differ between rounds, so rounds are matched by name).
RUNOFF_SQL = f"""
SELECT * FROM (
  SELECT 'prezydent' AS election, r2.year, r2.round, r2.level, r2.wojewodztwo, r2.teryt_powiat, r2.powiat,
         r2.teryt_gmina, r2.gmina, r2.full_name AS name, r1.votes AS votes_round1, r1.pct AS pct_round1,
         r2.votes, r2.pct, r2.votes - r1.votes AS gain_votes, r2.pct - r1.pct AS gain_pp
  FROM mart.v_prezydent_results r2
  JOIN mart.v_prezydent_results r1 ON r1.year = r2.year AND r1.round = 1 AND r1.full_name = r2.full_name
   AND r1.level = r2.level AND r1.wojewodztwo IS NOT DISTINCT FROM r2.wojewodztwo
   AND r1.teryt_powiat IS NOT DISTINCT FROM r2.teryt_powiat AND r1.teryt_gmina IS NOT DISTINCT FROM r2.teryt_gmina
  WHERE r2.year = %(year)s AND r2.round = 2
) r
WHERE true {_UNIT_FILTER}
ORDER BY wojewodztwo, teryt_powiat, teryt_gmina, votes DESC
LIMIT 50000
"""
# Change of each party against the previous election of the same kind (v_party_change), per unit.
CHANGE_SQL = f"""
SELECT election, year, 1 AS round, prev_year, level, wojewodztwo, teryt_powiat, powiat, teryt_gmina, gmina,
       party AS name, votes, prev_votes, pct, prev_pct, change_pp, comparable
FROM mart.v_party_change
WHERE election = %(election)s AND year = %(year)s {_UNIT_FILTER}
ORDER BY wojewodztwo, teryt_powiat, teryt_gmina, votes DESC
LIMIT 50000
"""
UNIT_SEARCH_SQL = """
SELECT DISTINCT ON (kind, ref) kind AS level, ref AS unit, regexp_replace(name, '^(gm\.|m\.|pow\.) ', '') AS name, detail
FROM mart.entity_lexicon
WHERE kind IN ('gmina', 'powiat', 'wojewodztwo') AND folded LIKE mart.fold_name(%(name)s) || '%%'
ORDER BY kind, ref, length(name)
LIMIT 20
"""
# One unit across every loaded election: turnout per election and round, and the winner.
UNIT_TURNOUT_SQL = f"""
SELECT election, year, round, coalesce(gmina, powiat, wojewodztwo) AS unit_name, turnout_pct
FROM mart.v_turnout WHERE true {_UNIT_FILTER}
ORDER BY year, election, round
"""
UNIT_WINNERS_SQL = f"""
SELECT DISTINCT ON (election, year, round) election, year, round, name, votes, pct FROM (
  SELECT election, year, 1 AS round, level, wojewodztwo, teryt_powiat, teryt_gmina, party AS name, votes, pct
  FROM mart.v_party_results
  UNION ALL
  SELECT 'prezydent', year, round, level, wojewodztwo, teryt_powiat, teryt_gmina, full_name, votes, pct
  FROM mart.v_prezydent_results
) r
WHERE true {_UNIT_FILTER}
ORDER BY election, year, round, votes DESC
"""
UNIT_GUS_SQL = """
SELECT year, name, population, urban_pct, area_km2, density, unemployed_share_working_age, unemployment_rate, avg_salary
FROM mart.v_gus_units WHERE level = %(level)s AND unit_key = %(unit)s
ORDER BY year DESC LIMIT 1
"""
# Sejm seats under another method, threshold or one national district (2001 on: 460 district seats).
# A committee passes with threshold % of the national valid list votes (coalition committees with
# coalition_threshold %); with exempt_minority, national-minority committees pass regardless.
# Committees listed in merge run as one coalition list (votes summed per district, coalition threshold).
SEAT_SIMULATION_SQL = """
WITH comm AS (
  SELECT committee_id, CASE WHEN committee_id::text = ANY(%(merge)s::text[]) THEN 'merged' ELSE committee_id::text END AS grp,
         committee_name, coalesce(aliases[1], committee_name) AS party, is_coalition, is_minority
  FROM mart.sejm_committees WHERE year = %(year)s
), groups AS (
  SELECT grp, string_agg(committee_name, ' + ' ORDER BY committee_id) AS committee_name,
         string_agg(party, ' + ' ORDER BY committee_id) AS party, array_agg(committee_id ORDER BY committee_id) AS committee_ids,
         bool_or(is_coalition) OR count(*) > 1 AS is_coalition, bool_and(is_minority) AS is_minority
  FROM comm GROUP BY grp
), lists AS (
  SELECT l.district_no, c.grp, l.votes, l.seats, d.seats AS district_seats
  FROM mart.sejm_district_lists l JOIN mart.sejm_districts d USING (year, district_no)
  JOIN comm c ON c.committee_id = l.committee_id
  WHERE l.year = %(year)s AND l.district_no > 0
), national AS (
  SELECT grp, sum(votes) AS votes, sum(seats) AS seats, 100.0 * sum(votes) / sum(sum(votes)) OVER () AS pct
  FROM lists GROUP BY grp
), passed AS (
  SELECT n.grp FROM national n JOIN groups g USING (grp)
  WHERE n.pct >= CASE WHEN g.is_coalition THEN %(coalition_threshold)s ELSE %(threshold)s END
     OR (%(exempt_minority)s AND g.is_minority)
), districts AS (
  SELECT CASE WHEN %(national)s THEN 0 ELSE district_no END AS district, grp, sum(votes) AS votes
  FROM lists JOIN passed USING (grp) GROUP BY 1, 2
), sizes AS (
  SELECT CASE WHEN %(national)s THEN 0 ELSE district_no END AS district, sum(district_seats) AS seats
  FROM (SELECT DISTINCT district_no, district_seats FROM lists) d GROUP BY 1
), allocated AS (
  SELECT unnest(grps) AS grp, unnest(mart.allocate_seats(v, seats, %(method)s)) AS seats
  FROM (SELECT d.district, s.seats, array_agg(d.grp ORDER BY d.grp) AS grps,
               array_agg(d.votes::numeric ORDER BY d.grp) AS v
        FROM districts d JOIN sizes s USING (district) GROUP BY d.district, s.seats) x
)
SELECT g.committee_name, g.party, g.committee_ids, n.votes, round(n.pct, 2) AS pct,
       n.seats AS seats_official, coalesce(sum(a.seats), 0) AS seats_simulated
FROM national n JOIN groups g USING (grp)
LEFT JOIN allocated a USING (grp)
GROUP BY g.committee_name, g.party, g.committee_ids, n.votes, n.pct, n.seats
ORDER BY seats_simulated DESC, n.votes DESC
"""
BROWSE_ELECTIONS = ("sejm", "senat", "prezydent", "pe", "referendum", "sejmik", "wbp")
# Letters only (hyphenated and apostrophe surnames kept), so LIKE wildcards never reach the query.
_NAME_TOKEN_RE = re.compile(r"[^\W\d_]+(?:[-'][^\W\d_]+)*")


def production_problems() -> list[str]:
    """Settings that are unsafe in production (default passwords shipped in docker-compose)."""
    problems = []
    if conninfo_to_dict(config.MART_DATABASE_URL).get("password") == config.MART_DEFAULT_READER_PASSWORD:
        problems.append("MART_DATABASE_URL uses the default reader password")
    if conninfo_to_dict(config.DATABASE_URL).get("password") == "plujka":
        problems.append("DATABASE_URL uses the default owner password")
    if config.ASK_RATE_LIMIT_PER_MINUTE <= 0:
        problems.append("ASK_RATE_LIMIT_PER_MINUTE is disabled")
    return problems


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Check production settings, initialize the DB schema and OpenSearch indexes once at startup."""
    problems = production_problems()
    if problems and config.PLUJKA_ENV == "production":
        raise RuntimeError("Refusing to start with unsafe production settings: " + "; ".join(problems))
    for problem in problems:
        log.warning("unsafe for production: %s", problem)
    db.init_database()
    opensearch_store.ensure_index()
    try:
        sql_examples.ensure_index()
    except Exception as exc:  # examples fall back to the curated file
        log.warning("sql examples index unavailable: %s", exc)
    yield


app = FastAPI(
    title="Plujka API",
    version="0.1.0",
    lifespan=lifespan,
    openapi_tags=OPENAPI_TAGS,
)


class EntityRef(BaseModel):
    """An explicit entity choice, e.g. an option picked from a clarification."""

    kind: str
    ref: str


class PreviousTurn(BaseModel):
    """The preceding question of a conversation, for follow-ups such as "a w 2019?"."""

    question: str = Field(max_length=1000, description="Previous question (may hold several earlier turns)")
    sql: str | None = Field(default=None, max_length=8000, description="SQL that answered it")
    entity_refs: list[EntityRef] | None = Field(default=None, description="Entity choices used for it")


class AskRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {"question": "Frekwencja w wyborach do Sejmu 2023 w województwach"},
                {
                    "question": "A w 2019?",
                    "previous": {"question": "Frekwencja w wyborach do Sejmu 2023 w województwach"},
                },
                {"question": "Wyniki Kwaśniewskiego w 1995", "entity_refs": [{"kind": "person", "ref": "1920"}]},
            ]
        }
    )

    question: str = Field(max_length=1000, description="Question in Polish")
    entity_refs: list[EntityRef] | None = Field(
        default=None, description="Entity choices from a previous clarification (kind + ref from its options)"
    )
    previous: PreviousTurn | None = Field(
        default=None, description="Previous turn when this question follows up on it; its filters carry over"
    )


class HealthResponse(BaseModel):
    """``GET /health`` — optional ``kbw_stats`` when ``details=true``."""

    status: Literal["ok"] = "ok"
    data_ready: bool = Field(description="True when kbw_facts has rows or the mart has at least one election")
    kbw_stats: dict[str, int] | None = Field(
        default=None,
        description="Approximate live row counts (pg_stat_user_tables) when details=true",
    )
    mart_elections: list[dict[str, Any]] | None = Field(
        default=None, description="Elections loaded into the mart (election, year, round, loaded_at) when details=true"
    )


class AskResponse(BaseModel):
    """Shape returned by ``router.route_question`` (stable fields + optional analytics meta)."""

    model_config = ConfigDict(extra="ignore")

    question: str
    intent: str
    entity: str | None = None
    year: int | None = None
    years: list[int] = Field(default_factory=list)
    sql: str
    params: dict[str, Any]
    result: list[dict[str, Any]]
    candidate_geo_source: str | None = Field(
        default=None,
        description="kbw_candidate_geo_votes vs kbw_facts scan for kbw_candidate_geo_votes_detail",
    )
    mandate_extremes_source: str | None = Field(
        default=None,
        description="pkw vs kbw_fallback for kbw_sejm_mandate_vote_extremes",
    )
    answer: dict[str, Any] | None = Field(
        default=None,
        description="Polish answer text: {text, source: llm|fallback, verified, caveats}; LLM text only when its numbers match the rows; caveats are fixed notes about data gaps",
    )
    clarification: dict[str, Any] | None = Field(
        default=None,
        description="Set when the question is ambiguous: {prompt, kind, options: [{label, kind, ref}]}; re-ask with entity_refs",
    )


class FeedbackRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={"examples": [{"rating": "thumbs_up", "question": "Frekwencja w wyborach do Sejmu 2023"}]}
    )

    rating: Literal["thumbs_up", "thumbs_down"]
    question: str
    ask_response: dict[str, Any] | None = None


class QuestionHintsRequest(BaseModel):
    model_config = ConfigDict(json_schema_extra={"examples": [{"q": "frekwencja w", "limit": 5}]})

    q: str
    limit: int = Field(default=8, ge=1, le=20)
    exclude_question: str | None = None


class FeedbackOkResponse(BaseModel):
    ok: Literal[True] = True


class QuestionHintsResponse(BaseModel):
    """Logged-question suggestions from OpenSearch (shape of hit docs varies slightly)."""

    text_hits: list[dict[str, Any]]
    semantic_hits: list[dict[str, Any]]


class KbwCatalogSummaryResponse(BaseModel):
    """Rollups over ``kbw_dane_files`` after ``kbw_catalog.profile_kbw_dane_files``."""

    total_files: int
    by_year: dict[str, int]
    by_file_kind: dict[str, int]


class PersonSummary(BaseModel):
    """One row of ``mart.persons`` plus the years active, derived from its candidacies."""

    person_id: int = Field(description="mart.persons.person_id; rebuilt on every mart load (not stable across loads)")
    full_name: str = Field(description="Given names and SURNAME as published by PKW")
    last_name: str
    first_names: str
    gender: str | None = None
    match_rule: str = Field(
        description="How candidacies were linked: 'name+gender', 'name+gender+residence' (national; PKW publishes "
        "no person ids), 'local:...' (local person: name, gender, birth year and powiat or voivodeship) or 'manual' "
        "(person_overrides.csv). Namesakes may still be merged or split."
    )
    candidacies: int = Field(description="Number of candidacies (one per election round) in all loaded elections")
    first_year: int | None = Field(default=None, description="Year of the first candidacy")
    last_year: int | None = Field(default=None, description="Year of the latest candidacy")
    elected_count: int = Field(default=0, description="Number of candidacies with elected = true")


class PersonCandidacy(BaseModel):
    """One row of ``mart.v_person_history`` (one candidacy and round), with the election date."""

    year: int
    election_date: str | None = Field(default=None, description="ISO date of the election round (mart.elections)")
    chamber: str = Field(description="sejm, senat, prezydent, pe, sejmik, wbp (mayor), rada_gminy, rada_powiatu, rada_dzielnicy")
    round: int
    district_no: int | None = None
    area: str | None = Field(default=None, description="sejmik: voivodeship; wbp: gmina; councils: council name")
    committee_name: str | None = None
    list_no: int | None = None
    list_position: int | None = None
    votes: int | None = None
    elected: bool | None = None
    age: int | None = Field(default=None, description="Age on election day; only local registers publish it")


class PersonResponse(BaseModel):
    """``GET /persons/{person_id}``: the person and every candidacy ordered by election date."""

    person: PersonSummary
    candidacies: list[PersonCandidacy]


class PersonSearchHit(BaseModel):
    """A person whose name matches the search, as listed in ``mart.entity_lexicon``."""

    person_id: int
    full_name: str
    gender: str | None = None
    match_rule: str
    candidacies: int
    detail: str | None = Field(default=None, description="Short list of candidacies (election year, district, area)")
    years: list[int] = Field(default_factory=list, description="Years with a candidacy")


class PersonSearchResponse(BaseModel):
    """``GET /persons?name=...``: at most ``limit`` matches, exact surnames first, then by candidacies."""

    persons: list[PersonSearchHit]


@app.get(
    "/health",
    response_model=HealthResponse,
    response_model_exclude_none=True,
    tags=["health"],
)
def health(details: bool = Query(False, description="Include approximate KBW table row counts")) -> HealthResponse:
    """Health."""
    elections = mart_schema.loaded_elections()
    payload: dict[str, Any] = {"status": "ok", "data_ready": bool(elections) or db.kbw_data_ready()}
    if details:
        payload["kbw_stats"] = db.kbw_health_snapshot()
        payload["mart_elections"] = elections
    return HealthResponse.model_validate(payload)


@app.get("/kbw/catalog/summary", response_model=KbwCatalogSummaryResponse, tags=["catalog"])
def kbw_catalog_summary() -> KbwCatalogSummaryResponse:
    """Aggregate counts for mirror files recorded in ``kbw_dane_files``."""
    return KbwCatalogSummaryResponse.model_validate(db.kbw_dane_files_catalog_summary())


def _telemetry(result: dict[str, Any], started: float) -> dict[str, Any]:
    """Flat telemetry fields for the question log (status, latency, rows, answer source, tokens)."""
    meta = (result.get("params") or {}).get("_dynamic_sql_meta") or {}
    tokens = dict(meta.get("tokens") or {})
    for name, value in ((result.get("answer") or {}).get("tokens") or {}).items():
        tokens[name] = tokens.get(name, 0) + value
    return {
        "status": meta.get("status") or "template",
        "latency_ms": int((time.monotonic() - started) * 1000),
        "row_count": len(result.get("result") or []),
        "answer_source": (result.get("answer") or {}).get("source"),
        "prompt_tokens": tokens.get("prompt_tokens", 0),
        "completion_tokens": tokens.get("completion_tokens", 0),
        "error": meta.get("error") or (result.get("params") or {}).get("_dynamic_sql_error"),
    }


def require_api_key(x_api_key: str | None = Header(default=None)) -> str | None:
    """Reject the request unless it carries one of ``API_KEYS`` (no keys configured: open API)."""
    if not config.API_KEYS:
        return None
    if not x_api_key or not any(secrets.compare_digest(x_api_key, k) for k in config.API_KEYS):
        raise HTTPException(status_code=401, detail="Missing or invalid X-API-Key.")
    return x_api_key


def _json_value(value: Any) -> Any:
    """JSON-friendly scalar: numbers stay numbers (Decimal 34.27 -> 34.27, not "34.27"), dates as ISO text."""
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def _mart_rows(sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    """Run a fixed query as the read-only mart role; JSON-friendly values; 503 when the mart fails."""
    try:
        rows = safe_sql.run_readonly(sql, params=params)
    except psycopg.Error as exc:
        log.warning("mart query failed: %s", exc)
        raise HTTPException(status_code=503, detail="Election database is unavailable; try again later.") from exc
    return [{k: _json_value(v) for k, v in row.items()} for row in rows]


@app.get(
    "/persons",
    response_model=PersonSearchResponse,
    tags=["persons"],
    dependencies=[Depends(require_api_key)],
    summary="Find persons by name",
)
def search_persons(
    name: str = Query(min_length=2, max_length=100, description="Name or its beginning, e.g. 'Hołownia' or 'Szymon Hołow'"),
    limit: int = Query(20, ge=1, le=PERSON_SEARCH_MAX_ROWS, description="Maximum number of persons returned"),
) -> PersonSearchResponse:
    """Prefix search over persons in ``mart.entity_lexicon`` (national persons, elected mayors and sejmik
    councillors among local persons). Every word must start a word of the name and one must start the
    surname; case and Polish diacritics are ignored. Requires ``X-API-Key`` when ``API_KEYS`` is set; not
    counted against the ``/ask`` rate limit (fixed SQL, no LLM).
    """
    tokens = _NAME_TOKEN_RE.findall(name)[:5]
    if not tokens:
        return PersonSearchResponse(persons=[])
    rows = _mart_rows(PERSON_SEARCH_SQL, {"tokens": tokens, "limit": limit})
    return PersonSearchResponse.model_validate({"persons": rows[:limit]})


@app.get(
    "/persons/{person_id}",
    response_model=PersonResponse,
    tags=["persons"],
    dependencies=[Depends(require_api_key)],
    summary="Every candidacy of one person",
    responses={404: {"description": "No person with this id in the current mart load"}},
)
def get_person(person_id: int = Path(ge=1, description="person_id from an answer row, entity or search")) -> PersonResponse:
    """The person (``mart.persons``, with ``match_rule`` telling how candidacies were linked) and every
    candidacy from ``mart.v_person_history`` ordered by election date. ``person_id`` changes when the mart
    is rebuilt. Requires ``X-API-Key`` when ``API_KEYS`` is set; not counted against the ``/ask`` rate limit.
    """
    people = _mart_rows(PERSON_SQL, {"person_id": person_id})
    if not people:
        raise HTTPException(status_code=404, detail="Person not found.")
    candidacies = _mart_rows(PERSON_CANDIDACIES_SQL, {"person_id": person_id})
    years = [row["year"] for row in candidacies]
    person = {
        **people[0],
        "first_year": min(years, default=None),
        "last_year": max(years, default=None),
        "elected_count": sum(1 for row in candidacies if row["elected"]),
    }
    return PersonResponse.model_validate({"person": person, "candidacies": candidacies})


RESULT_ELECTIONS = ("sejm", "sejmik", "pe", "prezydent")
UnitLevel = Literal["country", "wojewodztwo", "powiat", "gmina", "wojewodztwo_1975"]


class BrowseElection(BaseModel):
    election: str
    year: int
    round: int
    election_date: str | None = None
    description: str | None = None


class BrowseRow(BaseModel):
    """One unit's turnout, or one party's / candidate's result in a unit."""

    model_config = ConfigDict(extra="allow")

    election: str
    year: int
    round: int
    level: str
    wojewodztwo: str | None = None
    teryt_powiat: str | None = None
    powiat: str | None = None
    teryt_gmina: str | None = None
    gmina: str | None = None


class BrowseRowsResponse(BaseModel):
    rows: list[BrowseRow]


class UnitHit(BaseModel):
    level: str
    unit: str = Field(description="Gmina (6 digits) or powiat (4 digits) TERYT, or the voivodeship name")
    name: str
    detail: str | None = None


class UnitCard(BaseModel):
    level: str
    unit: str
    name: str | None
    turnout: list[dict[str, Any]] = Field(description="Turnout per election and round (v_turnout)")
    winners: list[dict[str, Any]] = Field(description="Party or candidate with most votes per election and round")
    gus: dict[str, Any] | None = Field(description="Latest GUS statistics of the unit (v_gus_units)")


def _unit_params(level: str, unit: str | None) -> dict[str, Any]:
    """Filter parameters of _UNIT_FILTER for one unit (or all units of a level when unit is None)."""
    return {
        "level": level,
        "wojewodztwo": unit if level == "wojewodztwo" else None,
        "teryt_powiat": unit if level == "powiat" else None,
        "teryt_gmina": unit if level == "gmina" else None,
    }


@app.get("/elections", response_model=list[BrowseElection], tags=["browse"], dependencies=[Depends(require_api_key)])
def list_elections() -> list[BrowseElection]:
    """Every loaded election and round with its date (``mart.elections``)."""
    return [BrowseElection.model_validate(r) for r in _mart_rows(ELECTIONS_SQL, {})]


Chamber = Literal["sejm", "senat", "pe", "prezydent", "sejmik", "wbp", "rada_gminy", "rada_powiatu", "rada_dzielnicy"]


class CandidateYear(BaseModel):
    year: int
    candidates: int
    women_pct: float | None
    elected: int
    elected_women_pct: float | None
    median_age: float | None
    elected_median_age: float | None


class CandidateAge(BaseModel):
    end_: Literal["youngest", "oldest"]
    person_id: int | None
    full_name: str
    gender: str | None
    age: int
    area: str | None
    committee_name: str | None
    elected: bool | None


@app.get("/candidates/stats", response_model=list[CandidateYear], tags=["browse"], dependencies=[Depends(require_api_key)])
def candidate_stats(chamber: Chamber = Query(description="Election (chamber in v_person_history)")) -> list[CandidateYear]:
    """Per election year: candidates, elected, women's share of both and median age (ages: local
    elections and Sejm/Senate 1991-1997 only)."""
    return [CandidateYear.model_validate(r) for r in _mart_rows(CANDIDATE_STATS_SQL, {"chamber": chamber})]


@app.get("/candidates/ages", response_model=list[CandidateAge], tags=["browse"], dependencies=[Depends(require_api_key)])
def candidate_ages(
    chamber: Chamber = Query(description="Election (chamber in v_person_history)"),
    year: int = Query(ge=1989, le=2100),
    elected: bool = Query(False, description="Only elected candidates"),
) -> list[CandidateAge]:
    """The 10 youngest and 10 oldest candidates (or elected) of one election (``end_``: youngest/oldest)."""
    rows = _mart_rows(CANDIDATE_AGES_SQL, {"chamber": chamber, "year": year, "elected": elected})
    return [CandidateAge.model_validate(r) for r in rows]


class MayorTerms(BaseModel):
    person_id: int | None
    full_name: str
    terms: int = Field(description="Regular elections won since 2002")
    years: list[int]
    gminas: list[str | None]
    wojewodztwa: list[str | None]


class MayorTermsResponse(BaseModel):
    histogram: dict[int, int] = Field(description="Number of terms -> persons")
    top: list[MayorTerms] = Field(description="Persons with the most terms (up to 100)")


class MayorTerm(BaseModel):
    year: int
    round: int
    gmina: str | None
    wojewodztwo: str | None
    person_id: int | None
    full_name: str
    committee_name: str | None
    pct: float | None
    same_as_before: bool


@app.get("/mayors/terms", response_model=MayorTermsResponse, tags=["browse"], dependencies=[Depends(require_api_key)])
def mayor_terms(wojewodztwo: str | None = Query(None, max_length=40, description="Only gminas in this voivodeship")) -> MayorTermsResponse:
    """How many regular elections (2002 on) each mayor won: a histogram and the longest-serving persons.
    Persons are linked by name and birth year (see v_person_history.match_rule); by-elections are not counted."""
    rows = _mart_rows(MAYOR_TERMS_SQL, {"wojewodztwo": wojewodztwo})
    return MayorTermsResponse(histogram={r["terms"]: r["persons_with_terms"] for r in rows},
                              top=[MayorTerms.model_validate(r) for r in rows[:100]])


@app.get("/mayors/gmina/{teryt_gmina}", response_model=list[MayorTerm], tags=["browse"],
         dependencies=[Depends(require_api_key)])
def mayor_gmina(teryt_gmina: str = Path(pattern=r"^\d{6}$")) -> list[MayorTerm]:
    """Mayors of one gmina elected in each regular election since 2002, and whether the same person won again."""
    return [MayorTerm.model_validate(r) for r in _mart_rows(MAYOR_GMINA_SQL, {"teryt_gmina": teryt_gmina, "wojewodztwo": None})]


class PersonRank(BaseModel):
    person_id: int
    full_name: str
    candidacies: int
    elected: int
    first_year: int
    last_year: int
    career_years: int
    votes: int | None
    chambers: list[str]


class PartySwitch(BaseModel):
    from_family: str
    to_family: str
    persons: int
    examples: list[str]


@app.get("/rankings/persons", response_model=list[PersonRank], tags=["browse"], dependencies=[Depends(require_api_key)])
def person_rankings(
    by: Literal["candidacies", "elected", "career", "votes"] = Query("candidacies"),
    chamber: Chamber | None = Query(None, description="Only this election; default: persons with a national candidacy"),
) -> list[PersonRank]:
    """Top 50 persons by candidacies, wins, career length (first to last candidacy) or votes (all rounds)."""
    return [PersonRank.model_validate(r) for r in _mart_rows(PERSON_RANKING_SQL, {"by": by, "chamber": chamber})]


@app.get("/rankings/party-switches", response_model=list[PartySwitch], tags=["browse"], dependencies=[Depends(require_api_key)])
def party_switches(since: int = Query(1991, ge=1989, le=2100, description="Only candidacies from this year")) -> list[PartySwitch]:
    """Moves between party families from one Sejm, Senate or EP candidacy to the next, with examples."""
    return [PartySwitch.model_validate(r) for r in _mart_rows(PARTY_SWITCH_SQL, {"since": since})]


class ElectedRow(BaseModel):
    district_no: int | None
    area: str | None = Field(description="Voivodeship for sejmik districts")
    party: str
    committee_name: str | None
    person_id: int | None
    full_name: str
    list_position: int | None
    votes: int | None


@app.get("/elected", response_model=list[ElectedRow], tags=["browse"], dependencies=[Depends(require_api_key)])
def elected(
    chamber: Literal["sejm", "senat", "pe", "sejmik"] = Query(description="Election"),
    year: int = Query(ge=1989, le=2100),
    district_no: int | None = Query(None, ge=0, description="Only this district"),
    party: str | None = Query(None, max_length=40, description="Only this committee lead alias, e.g. KO or PiS"),
) -> list[ElectedRow]:
    """Elected candidates of one election by district, optionally of one committee (lead alias)."""
    params = {"chamber": chamber, "year": year, "district_no": district_no, "party": party}
    return [ElectedRow.model_validate(r) for r in _mart_rows(ELECTED_SQL, params)]


class ElectionCard(BaseModel):
    election: str
    year: int
    round: int
    election_date: date | None
    description: str | None
    turnout: dict[str, Any] | None = Field(description="Country turnout (v_turnout)")
    precincts: int | None = Field(description="Precincts with a protocol in the mart")
    gminas: int | None = Field(description="Gminas with a protocol (sejmik and mayors, which are published per gmina)")
    districts: int | None
    seats: int | None
    seat_method: str | None = Field(description="Seat allocation method (Sejm, sejmik, EP; mart.allocate_seats check)")
    thresholds_pct: list[float] | None = Field(description="Sejm thresholds in force (committees, coalitions)")


@app.get("/elections/{election}/{year}", response_model=ElectionCard, tags=["browse"], dependencies=[Depends(require_api_key)],
         responses={404: {"description": "Election not loaded"}})
def election_card(
    election: Literal["sejm", "senat", "pe", "prezydent", "referendum", "sejmik", "wbp"] = Path(),
    year: int = Path(ge=1989, le=2100),
    round: int = Query(1, ge=1, le=2),
) -> ElectionCard:
    """One election at a glance: date, turnout, precincts, districts, seats, method and thresholds. Fixed SQL."""
    info = [r for r in _mart_rows(ELECTIONS_SQL, {}) if (r["election"], r["year"], r["round"]) == (election, year, round)]
    if not info:
        raise HTTPException(status_code=404, detail="Election not loaded.")
    params = {"election": election, "year": year, "round": round}
    turnout = _mart_rows(ELECTION_TURNOUT_SQL, params)
    units = _mart_rows(ELECTION_UNITS_SQL[election], params)[0]
    method = None
    if election in ("sejm", "sejmik", "pe"):
        method = SEAT_METHOD_LABELS[mart_derived.SEAT_METHODS.get((election, year), "dhondt")]
        if election == "pe":
            method += " w kraju, Hare-Niemeyer między okręgami komitetu"
        if election == "sejm" and year <= 1997:
            method += "; lista krajowa: " + SEAT_METHOD_LABELS[mart_derived.SEAT_METHODS.get(("sejm national list", year), "dhondt")]
    thresholds = _mart_rows(SEJM_THRESHOLDS_SQL, params)[0]["thresholds"] if election == "sejm" else None
    return ElectionCard.model_validate({
        **info[0], "turnout": turnout[0] if turnout else None, "precincts": units.get("precincts"),
        "gminas": units.get("gminas"), "districts": units.get("districts"), "seats": units.get("seats"),
        "seat_method": method, "thresholds_pct": thresholds,
    })


class DataVersion(BaseModel):
    mart_loaded_at: datetime | None = Field(description="When the newest election was loaded into the mart")
    elections: int = Field(description="Elections and rounds in the mart")
    latest_election_date: date | None
    source_files: int = Field(description="KBW/PKW files in this release's source manifest")
    sources_last_modified: datetime | None = Field(description="Newest Last-Modified among those files")
    manifest_sha256: str | None = Field(description="Hash of data/source_manifest.json, to compare releases")


@functools.cache
def _manifest_summary() -> dict[str, Any]:
    """Source manifest summary, read once (the file ships with the image)."""
    try:
        raw = SOURCE_MANIFEST.read_bytes()
    except FileNotFoundError:
        return {"source_files": 0, "sources_last_modified": None, "manifest_sha256": None}
    files = json.loads(raw).values()
    modified = [parsedate_to_datetime(f["last_modified"]) for f in files if f.get("last_modified")]
    return {"source_files": len(files), "sources_last_modified": max(modified, default=None),
            "manifest_sha256": hashlib.sha256(raw).hexdigest()}


@app.get("/data-version", response_model=DataVersion, tags=["browse"], dependencies=[Depends(require_api_key)])
def data_version() -> DataVersion:
    """Mart load time and size (``mart.elections``) and the source files this release was built from."""
    rows = _mart_rows(DATA_VERSION_SQL, {})
    return DataVersion.model_validate({**(rows[0] if rows else {"elections": 0}), **_manifest_summary()})


@app.get("/turnout", response_model=BrowseRowsResponse, tags=["browse"], dependencies=[Depends(require_api_key)])
def get_turnout(
    election: Literal[BROWSE_ELECTIONS] = Query(description="Election kind"),  # type: ignore[valid-type]
    year: int = Query(ge=1989, le=2100),
    round: int = Query(1, ge=1, le=2),
    level: UnitLevel = Query("wojewodztwo"),
    wojewodztwo: str | None = Query(None, max_length=40, description="Only units in this voivodeship"),
    teryt_powiat: str | None = Query(None, pattern=r"^\d{4}$", description="Only units in this powiat"),
) -> BrowseRowsResponse:
    """Turnout per unit (``v_turnout``: official basis per election). Fixed SQL, no LLM."""
    params = {**_unit_params(level, None), "election": election, "year": year, "round": round,
              "wojewodztwo": wojewodztwo, "teryt_powiat": teryt_powiat}
    return BrowseRowsResponse.model_validate({"rows": _mart_rows(TURNOUT_SQL, params)})


@app.get("/results", response_model=BrowseRowsResponse, tags=["browse"], dependencies=[Depends(require_api_key)])
def get_results(
    election: Literal[RESULT_ELECTIONS] = Query(description="Election kind"),  # type: ignore[valid-type]
    year: int = Query(ge=1989, le=2100),
    round: int = Query(1, ge=1, le=2),
    level: UnitLevel = Query("country"),
    wojewodztwo: str | None = Query(None, max_length=40, description="Only units in this voivodeship"),
    teryt_powiat: str | None = Query(None, pattern=r"^\d{4}$", description="Only units in this powiat"),
) -> BrowseRowsResponse:
    """Votes and percentages per unit: parties (lists grouped by lead alias, ``v_party_results``) or
    presidential candidates (``v_prezydent_results``). Fixed SQL, no LLM."""
    params = {**_unit_params(level, None), "election": election, "year": year, "round": round,
              "wojewodztwo": wojewodztwo, "teryt_powiat": teryt_powiat}
    return BrowseRowsResponse.model_validate({"rows": _mart_rows(RESULTS_SQL, params)})


@app.get("/runoff", response_model=BrowseRowsResponse, tags=["browse"], dependencies=[Depends(require_api_key)])
def get_runoff(
    year: int = Query(ge=1989, le=2100, description="Presidential election year with a run-off"),
    level: UnitLevel = Query("country"),
    wojewodztwo: str | None = Query(None, max_length=40, description="Only units in this voivodeship"),
    teryt_powiat: str | None = Query(None, pattern=r"^\d{4}$", description="Only units in this powiat"),
) -> BrowseRowsResponse:
    """The two presidential finalists per unit: round 1 and round 2 votes and percent, and the gain between
    rounds (``gain_votes``, ``gain_pp``). Votes cannot show who moved where, only the net change."""
    params = {**_unit_params(level, None), "year": year, "wojewodztwo": wojewodztwo, "teryt_powiat": teryt_powiat}
    return BrowseRowsResponse.model_validate({"rows": _mart_rows(RUNOFF_SQL, params)})


@app.get("/change", response_model=BrowseRowsResponse, tags=["browse"], dependencies=[Depends(require_api_key)])
def get_change(
    election: Literal["sejm", "sejmik", "pe"] = Query(description="Election kind"),
    year: int = Query(ge=1989, le=2100),
    level: UnitLevel = Query("country"),
    wojewodztwo: str | None = Query(None, max_length=40, description="Only units in this voivodeship"),
    teryt_powiat: str | None = Query(None, pattern=r"^\d{4}$", description="Only units in this powiat"),
) -> BrowseRowsResponse:
    """Each party's result against the previous election of the same kind (``v_party_change``):
    ``change_pp`` in percentage points; ``comparable`` is false when the party's lead alias changed."""
    params = {**_unit_params(level, None), "election": election, "year": year,
              "wojewodztwo": wojewodztwo, "teryt_powiat": teryt_powiat}
    return BrowseRowsResponse.model_validate({"rows": _mart_rows(CHANGE_SQL, params)})


@app.get("/units", response_model=list[UnitHit], tags=["browse"], dependencies=[Depends(require_api_key)])
def search_units(name: str = Query(min_length=2, max_length=60, description="Beginning of a gmina, powiat or voivodeship name")) -> list[UnitHit]:
    """Gminas, powiats and voivodeships whose name starts with ``name`` (case and diacritics ignored)."""
    return [UnitHit.model_validate(r) for r in _mart_rows(UNIT_SEARCH_SQL, {"name": name.strip()})]


@app.get("/units/{unit}", response_model=UnitCard, tags=["browse"], dependencies=[Depends(require_api_key)],
         responses={404: {"description": "No results for this unit"}})
def get_unit(unit: str = Path(max_length=40, description="Gmina TERYT (6 digits), powiat TERYT (4 digits) or voivodeship name")) -> UnitCard:
    """One unit across every loaded election: turnout, the winner of each election and the latest GUS
    statistics. 1990-1997 results are included through mart.gmina_codes_pre1999 (today's units)."""
    level = "gmina" if re.fullmatch(r"\d{6}", unit) else "powiat" if re.fullmatch(r"\d{4}", unit) else "wojewodztwo"
    params = _unit_params(level, unit)
    turnout = _mart_rows(UNIT_TURNOUT_SQL, params)
    if not turnout:
        raise HTTPException(status_code=404, detail="No results for this unit.")
    gus = _mart_rows(UNIT_GUS_SQL, {"level": level, "unit": unit})
    return UnitCard.model_validate({
        "level": level, "unit": unit, "name": gus[0]["name"] if gus else turnout[-1]["unit_name"],
        "turnout": [{k: v for k, v in r.items() if k != "unit_name"} for r in turnout],
        "winners": _mart_rows(UNIT_WINNERS_SQL, params),
        "gus": gus[0] if gus else None,
    })


class SeatSimulationRow(BaseModel):
    committee_name: str
    party: str
    committee_ids: list[int]
    votes: int
    pct: float
    seats_official: int
    seats_simulated: int


@app.get("/seats/simulate", response_model=list[SeatSimulationRow], tags=["browse"], dependencies=[Depends(require_api_key)])
def simulate_seats(
    year: int = Query(ge=2001, le=2100, description="Sejm election year (2001 on)"),
    method: Literal["dhondt", "sainte_lague", "sainte_lague_modified", "hare_niemeyer"] = Query("dhondt"),
    threshold: float = Query(5.0, ge=0, le=50, description="Share of national list votes a committee needs, in %"),
    coalition_threshold: float = Query(8.0, ge=0, le=50, description="The same for coalition committees, in %"),
    exempt_minority: bool = Query(True, description="National-minority lists pass below the threshold, as in the law"),
    national: bool = Query(False, description="One national district with all 460 seats instead of the 41 districts"),
    merge: list[int] = Query([], description="Committee ids to run as one coalition list (votes summed per district)"),
) -> list[SeatSimulationRow]:
    """Sejm seats per committee under another method, threshold or district layout, recomputed with
    ``mart.allocate_seats`` from the official list votes; ``seats_official`` is the real result. Fixed SQL,
    no LLM."""
    params = {"year": year, "method": method, "threshold": threshold, "coalition_threshold": coalition_threshold,
              "exempt_minority": exempt_minority, "national": national, "merge": [str(m) for m in merge]}
    rows = _mart_rows(SEAT_SIMULATION_SQL, params)
    if not rows:
        raise HTTPException(status_code=404, detail="No Sejm election in this year.")
    return [SeatSimulationRow.model_validate(r) for r in rows]


@app.post("/ask", response_model=AskResponse, tags=["ask"])
def ask(request: AskRequest, http_request: Request, api_key: str | None = Depends(require_api_key)) -> AskResponse:
    """Answer a question: route it to SQL, run it, and describe the rows in Polish."""
    if not request.question.strip():
        raise HTTPException(status_code=400, detail="Question is required.")
    client = http_request.client.host if http_request.client else "unknown"
    # With API keys each key has its own limit and budget; otherwise each client IP.
    client_key = f"key:{api_key}" if api_key else client
    if not ask_limiter.allow(client_key):
        raise HTTPException(status_code=429, detail="Too many questions; try again in a minute.")
    if token_budget.exceeded(client_key):
        raise HTTPException(status_code=429, detail="Daily token budget used up; try again tomorrow.")

    started = time.monotonic()
    refs = [r.model_dump() for r in request.entity_refs] if request.entity_refs else None
    previous = request.previous.model_dump() if request.previous else None
    result = router.route_question(request.question, entity_refs=refs, previous=previous)
    if not result.get("clarification"):
        result["answer"] = answer.compose_answer(request.question, result.get("result") or [], result.get("sql") or "")
    result["result"] = [{k: _json_value(v) for k, v in row.items()} for row in result.get("result") or []]
    telemetry = _telemetry(result, started)
    token_budget.add(client_key, telemetry["prompt_tokens"] + telemetry["completion_tokens"])
    log.info(
        "ask intent=%s status=%s rows=%s latency_ms=%s answer=%s tokens=%s/%s",
        result["intent"], telemetry["status"], telemetry["row_count"], telemetry["latency_ms"],
        telemetry["answer_source"], telemetry["prompt_tokens"], telemetry["completion_tokens"],
    )
    opensearch_store.log_question(
        question=request.question,
        detected_intent=result["intent"],
        sql=result["sql"],
        params=result["params"],
        telemetry=telemetry,
    )
    return AskResponse.model_validate(result)


@app.post("/feedback", response_model=FeedbackOkResponse, tags=["feedback"], dependencies=[Depends(require_api_key)])
def feedback(request: FeedbackRequest) -> FeedbackOkResponse:
    """Feedback."""
    if request.rating == "thumbs_down":
        if not request.ask_response:
            raise HTTPException(
                status_code=400,
                detail="ask_response is required for thumbs_down.",
            )
        feedback_store.append_feedback(
            {
                "rating": request.rating,
                "question": request.question.strip(),
                "needs_fix": True,
                "ask_response": request.ask_response,
            }
        )
    else:
        feedback_store.append_feedback(
            {
                "rating": request.rating,
                "question": request.question.strip(),
                "needs_fix": False,
            }
        )
        # A confirmed LLM-written query becomes a few-shot example for similar questions.
        response = request.ask_response or {}
        if response.get("intent") == "dynamic_sql" and response.get("sql"):
            sql_examples.add_example(request.question.strip(), response["sql"], source="user_confirmed")
    return FeedbackOkResponse()


@app.post("/question-hints", response_model=QuestionHintsResponse, tags=["hints"])
def question_hints(body: QuestionHintsRequest) -> QuestionHintsResponse:
    """Full-text and kNN suggestions over logged questions (OpenSearch)."""
    q = body.q.strip()
    if len(q) < 2:
        return QuestionHintsResponse(text_hits=[], semantic_hits=[])

    limit = body.limit
    exclude = (body.exclude_question or "").strip().lower()

    def _drop_excluded(hits: list[dict]) -> list[dict]:
        """Drop excluded."""
        if not exclude:
            return hits
        return [h for h in hits if (h.get("question") or "").strip().lower() != exclude]

    text_hits = _drop_excluded(opensearch_store.search_hints_text(q, limit))

    semantic_hits: list[dict] = []
    if len(q) >= config.QUESTION_HINTS_SEMANTIC_MIN_CHARS:
        semantic_hits = _drop_excluded(opensearch_store.search_hints_semantic(q, limit))

    return QuestionHintsResponse(
        text_hits=text_hits[:limit],
        semantic_hits=semantic_hits[:limit],
    )
