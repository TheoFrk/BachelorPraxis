# Lesbarkeitsreview Kapitel-4-Grafiken

Erstellt am: 2026-05-30  
Gepruefte Dateien:

- `images/ch4_coverage_overview.png`
- `images/ch4_main_equity_curves.png`
- `images/ch4_main_equity_curves_compact.png`
- `images/ch4_main_equity_curves_hybrids_only.png`

## Kurzfazit

Die Coverage-Grafik ist fuer den Haupttext geeignet. Die Hauptgrafik der Equity-Kurven ist methodisch sauber und durch die 2x2-Struktur lesbar, wirkt aber fuer A4 dicht, weil sie neun verschiedene Serien in einer Abbildung erklaert. Deshalb wurden zwei Zusatzgrafiken erzeugt: eine kompakte Hauptsignal-Grafik und eine Hybrid-only-Grafik. Fuer den Haupttext ist die kompakte Grafik die klarste Alternative, falls die 2x2-Hauptgrafik kleiner als ca. Seitenbreite gesetzt wird.

## Pruefkriterien

| Kriterium | Status | Befund | Empfehlung |
|---|---|---|---|
| Achsentitel vollstaendig sichtbar | PASS | Programmatische Bounding-Box-Pruefung zeigt ausreichende Randbereiche; y- und x-Achsentitel sind in allen PNGs sichtbar. | Keine Aenderung noetig. |
| Direktlabels oder Legenden ueberlappen | WARN | Keine harten Ueberlappungen. In der Hauptgrafik liegen die Hybrid-Endlabels in Panel C/D dicht beieinander; Leader-Lines und Zusatzgrafiken reduzieren die Mehrdeutigkeit. | Hauptgrafik nur ausreichend gross setzen; fuer Detaildiskussion die Hybrid-only-Grafik nutzen. |
| Linienstile in Graustufen unterscheidbar | PASS | S1 ist dick schwarz, Benchmark rot gestrichelt; S3 wurde dash-dot, S4 gepunktet, Hybride haben unterschiedliche Strichmuster. | Farbcodierung beibehalten, aber nicht allein darauf verlassen. |
| Maximal 4 Linien pro Panel | PASS | Hauptgrafik: Panel A=2, B=4, C=4, D=4. Compact: Panel A=4, B=3. Hybrids-only: Panel A=4, B=4. | Kriterium erfuellt. |
| S1 und Benchmark eindeutig erkennbar | PASS | S1 ist dunkel, dick und direkt beschriftet; Benchmark ist gestrichelt und direkt beschriftet. | Keine Aenderung noetig. |
| Text-only, Hybrid und Referenz nicht vermischt | PASS | Hauptgrafik trennt Referenz, Text-only, Hybride und S4-Gewichtungsrobustheit in eigene Panels. Die Zusatzgrafiken behalten diese Trennung bei. | Keine Aenderung noetig. |
| y-Achse ueber Panels vergleichbar | PASS | Hauptgrafik und Zusatzgrafiken verwenden jeweils eine gemeinsame y-Skala innerhalb der Abbildung. | In der Bildunterschrift erwaehnen, dass je Panel matched dates genutzt werden. |
| A4-Lesbarkeit | WARN | Schriftgroessen und Linien sind grundsaetzlich A4-tauglich. Die Hauptgrafik ist aber wegen langer Legende und vielen Serien dicht. | Hauptgrafik in voller Textbreite setzen oder compact als Hauptabbildung verwenden; hybrids-only als Ergaenzung/Anhang. |

## Programmatische Pruefung

| Datei | Pixelgroesse | DPI | Randbefund |
|---|---:|---:|---|
| `ch4_coverage_overview.png` | 2157 x 2436 | 300 | keine sichtbare Beschneidung |
| `ch4_main_equity_curves.png` | 2177 x 1971 | 300 | keine sichtbare Beschneidung |
| `ch4_main_equity_curves_compact.png` | 2176 x 1270 | 300 | keine sichtbare Beschneidung |
| `ch4_main_equity_curves_hybrids_only.png` | 2175 x 1270 | 300 | keine sichtbare Beschneidung |

## Konkrete Verbesserungen umgesetzt

- `scripts/create_ch4_figures.py` erzeugt nun zusaetzlich `ch4_main_equity_curves_compact.png`.
- `scripts/create_ch4_figures.py` erzeugt nun zusaetzlich `ch4_main_equity_curves_hybrids_only.png`.
- Die Hauptgrafik nutzt staerkere Graustufen-Unterscheidung: S3 als Dash-Dot-Linie und S4 als gepunktete Linie.
- Die Zusatzgrafiken vermeiden mehr als vier Linien pro Panel und trennen Text-only-Strategien von Hybridvergleichen.

## Empfehlung fuer die Bachelorarbeit

Primaer geeignet:

- `ch4_coverage_overview.png`
- `ch4_main_equity_curves_compact.png`, wenn die Abbildung im Haupttext knapp gesetzt wird

Geeignet bei voller Seitenbreite:

- `ch4_main_equity_curves.png`

Geeignet als Ergaenzung oder Anhang:

- `ch4_main_equity_curves_hybrids_only.png`

