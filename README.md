# AirGrid Heatmap

Live tracker ruchu lotniczego nad Polską z mapą cieplną (heatmap) pokazującą, gdzie samoloty latają najczęściej.

## Co robi

- Nakłada siatkę na obszar Polski i śledzi na żywo pozycje samolotów
- Generuje mapę kolorów (heatmap) na podstawie zagęszczenia ruchu lotniczego w poszczególnych komórkach siatki
- Pozwala zobaczyć, które trasy/regiony mają największy ruch powietrzny

## Struktura projektu

```
.
├── app.py            # główna aplikacja
├── requirements.txt  # zależności Python
├── Dockerfile
├── data/              # dane runtime (pomijane w repo)
└── templates/         # szablony HTML
```

## Stack

- Python
- framework webowy 
- Dane o lotach z OpenSky

## Konfiguracja

Projekt wymaga pliku `.env` z kluczami/konfiguracją (nie jest dołączony do repo ze względów bezpieczeństwa). Utwórz plik `.env` w katalogu głównym na podstawie poniższego wzoru:

```
OPENSKY_CLIENT_ID=
OPENSKY_CLIENT_SECRET=

```

## Uruchomienie

```bash
docker build -t airgrid-heatmap .
docker run -p 5050:5000 --env-file .env airgrid-heatmap
```

Aplikacja dostępna pod `http://localhost:5050`.

## Status

Projekt hobbystyczny / do zabawy.
