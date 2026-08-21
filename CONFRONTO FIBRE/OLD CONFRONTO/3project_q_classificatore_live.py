import sys
import socket
import numpy as np
import os
import pickle
from PySide6 import QtCore, QtWidgets
import pyqtgraph as pg

UDP_IP = "0.0.0.0"
UDP_PORT = 12345
CAMPIONI_PER_PACCHETTO = 254  
SAMPLE_RATE = 24000  
SECONDI_VISIBILI = 15  
STORICO_CAMPIONI = SAMPLE_RATE * SECONDI_VISIBILI  
FILE_MODELLO = "modello_ia_project_q.pkl"

class NetworkWorker(QtCore.QThread):
    nuovi_dati_signal = QtCore.Signal(np.ndarray)

    def __init__(self, ip, port, packet_size):
        super().__init__()
        self.ip = ip
        self.port = port
        self.packet_size = packet_size
        self.running = True

    def run(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 2 * 1024 * 1024)
        sock.bind((self.ip, self.port))
        sock.settimeout(0.5)
        bytes_attesi = self.packet_size * 2

        while self.running:
            try:
                data, addr = sock.recvfrom(1024)
                if len(data) == bytes_attesi:
                    nuovi_campioni = np.frombuffer(data, dtype=np.int16)
                    self.nuovi_dati_signal.emit(nuovi_campioni)
            except socket.timeout:
                continue
        sock.close()

class DASAutomaticClassifier(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        pg.setConfigOptions(antialias=False)
        
        self.buffer_audio = np.zeros(STORICO_CAMPIONI, dtype=np.int16)
        
        if not os.path.exists(FILE_MODELLO):
            print(f"❌ File modello '{FILE_MODELLO}' non trovato! Controlla di aver lanciato addestratore_ia.py")
            sys.exit(1)
            
        with open(FILE_MODELLO, 'rb') as f:
            compilato = pickle.load(f)
            self.modello_ia = compilato["modello"]
            self.num_bins = compilato["num_bins"]
            
        print("🧠 [ENGINE] Modello predittivo IA caricato con successo.")

        self.soglia_trigger_globale = 2000.0 
        self.sto_ascoltando_impulso = False
        self.buffer_impulso = []
        self.campioni_finestra = int(SAMPLE_RATE * 1.5)

        # --- INTERFACCIA GRAFICA ---
        self.setWindowTitle("Project Q - DAS Fully Automatic Classifier (Optimized)")
        self.resize(1000, 550)
        
        central_widget = QtWidgets.QWidget()
        self.setCentralWidget(central_widget)
        layout_principale = QtWidgets.QHBoxLayout(central_widget)
        
        # Grafico (Colonna Sinistra)
        colonna_sinistra = QtWidgets.QVBoxLayout()
        self.plot_widget = pg.PlotWidget()
        self.plot_widget.setBackground('#111111')
        self.plot_widget.showGrid(x=True, y=True, alpha=0.2)
        self.data_line = self.plot_widget.plot(self.buffer_audio, pen=pg.mkPen(color='#00d2c4', width=1.5))
        
        self.linea_soglia_grafica = pg.InfiniteLine(angle=0, movable=False, pen=pg.mkPen(color='#ff4757', width=1.5, style=QtCore.Qt.DashLine))
        self.linea_soglia_grafica.setValue(self.soglia_trigger_globale)
        self.plot_widget.addItem(self.linea_soglia_grafica)
        
        self.limite_y = 20000.0
        self.plot_widget.setYRange(-self.limite_y, self.limite_y, padding=0)
        self.plot_widget.setXRange(0, STORICO_CAMPIONI, padding=0)
        colonna_sinistra.addWidget(self.plot_widget)
        layout_principale.addLayout(colonna_sinistra, stretch=4)
        
        # Pannello di Controllo (Colonna Destra)
        colonna_destra = QtWidgets.QVBoxLayout()
        colonna_destra.setContentsMargins(10, 10, 10, 10)
        
        titolo = QtWidgets.QLabel("AUTOMATIC DAS INTELLIGENCE")
        titolo.setStyleSheet("font-weight: bold; font-size: 14px; color: #00d2c4;")
        colonna_destra.addWidget(titolo)
        
        colonna_destra.addWidget(QtWidgets.QLabel("Soglia di Sveglia Generale (RMS):"))
        self.spin_soglia = QtWidgets.QSpinBox()
        self.spin_soglia.setRange(100, 50000)
        self.spin_soglia.setValue(int(self.soglia_trigger_globale))
        self.spin_soglia.setSingleStep(250)
        self.spin_soglia.valueChanged.connect(self.aggiorna_soglia_globale)
        colonna_destra.addWidget(self.spin_soglia)
        
        self.label_rms_live = QtWidgets.QLabel("RMS Corrente: 0")
        self.label_rms_live.setStyleSheet("color: #aaaaaa; font-family: monospace;")
        colonna_destra.addWidget(self.label_rms_live)
        
        colonna_destra.addSpacing(40)
        
        colonna_destra.addWidget(QtWidgets.QLabel("EVENTO RILEVATO DALL'IA VIA FIBRA:"))
        
        # --- BLOCCO UNIFICATO E IMMUTABILE PER IL DISPLAY IA ---
        self.label_risultato_ia = QtWidgets.QLabel("SISTEMA IN ASCOLTO...")
        self.label_risultato_ia.setAlignment(QtCore.Qt.AlignCenter)
        self.label_risultato_ia.setWordWrap(True)         # Evita clipping su testi lunghi
        self.label_risultato_ia.setFixedSize(360, 180)    # Dimensione blindata (Niente più oscillazioni della GUI)
        self.label_risultato_ia.setStyleSheet(
            "background-color: #1a1a1a; color: #00d2c4; font-size: 20px; font-weight: bold; "
            "border: 2px solid #333333; padding: 15px; border-radius: 5px;"
        )
        colonna_destra.addWidget(self.label_risultato_ia)
        colonna_destra.addStretch()
        layout_principale.addLayout(colonna_destra, stretch=1)
        
        self.worker = NetworkWorker(UDP_IP, UDP_PORT, CAMPIONI_PER_PACCHETTO)
        self.worker.nuovi_dati_signal.connect(self._gestisci_nuovi_campioni)
        self.worker.start()
        
        self.timer = QtCore.QTimer()
        self.timer.setInterval(50)
        self.timer.timeout.connect(self.update_graph)
        self.timer.start()

    def aggiorna_soglia_globale(self, valore):
        self.soglia_trigger_globale = float(valore)
        self.linea_soglia_grafica.setValue(valore)

    @QtCore.Slot(np.ndarray)
    def _gestisci_nuovi_campioni(self, nuovi_campioni):
        self.buffer_audio = np.roll(self.buffer_audio, -CAMPIONI_PER_PACCHETTO)
        self.buffer_audio[-CAMPIONI_PER_PACCHETTO:] = nuovi_campioni
        
        rms_pacchetto = np.sqrt(np.mean(nuovi_campioni.astype(float) ** 2))
        
        if not self.sto_ascoltando_impulso:
            self.label_rms_live.setText(f"RMS Corrente: {int(rms_pacchetto)}")
        
        if rms_pacchetto > self.soglia_trigger_globale and not self.sto_ascoltando_impulso:
            self.sto_ascoltando_impulso = True
            self.buffer_impulso = list(nuovi_campioni)
            self.label_risultato_ia.setText("⚡ ANALISI SEGNALE...")
            self.label_risultato_ia.setStyleSheet(
                "background-color: #221100; color: #ffa502; font-size: 20px; font-weight: bold; "
                "border: 2px solid #ffa502; padding: 15px; border-radius: 5px;"
            )
        
        elif self.sto_ascoltando_impulso:
            self.buffer_impulso.extend(nuovi_campioni)
            if len(self.buffer_impulso) >= self.campioni_finestra:
                self.esegui_predizione_ia()
                self.sto_ascoltando_impulso = False
                self.buffer_impulso = []

    def esegui_predizione_ia(self):
        vettore_dati = np.array(self.buffer_impulso, dtype=np.int16)[:self.campioni_finestra]
        
        fft_vettore = np.abs(np.fft.rfft(vettore_dati))
        frequenze = np.fft.rfftfreq(len(vettore_dati), d=1.0/SAMPLE_RATE)
        idx_8k = np.where(frequenze <= 8000)[0][-1]
        fft_tagliata = fft_vettore[:idx_8k]
        
        blocchi = np.array_split(fft_tagliata, self.num_bins)
        features = [np.mean(b) if len(b) > 0 else 0.0 for b in blocchi]
        features = np.array(features)
        max_val = np.max(features)
        if max_val > 0: features = features / max_val
        
        features_2d = features.reshape(1, -1)
        classe_predetta = self.modello_ia.predict(features_2d)[0]
        probabilita = self.modello_ia.predict_proba(features_2d)[0]
        sicurezza = np.max(probabilita) * 100
        
        colori_evento = {'BATTITO_MANI': '#ff4757', 'FISCHIO': '#1e90ff', 'RUMORE_AMBIENTALE': '#aaaaaa'}
        colore_testo = colori_evento.get(classe_predetta, '#00ff00')
        
        testo_display = f"🎯 RILEVATO:\n{classe_predetta}\n({sicurezza:.1f}%)"
        print(f"[LIVE] Rilevato {classe_predetta} ({sicurezza:.1f}%)")
        
        stile = (
            f"background-color: #051a10; color: {colore_testo}; font-size: 20px; font-weight: bold; "
            f"border: 2px solid {colore_testo}; padding: 15px; border-radius: 5px;"
        )
        self.label_risultato_ia.setText(testo_display)
        self.label_risultato_ia.setStyleSheet(stile)

    def update_graph(self):
        self.data_line.setData(self.buffer_audio)

    def closeEvent(self, event):
        self.timer.stop()
        self.worker.running = False
        self.worker.wait()
        event.accept()

if __name__ == '__main__':
    app = QtWidgets.QApplication(sys.argv)
    app.setStyle('Fusion')
    viewer = DASAutomaticClassifier()
    viewer.show()
    sys.exit(app.exec())