# Roadmap

Plan w trzech etapach. Kolejność realizacji: etap 3 (punkty 1–30), etap 1, reszta etapu 3, etap 2.
Zasada repo obowiązuje wszędzie: fakty tylko z SQL; LLM interpretuje pytanie i pisze SQL, ale nie
dostarcza faktów (`AGENTS.md`). Odhaczone punkty są zrobione.

## Etap 1 — funkcje bez klucza API (stały SQL nad `mart`)

Cel: aplikacja jest użyteczna bez LLM (także przy limicie tokenów lub awarii OpenAI). Gotowe, gdy
funkcja ma stały SQL na roli tylko do odczytu, test i ekran w UI.

**Eksploratory wyników**
- [x] 1. Tryb „Przeglądaj”: wybór wyborów, roku, poziomu i jednostki → tabela, mapa, wykres bez pytania.
- [x] 2. Karta gminy / powiatu / województwa: wszystkie wybory, frekwencja w czasie, zwycięzcy, GUS.
- [x] 3. Porównanie dwóch jednostek obok siebie.
- [x] 4. Porównanie dwóch wyborów dla jednej jednostki (`v_party_change`).
- [x] 5. Ranking jednostek według miary, z filtrem województwa.
- [ ] 6. Wyniki obwodowe z wyszukiwaniem obwodu po numerze i gminie.
- [ ] 7. Wykres liniowy jednostki w czasie (`chart_columns.py`).
- [ ] 8. Karta wyborów: frekwencja, mandaty, progi, metoda, liczba obwodów.
- [ ] 9. Lista wybranych według okręgu z filtrem komitetu.
- [ ] 10. Druga tura (prezydent, wójtowie): przepływ wyników między turami w jednostce.

**Symulacje**
- [x] 11. Symulator mandatów: metoda i próg suwakami (`mart.allocate_seats`).
- [x] 12. Symulacja koalicji (komitety jako jedna lista).
- [x] 13. Inny podział na okręgi (np. jeden okręg krajowy).
- [x] 14. Koszt mandatu: głosy na mandat według komitetu.
- [ ] 15. Głosy zmarnowane według województwa. (Krajowo już w symulatorze mandatów.)

**Osoby**
- [ ] 16. Rankingi osób: kandydowania, długość kariery, suma głosów.
- [ ] 17. Przejścia między komitetami w czasie.
- [ ] 18. Wójtowie i burmistrzowie: ciągłość władzy, liczba kadencji.
- [ ] 19. Najmłodsi i najstarsi kandydaci i wybrani.
- [ ] 20. Kobiety wśród kandydatów i wybranych w czasie.
- [ ] 21. „Zgłoś błędne połączenie osoby” → `feedback.jsonl`.

**Dane**
- [ ] 22. Mapowanie kodów gmin sprzed 1999 jako tabela w marcie (odtwarzalny skrypt, kontrole).
- [ ] 23. Widoki lat 90. według dawnych województw i dzisiejszych gmin, z `CAVEATS`.
- [ ] 24. Wybory samorządowe 1994 i 1990 (jeśli KBW publikuje pliki).
- [ ] 25. Głosy kandydatów na poziomie gminy z plików obwodowych.
- [ ] 26. Więcej GUS: struktura wieku, wykształcenie (NSP 2002/2011/2021), dochody gmin.
- [ ] 27. Wybory uzupełniające do Senatu.
- [ ] 28. Referenda lokalne i odwołania wójtów (jeśli dane są dostępne).
- [ ] 29. Granice okręgów sejmowych i senackich na mapie.
- [ ] 30. Adresy obwodów i wyszukiwarka „mój obwód”.

**API i otwarte dane**
- [x] 31. Endpointy bez LLM: `/elections`, `/units/{teryt}`, `/results`, `/turnout`.
- [ ] 32. Eksport Parquet i XLSX.
- [ ] 33. Paczki danych na wybory (zip ze słownikiem kolumn).
- [ ] 34. `GET /data-version`: wersja martu i daty źródeł.
- [ ] 35. Widget do osadzania (mapa / wykres dla jednostki).
- [ ] 36. Stałe linki odtwarzające widok „Przeglądaj”.
- [ ] 37. Słownik danych generowany z `COMMENT ON`.

**UI i dostępność**
- [x] 38. Panel „Co jest w bazie”.
- [ ] 39. Gotowe zestawienia dla każdej rodziny wyborów, działające bez LLM.
- [ ] 40. Tryb ciemny i palety map przyjazne daltonistom.
- [ ] 41. Dostępność: klawiatura, kontrast, opisy wykresów.
- [ ] 42. Widok mobilny.
- [ ] 43. Legenda map z wyborem przedziałów.
- [ ] 44. Eksport mapy i wykresu do PNG/SVG.
- [ ] 45. Historia zapytań w sesji.
- [ ] 46. Angielska wersja interfejsu (etykiety).

**Jakość danych dla użytkownika**
- [ ] 47. Strona „Znane luki w danych” z `docs/MART.md` i `CAVEATS`.
- [ ] 48. Link do pliku źródłowego KBW/PKW przy każdym wyniku.
- [ ] 49. Raport zgodności z PKW dla każdych wyborów.
- [ ] 50. Strona „Zmiany w danych” z `source_manifest.json`.

## Etap 2 — funkcje z kluczem API (LLM)

Cel: lepsze rozumienie pytań i odpowiedzi bez faktów spoza SQL. Gotowe, gdy funkcja przechodzi przez
`validate_mart_sql` → `run_readonly` → `compose_answer` i ma przypadki w zestawie ewaluacyjnym.

**Ewaluacja**
- [ ] 1. Zestaw ewaluacyjny (300 pytań z oczekiwanym wynikiem), nocne CI z prawdziwym modelem.
- [ ] 2. Metryki: trafność, doprecyzowania, naprawy, koszt, czas — według rodziny wyborów.
- [ ] 3. Grupowanie pytań z kciukiem w dół przez LLM, propozycje przykładów do zatwierdzenia.
- [ ] 4. Kolejka „kciuk w górę → przykład SQL” z ręcznym zatwierdzeniem.
- [ ] 5. Wykrywanie regresji po zmianie modelu lub promptu.

**Rozumienie pytań**
- [ ] 6. Doprecyzowania jako 2–4 interpretacje z gotowym SQL.
- [ ] 7. Pytania wieloczęściowe (kilka zapytań, połączony wynik).
- [ ] 8. Pytania porównawcze z pamięcią rozmowy.
- [ ] 9. Potoczne nazwy komitetów i osób, leksykon jako źródło prawdy.
- [ ] 10. Literówki i brak polskich znaków.
- [ ] 11. Pytania po angielsku.
- [ ] 12. Definicje z dokumentacji repo, nie z wiedzy modelu.
- [ ] 13. Odmowa dla pytań spoza zakresu (sondaże, prognozy).
- [ ] 14. Okresy („lata 90.”, „od 2005”) → lata obecne w bazie.
- [ ] 15. Pytania geograficzne przez widoki atrybutów jednostek.

**Generowanie SQL**
- [ ] 16. Wybór schematu przez embeddingi (`FAMILY_KEYWORDS` jako zabezpieczenie).
- [ ] 17. Plan zapytania, potem SQL.
- [ ] 18. Routing modeli: tańszy najpierw, droższy przy naprawie.
- [ ] 19. Prompt caching stałej części promptu.
- [ ] 20. Naprawa SQL z `EXPLAIN` w drugiej próbie.
- [ ] 21. Odrzucanie zbyt kosztownych zapytań przed uruchomieniem.
- [ ] 22. Automatyczne `LIMIT` i sortowanie.
- [ ] 23. Korelacje z GUS (`corr()`, `regr_slope()`).
- [ ] 24. Zmiany między wyborami z rodzinami partii.
- [ ] 25. Mandaty hipotetyczne przez `allocate_seats`.

**Odpowiedzi**
- [ ] 26. Wyjaśnienie zapytania po polsku, generowane z SQL.
- [ ] 27. Wybór typu wykresu spośród dozwolonych, reguła deterministyczna jako zabezpieczenie.
- [ ] 28. Tytuły i podpisy wykresów.
- [ ] 29. Podsumowanie dużego wyniku (liczby z wierszy).
- [ ] 30. Zastrzeżenia dobrane do wyniku.
- [ ] 31. Proponowane pytania uzupełniające.
- [ ] 32. Styl odpowiedzi: dziennikarski / techniczny.
- [ ] 33. Raport PDF dla gminy lub wyborów.
- [ ] 34. Opis mapy tekstem (dostępność).
- [ ] 35. Wyjaśnienie pustego wyniku.

**Interakcja**
- [ ] 36. Wejście głosowe.
- [ ] 37. Tryb czatu z edycją poprzedniego pytania.
- [ ] 38. „Pokaż coś ciekawego” z biblioteki gotowych zestawień.
- [ ] 39. Asystent tworzenia zestawień → zapisany widok.
- [ ] 40. Interfejs MCP / API dla agentów z tymi samymi strażnikami.

**Koszty i bezpieczeństwo**
- [ ] 41. Testy prompt injection (pytania, nazwy komitetów i osób).
- [ ] 42. Panel kosztów: tokeny dziennie według klucza i modelu.
- [ ] 43. Cache semantyczny: podobne pytanie → ten sam zwalidowany SQL.
- [ ] 44. Limit kosztu jednego pytania.
- [ ] 45. Tryb awaryjny: przy błędzie OpenAI tryb „Przeglądaj” zamiast szablonów Sejmu.
- [ ] 46. Inny dostawca LLM (np. Azure OpenAI w UE) przez konfigurację.
- [ ] 47. Anonimizacja logów przed analizą przez LLM.
- [ ] 48. Limit długości i liczby pytań na sesję.
- [ ] 49. Ocena pewności SQL widoczna w UI.
- [ ] 50. Wersjonowanie promptów z wynikami ewaluacji.

## Etap 3 — przygotowanie do produkcji

Cel: odtwarzalne wdrożenie na Hetznerze z backupem, monitoringiem i aktualną dokumentacją. Gotowe,
gdy nowy serwer powstaje z Terraformu, przechodzi próbę odtworzenia i ma alerty.

**Dokumentacja**
- [x] 1. Przegląd dokumentów: liczby, rozmiary, polecenia, ścieżki.
- [x] 2. Duplikaty: `Architecture.md` / `docs/ARCHITECTURE.md`, `AGENT_INSTRUCTIONS.md` / `AGENTS.md`.
- [x] 3. README: szybki start, zakres danych.
- [x] 4. `docs/DEPLOYMENT.md`: Hetzner krok po kroku.
- [x] 5. Runbook incydentów.
- [ ] 6. Odtwarzanie z backupu, przetestowane.
- [ ] 7. Licencja kodu (decyzja właściciela) i atrybucja danych (NOTICE — zrobione; brak LICENSE).
- [x] 8. CHANGELOG i wydania z tagów.
- [x] 9. Dokumentacja API (OpenAPI) z przykładami.
- [x] 10. Sprawdzanie linków w dokumentacji w CI.

**Terraform dla Hetznera**
- [x] 11. Moduł `infra/hetzner`: serwer, firewall, klucz SSH.
- [x] 12. Hetzner Volume na dane Postgresa i OpenSearch.
- [x] 13. Cloud-init: Docker, aktualizacje, start compose.
- [x] 14. Caddy z TLS przed API i UI.
- [x] 15. DNS.
- [x] 16. Storage Box na backupy.
- [x] 17. Stan Terraformu w Object Storage.
- [x] 18. Sekrety poza repo.
- [x] 19. Środowiska staging i production.
- [x] 20. Resize i wymiana serwera bez utraty danych.

**CI i wdrożenia**
- [x] 21. Dump martu z CI jako asset release'u z sumą kontrolną.
- [x] 22. Skrypt wdrożenia martu: pobranie, `pg_trgm`, restore, `derived.rebuild`, uprawnienia.
- [x] 23. Wdrożenie martu bez przestoju (druga baza, przełączenie `MART_DATABASE_URL`).
- [x] 24. Cache martu w CI (hash danych i kodu buildu).
- [x] 25. Pomiar w CI: rozmiar martu i dumpu, wolny dysk, czas buildu.
- [x] 26. Obrazy Dockera w GHCR z tagiem commita.
- [x] 27. Skanowanie obrazów i zależności.
- [x] 28. Wdrożenie staging przy pushu, produkcja po zatwierdzeniu.
- [x] 29. Test dymny po wdrożeniu.
- [x] 30. Lint i sprawdzanie typów w CI.

**Testy**
- [ ] 31. Testy end-to-end z zaślepionym LLM.
- [ ] 32. Pokrycie testami w CI.
- [ ] 33. Testy kontraktowe nowych endpointów.
- [ ] 34. Testy UI (`AppTest`).
- [ ] 35. Test regresyjny encji na bazie.
- [ ] 36. Testy właściwości `allocate_seats`.
- [ ] 37. Test obciążenia z zaślepionym LLM.
- [ ] 38. Test odtwarzania z backupu w CI.

**Backup i odtwarzanie**
- [ ] 39. Nocny backup `public` na Storage Box, retencja 7 dni / 4 tygodnie / 6 miesięcy.
- [ ] 40. Backup `feedback.jsonl` i konfiguracji.
- [ ] 41. Snapshoty OpenSearch albo log pytań w Postgresie (pgvector).
- [ ] 42. Comiesięczna próba odtworzenia na nowym serwerze.
- [ ] 43. Szyfrowanie backupów, klucz poza serwerem.

**Monitoring i bezpieczeństwo**
- [ ] 44. Monitoring dostępności z zewnątrz z alertem.
- [ ] 45. Prometheus + Grafana (Postgres, węzeł, API).
- [ ] 46. Alerty: dysk, wolne zapytania, 5xx, tokeny, backup.
- [ ] 47. Centralne logi z identyfikatorem żądania.
- [ ] 48. Śledzenie błędów (Sentry / GlitchTip).
- [ ] 49. Lista kontrolna bezpieczeństwa produkcji.
- [ ] 50. Strona statusu i komunikacja przerw.
