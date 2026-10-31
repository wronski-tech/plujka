# Definitions

Answers to definition questions ("Co to jest frekwencja?", "Jak działa metoda D'Hondta?",
"What is the electoral threshold?") come from this file, not from the model: `api/services/definitions.py`
reads every `##` section, matches its `match` pattern (a regular expression over the folded
question: lower case, no Polish letters) and returns the Polish text, or the text after `**EN:**`
for English questions. Only questions that ask for a definition and name no year are answered here;
everything else goes to SQL. Keep the texts free of results: numbers in them are rules (thresholds,
divisors), not data. When a rule changes in the mart (`derived.py`, schema comments), change it here too.

## Frekwencja
<!-- match: frekwencj|turnout -->
Frekwencja to odsetek uprawnionych do głosowania, którzy wzięli udział w wyborach, liczony tak jak
PKW: liczba kart ważnych wyjętych z urny podzielona przez liczbę uprawnionych. W części wyborów PKW
liczyła ją z kart wydanych wyborcom (Sejm i Senat 2001–2007, prezydent 2000–2010, sejmiki i wójtowie
2002 i 2006). W bazie: kolumna `turnout_pct` widoku `mart.v_turnout` (licznik w `turnout_basis`).

**EN:** Turnout is the share of eligible voters who took part, computed as PKW did: valid ballot
papers taken from the ballot box divided by eligible voters. In some elections PKW used ballot papers
issued to voters instead (Sejm and Senate 2001–2007, president 2000–2010, sejmiki and mayors 2002 and
2006). In the database: `turnout_pct` of `mart.v_turnout` (numerator in `turnout_basis`).

## Próg wyborczy
<!-- match: prog\w* wyborcz|\bprog\w*\b|threshold -->
Próg wyborczy to minimalny odsetek ważnych głosów, który komitet musi zdobyć, by brać udział w podziale
mandatów. W wyborach do Sejmu: 5% dla komitetu, 8% dla koalicji, bez progu dla komitetów mniejszości
narodowych; w 1991 roku progu nie było. W wyborach do Parlamentu Europejskiego 5% w skali kraju, do
sejmików 5% w skali województwa. W bazie: `threshold_pct` i `passed_threshold` w
`mart.v_sejm_national_results`.

**EN:** The electoral threshold is the minimum share of valid votes a committee needs to take part in
seat allocation. Sejm: 5% for a committee, 8% for a coalition, none for national-minority committees;
there was no threshold in 1991. European Parliament: 5% nationwide; sejmiki: 5% in the voivodeship.
In the database: `threshold_pct` and `passed_threshold` in `mart.v_sejm_national_results`.

## Metoda D'Hondta
<!-- match: hondt -->
Metoda D'Hondta dzieli mandaty w okręgu między listy: liczbę głosów każdej listy dzieli się kolejno przez
1, 2, 3, … i mandaty dostają największe ilorazy. Sprzyja większym komitetom. Stosowana w wyborach do
Sejmu (poza 1991 i 2001), Parlamentu Europejskiego, sejmików i rad wybieranych proporcjonalnie.
W bazie: `mart.v_seat_methods` i funkcja `mart.allocate_seats`.

**EN:** The D'Hondt method splits a district's seats between lists: each list's votes are divided by
1, 2, 3, … and the largest quotients win seats. It favours larger committees. Used for the Sejm
(except 1991 and 2001), the European Parliament, sejmiki and proportionally elected councils. In the
database: `mart.v_seat_methods` and the function `mart.allocate_seats`.

## Metoda Sainte-Laguë
<!-- match: sainte|lague -->
Metoda Sainte-Laguë dzieli głosy każdej listy przez kolejne liczby nieparzyste 1, 3, 5, … i mandaty
dostają największe ilorazy; jest bardziej proporcjonalna niż D'Hondt. W wersji zmodyfikowanej
pierwszym dzielnikiem jest 1,4 zamiast 1 — tak dzielono mandaty w wyborach do Sejmu w 2001 roku.
W bazie: `mart.v_seat_methods` i funkcja `mart.allocate_seats`.

**EN:** The Sainte-Laguë method divides each list's votes by the odd numbers 1, 3, 5, … and the largest
quotients win seats; it is more proportional than D'Hondt. The modified version uses 1.4 instead of 1
as the first divisor; the 2001 Sejm seats were allocated this way. In the database:
`mart.v_seat_methods` and the function `mart.allocate_seats`.

## Metoda Hare'a-Niemeyera
<!-- match: hare|niemeyer|najwieksz\w* reszt|largest remainder -->
Metoda Hare'a-Niemeyera (największych reszt) daje każdej liście całą część z iloczynu jej udziału
w głosach i liczby mandatów, a pozostałe mandaty listom o największych resztach. Tak dzielono mandaty
w okręgach w wyborach do Sejmu w 1991 roku.

**EN:** The Hare-Niemeyer (largest remainder) method gives each list the whole part of its vote share
times the number of seats, and the remaining seats to the lists with the largest remainders. It was
used in the districts of the 1991 Sejm election.

## Głosy ważne i nieważne
<!-- match: niewazn|glos\w* wazn|invalid vote|valid vote -->
Głos ważny to karta, na której wyborca poprawnie wskazał wybór, np. postawił znak „x” przy jednym
kandydacie. Głos nieważny to karta wyjęta z urny bez ważnego wyboru, np. ze znakiem przy kilku
kandydatach różnych list albo bez żadnego znaku. Procenty komitetów i kandydatów liczy się od głosów
ważnych.

**EN:** A valid vote is a ballot on which the voter marked a choice correctly, e.g. an "x" next to one
candidate. An invalid vote is a ballot taken from the box without a valid choice, e.g. marks next to
candidates of several lists or no mark at all. Committee and candidate percentages are shares of
valid votes.

## Druga tura
<!-- match: drug\w* tur|dogrywk|runoff|second round -->
W wyborach prezydenta oraz wójta, burmistrza i prezydenta miasta wygrywa w pierwszej turze kandydat,
który zdobył więcej niż połowę ważnych głosów. Jeśli nikt jej nie zdobył, dwa tygodnie później odbywa
się druga tura z udziałem dwóch kandydatów z największą liczbą głosów. W bazie: kolumna `round`.

**EN:** In presidential and mayoral elections a candidate with more than half of the valid votes wins
in the first round. Otherwise a second round between the two candidates with the most votes takes
place two weeks later. In the database: the `round` column.

## Ważność referendum
<!-- match: referend.*\b(wazn|wiazac)|\b(wazn|wiazac)\w*.*referend|referend.*\b(valid|binding)|\b(valid|binding).*referend -->
Wynik referendum ogólnokrajowego jest wiążący, gdy wzięła w nim udział więcej niż połowa uprawnionych.
Lokalne referendum w sprawie odwołania wójta, burmistrza, prezydenta miasta albo rady jest ważne, gdy
wzięło w nim udział co najmniej 3/5 liczby osób, które głosowały w wyborach odwoływanego organu.
W bazie: `mart.v_referendum_results` i kolumna `is_valid` w `mart.local_referenda`.

**EN:** A national referendum is binding when more than half of eligible voters take part. A local
recall referendum (mayor or council) is valid when at least 3/5 of the number of people who voted in
the election of that organ take part. In the database: `mart.v_referendum_results` and the `is_valid`
column of `mart.local_referenda`.

## Rodziny partii
<!-- match: rodzin\w* (parti|polityczn|komitet)|party famil -->
Rodzina partii łączy komitety tej samej formacji z różnych lat pod jedną nazwą (np. PO i KO), żeby
porównywać wyniki między wyborami. Przypisanie komitetów i opis ich rodowodu są w tabeli
`mart.party_families`; korzystają z niej widoki `mart.v_party_results` i `mart.v_party_change`.

**EN:** A party family groups the committees of one political formation across years under one name
(e.g. PO and KO) so results can be compared between elections. The assignment and the lineage notes
are in `mart.party_families`, used by `mart.v_party_results` and `mart.v_party_change`.

## TERYT
<!-- match: teryt -->
TERYT to urzędowy kod jednostki terytorialnej: dwie pierwsze cyfry oznaczają województwo, cztery
powiat, sześć gminę (siódma cyfra, gdy występuje, to rodzaj gminy). Wyniki z lat 1990–1998 mają dawne
kody gmin; klucz do dzisiejszych jednostek to `mart.gmina_codes_pre1999`.

**EN:** TERYT is the official code of a territorial unit: the first two digits are the voivodeship,
four the powiat, six the gmina (a seventh digit, when present, is the gmina type). Results of
1990–1998 use the old gmina codes; `mart.gmina_codes_pre1999` maps them to today's units.

## Okręg wyborczy i obwód głosowania
<!-- match: okreg\w* wyborcz|obwod\w* glosowan|czym (sie )?rozni\w* okreg|electoral district|polling district|constituency -->
Okręg wyborczy to obszar, w którym wybiera się określoną liczbę posłów, senatorów lub radnych; komitety
zgłaszają w nim listy. Obwód głosowania to najmniejsza jednostka, z własną komisją i lokalem
wyborczym, gdzie się głosuje i liczy głosy. W bazie: tabele `*_district*` (okręgi) i
`mart.v_precinct_results` (obwody).

**EN:** An electoral district elects a fixed number of MPs, senators or councillors; committees register
their lists there. A polling district is the smallest unit, with its own commission and polling
station, where votes are cast and counted. In the database: the `*_district*` tables (districts) and
`mart.v_precinct_results` (polling districts).

## Okręgi jednomandatowe (JOW)
<!-- match: \bjow\b|jednomandat|single.member -->
W okręgu jednomandatowym wybiera się jedną osobę: mandat dostaje kandydat z największą liczbą głosów.
Tak wybiera się senatorów od 2011 roku oraz radnych gmin do 20 tys. mieszkańców (w 2014 roku: wszystkich
gmin poza miastami na prawach powiatu).

**EN:** A single-member district elects one person: the candidate with the most votes wins the seat.
Senators have been elected this way since 2011, and councillors of gminas up to 20,000 inhabitants
(in 2014: of all gminas except cities with powiat rights).

## Sejmik
<!-- match: co to (jest|sa) sejmik|czym (jest|sa) sejmik|sejmik\w* (to|wojewodztw)|regional assembl|voivodeship assembl -->
Sejmik województwa to organ stanowiący samorządu województwa, wybierany w wyborach samorządowych
proporcjonalnie w okręgach wielomandatowych. W bazie: tabele i widoki `sejmik_*`.

**EN:** A sejmik is the elected assembly of a voivodeship's self-government, chosen in local elections
by proportional representation in multi-member districts. In the database: the `sejmik_*` tables and
views.

## Wójt, burmistrz, prezydent miasta
<!-- match: \bwojt|burmistrz|prezydent\w* miast|\bmayor -->
Wójt (w gminie wiejskiej), burmistrz (w gminie z miastem) i prezydent miasta (w większych miastach)
to organ wykonawczy gminy. Od 2002 roku wybierani są bezpośrednio, w razie potrzeby w dwóch turach.
W bazie: tabele i widoki `wbp_*`.

**EN:** The wójt (rural gmina), burmistrz (gmina with a town) and prezydent miasta (larger cities) are
the gmina's executive. Since 2002 they have been elected directly, in two rounds when needed. In the
database: the `wbp_*` tables and views.
