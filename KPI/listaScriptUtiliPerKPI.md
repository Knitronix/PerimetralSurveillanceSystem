
Aggiornamento: gli script sono stati spostati da MLtoDL/ a questa cartella (KPI/). Il motivo per
cui prima non potevano essere spostati (0registratoreStalta.py compilava kpi_calibration_log.jsonl
con un percorso relativo fisso) è stato risolto: ora ogni script calcola i propri percorsi in base
alla posizione del file (non alla cartella da cui viene lanciato), quindi funzionano da qui pur
leggendo dataset/modello da MLtoDL/.

sensor_integrity_verification.py  4.3
kpi_calibration_log.jsonl 4.1
curva_apprendimento.py 4.4
calcola_kpi.py 4.5 , 5.1 5.2 5.3, 5.5
valuta_modello.py KPI 7.1 KPI 7.6
