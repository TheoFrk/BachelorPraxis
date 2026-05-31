## Woher kommt dieser Sprung? in s2

![image-20260525082210001](/Users/theo_frk/Library/Application Support/typora-user-images/image-20260525082210001.png)

Ich habe nur gelesen, nichts geändert.

Der große Sprung bei `S2 lexicon-only` kommt fast komplett von `ABVX.PA / ABIVAX SA` am `2025-07-22`.

In `s2_vs_s1_daily` springt die Matched-Equity an diesem Datum von `1.2396` auf `1.3652`, weil der Tagesreturn `+10.13%` ist. Davon liefert ABIVAX allein ca. `+8.65` Prozentpunkte:

`ABVX.PA ret_fwd_1d = 5.1011` = `+510.1%`  
Gewicht im Portfolio: `1/59`  
Beitrag: `5.1011 / 59 = 0.08646`

Die zugrunde liegenden Preisdaten zeigen den Sprung direkt:

`2025-07-22 adjusted_close = 8.90`  
`2025-07-23 adjusted_close = 54.30`

Wichtig: Das war **kein Lexikon-News-Signal**. Für ABIVAX gab es an dem Datum `has_lex_news = 0`, `n_lex_articles = 0`, `lex_score_raw = 0`. ABIVAX kam trotzdem rein, weil bei `NEUTRAL_FILL` fehlende News als `0` gesetzt werden und dieser `0`-Wert nach Tages-Z-Score positiv war (`lex_component = 0.1221`). Bei `291` Kandidaten werden `ceil(20% * 291) = 59` Titel genommen; ABIVAX war genau Rang `59`.

Ohne ABIVAX läge der Tagesreturn nicht bei `+10.13%`, sondern nur bei ungefähr `+1.49%` bis `+1.51%`. Die `EVENT_ONLY`-Variante enthält ABIVAX an dem Tag nicht und hat nur `+1.46%`.

Kurz: Der Sprung ist ein einzelner extremer ABIVAX-Kursmove, der durch `lexicon-only + neutral fill + Top-20%-Cutoff` zufällig ins Portfolio geraten ist, nicht durch ein positives Lexikon-Sentiment-Signal.



Das Ereignis war **positive Phase-3-Daten von Abivax für Obefazimod bei Colitis ulcerosa**.

Abivax hat am **22. Juli 2025 um 22:05 CEST**, also nach Börsenschluss in Paris, positive Topline-Ergebnisse aus den beiden Phase-3-Induktionsstudien `ABTECT-1` und `ABTECT-2` gemeldet. Am nächsten Handelstag, **23. Juli 2025**, ist die Aktie deshalb von `8.90 EUR` auf `54.30 EUR` gesprungen.

Rechnerisch genau der Sprung in deinen Daten:

```text
54.30 / 8.90 - 1 = 5.1011 = +510.1%
```

Und weil `S2 lexicon-only` an `2025-07-22` für den Folgetag `ret_fwd_1d` nutzt, landet dieser Kursmove als Rendite am Datum `2025-07-22` im Backtest.

Warum das im S2-Lexikonmodell passiert:

```text
ABVX.PA Gewicht im Portfolio: 1/59
ABVX.PA Rendite: +510.1%
Beitrag zur Tagesrendite: 510.1% / 59 = +8.65 Prozentpunkte
Gesamter S2-Tagesreturn: +10.13%
Ohne ABVX: ca. +1.5%
```

Wichtig: Das Lexikonmodell hat dieses Ereignis **nicht erkannt**. In deiner lokalen News-Tabelle gibt es für ABIVAX bis zu diesem Zeitpunkt keine News; `has_lex_news = 0`, `n_lex_articles = 0`, `lex_score_raw = 0`. ABIVAX kam nur durch `NEUTRAL_FILL` und den Top-20%-Cutoff ins Portfolio: der neutrale `0`-Lexikonwert wurde an diesem Tag z-standardisiert leicht positiv (`lex_component = 0.1221`) und ABIVAX war exakt Rang `59` von `59` selektierten Titeln.

Quelle für das Ereignis: Abivax meldete die positiven Phase-3-Ergebnisse am 22. Juli 2025 nach Börsenschluss; BioWorld berichtet, dass die Paris-Aktie am 23. Juli um `€45.40` bzw. `510%` auf `€54.30` stieg.  
[Abivax Pressemitteilung](https://ir.abivax.com/news-releases/news-release-details/abivax-announces-positive-phase-3-results-both-abtect-8-week) · [BioWorld Meldung](https://www.bioworld.com/newsletters/1/issues/38527)



ABIVAX wurde in `S2 lexicon-only` gewählt, weil diese Variante **nur den Lexikon-Score** rankt und den extrem schlechten S1-Price-Score komplett ignoriert.

Am `2025-07-22`:

```text
S2 lexicon-only:
ABIVAX lex_component = 0.1221
Rang = 59
Top-N = 59
=> gerade noch drin
```

Das war aber kein echtes positives Lexikon-Signal:

```text
has_lex_news = 0
n_lex_articles = 0
lex_score_raw = 0
```

Durch `NEUTRAL_FILL` bekommt ABIVAX also einen neutralen Rohwert `0`. Weil der Tagesdurchschnitt der Lexikonwerte leicht negativ war, wird dieser neutrale `0` nach Z-Standardisierung leicht positiv (`0.1221`). Danach gab es viele gleiche neutrale Scores; der Code sortiert bei Gleichstand nach `security_id`. ABIVAX hat `security_id = 66` und landete dadurch exakt auf dem letzten ausgewählten Platz `59`.

Warum nicht in den anderen Hauptvarianten:

```text
S1 price-only:
ABIVAX S1 score = -4.7593
Rang = 290 von 291
Cutoff = Rang 59
=> nicht drin
```

Der S1-Score war so schlecht, weil ABIVAX vor dem Kurssprung aus Price-Sicht extrem unattraktiv aussah: schwache 12-1 Momentum-Komponente und sehr hohe Volatilität/Low-Vol-Strafe.

```text
S2 Hybrid 50/50:
score = 0.5 * S1 + 0.5 * Lex
      = 0.5 * -4.7593 + 0.5 * 0.1221
      = -2.3186
Rang = 174
=> nicht drin
```

```text
S3 FinBERT-only:
FinBERT component = -0.0467
Rang = 119
Top-N = 58
=> nicht drin
```

```text
S3 Hybrid:
score = -2.4030
Rang = 289
=> nicht drin
```

Bei S4 gibt es eine wichtige Korrektur: `S4_LLM_ONLY_EN_NEUTRAL_FILL` hat ABIVAX **auch** gewählt:

```text
S4 LLM-only:
score = 0.0
Rang = 42
Top-N = 58
=> drin
Tagesreturn S4 LLM-only am 2025-07-22: +10.46%
```

Die S4-Hybride haben ABIVAX dagegen nicht gewählt, weil dort der negative S1-Anteil wieder greift:

```text
S4 LLM 50/50:
score = -2.3797
Rang = 284
=> nicht drin

S4 LLM 70/30:
score = -3.3315
Rang = 285
=> nicht drin
```

Kurz: ABIVAX kommt nur in reinen Text-/Neutral-Fill-Modellen rein, die den schlechten Price-Score ignorieren. In `S2 lexicon-only` war es ein knapper Gleichstands-/Cutoff-Effekt; in den hybriden und S1-basierten Varianten verhindert der sehr negative S1-Score die Auswahl.



