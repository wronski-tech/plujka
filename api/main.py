from __future__ import annotations

import logging
import re
import secrets
import time
from contextlib import asynccontextmanager
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

import psycopg
from fastapi import Depends, FastAPI, Header, HTTPException, Path, Query, Request
from psycopg.conninfo import conninfo_to_dict
from pydantic import BaseModel, ConfigDict, Field

from api.services import answer, config, db, feedback_store, opensearch_store, router
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
    """Run a fixed person query as the read-only mart role; JSON-friendly values; 503 when the mart fails."""
    try:
        rows = safe_sql.run_readonly(sql, params=params)
    except psycopg.Error as exc:
        log.warning("person query failed: %s", exc)
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
UnitLevel = Literal["country", "wojewodztwo", "powiat", "gmina"]


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


@app.get("/units", response_model=list[UnitHit], tags=["browse"], dependencies=[Depends(require_api_key)])
def search_units(name: str = Query(min_length=2, max_length=60, description="Beginning of a gmina, powiat or voivodeship name")) -> list[UnitHit]:
    """Gminas, powiats and voivodeships whose name starts with ``name`` (case and diacritics ignored)."""
    return [UnitHit.model_validate(r) for r in _mart_rows(UNIT_SEARCH_SQL, {"name": name.strip()})]


@app.get("/units/{unit}", response_model=UnitCard, tags=["browse"], dependencies=[Depends(require_api_key)],
         responses={404: {"description": "No results for this unit"}})
def get_unit(unit: str = Path(max_length=40, description="Gmina TERYT (6 digits), powiat TERYT (4 digits) or voivodeship name")) -> UnitCard:
    """One unit across every loaded election: turnout, the winner of each election and the latest GUS
    statistics. Units from before 1999 are not covered (results of that period are national only)."""
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
