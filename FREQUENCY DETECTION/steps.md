PULIRE PER RIPARTIRE DA ZERO
registratore di ML TO DL
togli tutto tranne la parte di interrogatore e visualizzazione. (voglio solo ascoltare e vedere)


poi indagare su:

- sapendo in che formato mi arriva un segnale 
- sapendo che io volutamente genero due segnali a frequenze definite e a intervalli definiti
qual è il miglior METODO DI RICONOSCIMENTO per 2 frequenze contemporaneamente:
non a quanto sono ma:
- ci sono? 
-per quanti secondi quanto durano?

L'OBIETTIVO è: data f1
se f1 dura t0 allora sto contando lo 0
se f1 dura t1 sto contando i successivi dopo lo zero, in modo incrementale (da 1 a 100 per esempio)

contemporaneamente POTREBBE arrivare f2 di durata t1

io devo
contare gli f1
scrivere le coppie di punti (f1,f2) dove il SIGNIFICATO è
avendo 100 interruttori
f1 mi serve come counter per sincronizzarmi col mio hardware che genera frequenze
f2 invece mi va a dire se un interruttore è chiuso o aperto
quindi (f1,f2)=(numero di interruttore, aperto / chiuso) con logica se aperto f2 non è arrivata, se chiuso f2 è arrivata




FARE ANCHE CODICE DI ARDUINO CHE
lancia 
f1 lunga t0= 300ms e poi lancia f1 lunga t1= 150ms
contemporaneamente lancia f2 lunga t1 ma solo quando anche f1 è lunga t1 altrimenti f2 non viene lanciata

f1 --- - - - - - - - - - - 
f2     -     -     - - -   

nell'esempio sopra solamente gli interruttori n 1,4,7,8,9 sono stati chiusi dato che
solo le f2 corrispondenti sono presenti
