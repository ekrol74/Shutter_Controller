# tools/rdi_callsign_check.py

Controleert welke roepletters nog vrij zijn in het callbook van de RDI
(<https://callbook.rdi.nl/>). Standaard wordt de hele reeks **PD0A t/m PD9Z**
doorlopen: derde teken `0-9`, vierde teken `A-Z`, dus 10 × 26 = 260 combinaties.

Alleen Python 3.8+ en de standaardbibliotheek — geen `pip install` nodig.

## Waarom je eerst één request uit de browser kopieert

Het callbook is een JavaScript-applicatie zonder gepubliceerde, stabiele API.
Een hard-gecodeerd endpoint in dit script zou bij de eerste de beste wijziging
van de site stilletjes verkeerde antwoorden geven. Daarom herhaalt het script
het verzoek dat de site zelf doet:

1. Zoek in de browser één call op, bijvoorbeeld je eigen call.
2. DevTools (F12) → tabblad **Network** → klik op het zoek-request →
   rechtermuisknop → **Copy** → **Copy as cURL (bash)**.
3. Plak dat in een bestand, bijvoorbeeld `request.curl`.

Het script vervangt die voorbeeldcall in URL, headers én body door elke te
controleren call.

## Gebruik

Eerst kijken wat er zou gebeuren, zonder iets te versturen:

```bash
python3 tools/rdi_callsign_check.py --curl-file request.curl --sample-call PD1ABC --dry-run
```

Dan de echte run, met een ijkslag vooraf:

```bash
python3 tools/rdi_callsign_check.py \
  --curl-file request.curl \
  --sample-call PD1ABC \
  --calibrate-taken PD1ABC \
  --calibrate-free PD9ZZ9 \
  --delay 2 \
  --output rdi_callsigns.csv
```

`--calibrate-taken` / `--calibrate-free` doen eerst één bekende bezette en één
bekende vrije call. Komt de uitkomst niet overeen met de verwachting, dan stopt
het script meteen — zo ontdek je een verkeerde herkenning na 2 aanvragen in
plaats van na 260.

Ken je het endpoint al, dan kan het ook zonder cURL-bestand, met `{call}` als
plaatshouder in URL, body of header:

```bash
python3 tools/rdi_callsign_check.py --url 'https://callbook.rdi.nl/api/search?callsign={call}'
```

En om de site te inspecteren op formulieren, invoervelden en endpoints die in de
JavaScript-bundels staan:

```bash
python3 tools/rdi_callsign_check.py --discover
```

## Uitkomsten

Per call wordt één van vier statussen weggeschreven naar de CSV
(`callsign,status,http_status,checked_at,detail`):

| status | betekenis |
| --- | --- |
| `vrij` | niet in het callbook gevonden |
| `bezet` | er is een registratie gevonden |
| `onbekend` | antwoord niet eenduidig te classificeren |
| `fout` | HTTP- of netwerkfout |

Bij twijfel wordt bewust `onbekend` gerapporteerd in plaats van een gok. Herken
het script de antwoorden nog niet goed, pas dan `--free-regex` / `--taken-regex`
aan; met `--dump-dir dumps` bewaart het de ruwe antwoorden zodat je kunt zien
welke tekst de site precies teruggeeft.

De CSV wordt regel voor regel weggeschreven en bij een volgende run worden
calls die al `vrij` of `bezet` zijn overgeslagen. Een onderbroken run (Ctrl-C,
netwerk weg) pak je dus op door het commando opnieuw te draaien; met
`--recheck` doe je alles alsnog over.

## Nette omgang met de site

- `--delay 2` (standaard) met `--jitter 0.3`: één aanvraag per ~2 seconden,
  serieel. De hele reeks duurt daarmee ongeveer 10 minuten.
- `robots.txt` wordt vooraf gecontroleerd; staat het pad niet toe, dan stopt het
  script (bewust te overrulen met `--ignore-robots`).
- Tijdelijke fouten (429, 5xx, timeouts) worden met oplopende wachttijd
  herkanst, inclusief `Retry-After`.
- Na 10 fouten op rij stopt het script (`--max-consecutive-errors`).

Zet `--delay` niet lager; het callbook is een publieke dienst en 260 aanvragen
zijn niet urgent.

## Kleinere reeksen

```bash
--digits 0-3 --letters A-F   # alleen PD0A..PD3F
--digits 024 --letters AQZ   # losse tekens mogen ook
--prefix PA                  # andere prefix
--limit 10                   # stop na 10 calls
```

## Voorbehoud

"Vrij" betekent hier alleen: **niet gevonden in het callbook**. Het zegt niets
over calls die gereserveerd, recent ingetrokken of nog niet verwerkt zijn — het
RDI-callbook loopt soms achter op nieuwe registraties. De RDI bepaalt bij de
aanvraag of je een call daadwerkelijk kunt krijgen.
