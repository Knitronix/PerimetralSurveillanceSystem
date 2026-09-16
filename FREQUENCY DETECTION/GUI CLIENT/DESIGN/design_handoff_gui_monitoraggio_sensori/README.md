# Handoff: GUI Monitoraggio Sensori (Knitronix)

## Overview
GUI desktop per monitorare in tempo reale una catena di 10 sensori di pressione collegati in serie da un unico cavo. Attualmente solo i primi 2 sensori sono fisicamente collegati; gli altri 8 sono predisposti per un collegamento futuro. Pensata per restare aperta su un monitor/tablet, comprensibile a un utente non tecnico.

## About the Design Files
I file in questo pacchetto (`design.html`, screenshot) sono **riferimenti di design creati in HTML** — mockup che mostrano aspetto e comportamento previsti, non codice di produzione da copiare. Il compito è **ricreare questo design nell'ambiente Python/PySide6 dell'app desktop**, usando i widget e i pattern nativi di Qt (QFrame, QLabel, QTimer, Signal/Slot), non incorporare HTML.

## Fidelity
**Alta fedeltà (hifi)**: colori, layout, tipografia e testi sono definitivi. Ricreare pixel-per-pixel dove possibile con i widget Qt.

## Screens / Views
Una sola schermata (finestra principale).

### Header
- Layout: riga flex, logo a sinistra + blocco testo a destra, allineati verticalmente al centro, gap 18px.
- Logo: `logo-knitronix.png`, altezza 64px, larghezza proporzionale.
- Titolo: "Sistema di monitoraggio" — uppercase, weight 800, 26px, colore `#1A1A1A`, letter-spacing 0.5px.
- Sottotitolo: "Catena di 10 sensori — i primi 2 collegati, gli altri 8 in attesa di collegamento" — 14px, colore `#6B6560`.
- Separatore: bordo inferiore 3px solido `#1A1A1A`, padding-bottom 18px sotto la riga.

### Catena di sensori
- Contenitore: `position: relative`, margin-top 40px, padding-top 28px.
- Cavo: linea orizzontale continua, altezza 4px, colore `#1A1A1A`, posizionata dietro le card (a ~88px dal top del contenitore), che attraversa tutta la larghezza della fila (inset 20px sui lati).
- Card sensori: riga flex orizzontale, gap 14px, `overflow-x: auto` se non entrano tutte (scroll orizzontale su schermi piccoli), allineate in basso (`align-items: flex-end`).
- Ogni sensore ha un piccolo "spinotto" verticale (2px larghezza, 16px altezza, colore `#1A1A1A`) sopra il cavo che lo collega visivamente.
- Sotto la fila: caption 12px colore `#6B7370`: "— cavo unico che collega la catena di sensori".

#### Card sensore ATTIVO (Sensore 1 e 2 — i soli fisicamente collegati)
- Larghezza card: 140px, padding 22px, border-radius 16px, border 1px `#DCD8D4`, box-shadow leggera (`0 3px 16px rgba(0,0,0,0.05)`).
- Nome: "Sensore N", uppercase, weight 700, 16px, colore `#1A1A1A`.
- Blocco colore stato: rettangolo 100px × 150px, border-radius 18px, colore secondo stato (vedi Design Tokens).
- Etichetta stato: weight 700, 15px, colore = colore dello stato.
  - Stato "Libero": colore `#1A1A1A`, sfondo card `#E9E7E5`.
  - Stato "Rilevata pressione": colore `#E13A2E`, sfondo card `#FBE2DF`.
- Timestamp: 11px, colore `#6B7370`, testo "Ultimo cambio: HH:MM:SS" (aggiornato ad ogni cambio di stato).

#### Card sensore NON COLLEGATO (Sensore 3–10)
- Più piccola: larghezza 100px, padding 14px, blocco colore 64px × 96px.
- Nome 13px, etichetta stato 12px.
- Colore fisso grigio `#BFBAB6`, sfondo card `#EDEBE9`, opacità intera card 0.55.
- Etichetta stato: testo fisso "Non collegato". Timestamp: "—" (nessun timestamp, non hanno mai cambiato stato).

### Pannello di test/simulazione
- Card con border tratteggiato `#BFBAB6`, sfondo bianco, border-radius 14px, padding 14px 20px, layout flex con gap 16px, wrap.
- Etichetta "Simulazione (solo per test):" uppercase, 12px, weight 700, colore `#6B6560`.
- Bottone "Commuta Sensore 1": sfondo nero `#1A1A1A`, testo bianco, weight 700, border-radius 10px, padding 8px 16px.
- Bottone "Commuta Sensore 2": sfondo rosso `#E13A2E`, stesso stile.
- Questo pannello è SOLO per generare eventi di test con dati simulati; va rimosso o nascosto quando si collegano i sensori reali. Nell'app Python il pattern equivalente (bottoni di test + logica sostituibile) è già implementato in `alarm_monitor_gui.py` incluso nel pacchetto — riusare quella struttura, applicando i nuovi stili/layout qui descritti.

## Interactions & Behavior
- Ogni sensore attivo può cambiare stato Libero ↔ Rilevata pressione; al cambio si aggiorna sia il colore del blocco/etichetta sia il timestamp.
- I sensori non collegati sono statici, non reagiscono a input, restano sempre grigi/opachi.
- Nessuna animazione richiesta: il cambio di stato è istantaneo (nessuna transizione da implementare, ma è accettabile un fade rapido se il framework lo offre gratuitamente).
- Scroll orizzontale sulla fila di sensori se la finestra è troppo stretta per contenerli tutti.

## State Management
- Stato per sensore: `id`, `attivo: bool` (collegato fisicamente o no — per ora sensori 1-2 = true, 3-10 = false), `stato: "libero" | "allarme"`, `ultimo_cambio: datetime`.
- La sorgente dati è per ora simulata (vedi `SimulatedAlarmSource` in `alarm_monitor_gui.py`): esporrà un segnale `stato_cambiato(mat_id, nuovo_stato)` e un metodo `imposta_stato(mat_id, stato)`. La futura sorgente reale (lettura seriale/GPIO/rete dal sistema di allarme) dovrà implementare la stessa interfaccia così l'attacco alla UI è immediato.

## Design Tokens

**Colori**
- Nero brand / testo principale / stato "Libero": `#1A1A1A`
- Rosso brand / stato "Rilevata pressione": `#E13A2E`
- Grigio "non collegato": `#BFBAB6`
- Sfondo pagina: `#F7F5F4`
- Sfondo card libero: `#E9E7E5`
- Sfondo card allarme: `#FBE2DF`
- Sfondo card non collegato: `#EDEBE9`
- Bordo card: `#DCD8D4`
- Testo secondario: `#6B7370` / `#6B6560`

**Tipografia**
- Famiglia: Helvetica / Arial (sans-serif)
- Titolo pagina: 26px, weight 800, uppercase
- Nome sensore attivo: 16px, weight 700, uppercase
- Nome sensore non collegato: 13px, weight 700, uppercase
- Etichetta stato attivo: 15px, weight 700
- Etichetta stato non collegato: 12px, weight 700
- Timestamp/caption: 11–14px, weight 400

**Border radius**: card 16px, blocco colore 18px, bottoni 10px.

**Ombre**: card attive `0 3px 16px rgba(0,0,0,0.05)`.

## Assets
- `logo-knitronix.png` — logo aziendale Knitronix (fornito dal cliente), da usare esattamente come da file, senza ricolorare.

## Files
- `design.html` — mockup HTML interattivo del design (riferimento visivo, cliccabile per testare i cambi di stato).
- `logo-knitronix.png` — asset logo.
- `alarm_monitor_gui.py` — prototipo PySide6 già aggiornato all'ultima versione del design (10 sensori, palette rosso/nero/bianco, cavo di collegamento, logo in header). Eseguibile as-is come riferimento di partenza per la struttura del codice (classi, segnali, simulazione); il disegno del cavo con QPainter è indicativo e può essere raffinato.
