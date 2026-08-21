Flusso "azienda strutturata" per questo task:
voglio lavorare (anche se sono sola) come in una grande azienda e devo per esempio spostare tutti i file creti per la valutazione dei kpi, nella cartella KPI e quindi modificare tutti i file ad esso collegati

```
git checkout main
git pull
git checkout -b refactor/riorganizza-cartella-kpi
```

Fai le modifiche (sposti i file, aggiorni i riferimenti nei file collegati), poi commit **a step logici**, non uno enorme:

```
git add "path/cartella KPI"
git commit -m "refactor: sposta file valutazione KPI nella cartella dedicata"

git add altri-file-modificati
git commit -m "refactor: aggiorna riferimenti ai file spostati"
```

Push del branch:

```
git push -u origin refactor/riorganizza-cartella-kpi
```

Poi vai su GitHub e apri una **Pull Request** da questo branch verso `main` (anche da soli, serve comunque a vedere il diff completo, controllare che nulla si sia rotto, e lasciare traccia del perché). Quando sei soddisfatta, fai il **merge** dalla PR su GitHub (bottone "Merge pull request").

Infine, in locale:

```
git checkout main
git pull
```

per riallineare `main` col merge appena fatto, ed eventualmente cancellare il branch:

```
git branch -d refactor/riorganizza-cartella-kpi
```