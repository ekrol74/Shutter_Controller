# Shutter_Controller
Open and close shutter by pulse with arduino and RFID

## subheader

Watch the tutorial

## Home Assistant: airco automatisch uit

`home_assistant/automations/airco_auto_uit.yaml` zet de airco uit wanneer
koelen geen zin meer heeft — bedoeld voor als het uitzetten wordt vergeten.

| Voorwaarde | Waarde |
| --- | --- |
| Ruimtetemperatuur | onder 23 °C |
| Buitentemperatuur | niet warmer dan 25 °C |
| Wachttijd | 90 minuten onafgebroken |

Zodra één van beide voorwaarden tussentijds wegvalt, begint de 90 minuten
opnieuw. Staat de airco in **droogstand** (`dry`), dan blijft hij draaien;
ontvochtigen is geen koelvraag en loopt net als de overige automatiseringen
gewoon door.

Vervang voor gebruik de drie placeholder-entity's boven in het bestand
(`climate.airco_luc`, `sensor.kamer_temperatuur_luc`,
`sensor.buiten_temperatuur`). De automatisering gebruikt de HA 2024.10+
syntax (`triggers` / `conditions` / `actions`).
