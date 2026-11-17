"""
=====================================================================
 LogiServe S.r.l. - Gestionale ordini e magazzino
=====================================================================

ARCHITETTURA A TRE LIVELLI
    1. DATI         Archivio, Registro: gli unici che leggono e scrivono file.
    2. LOGICA       Magazzino, GestioneOrdini, genera_report: le regole di
                    business. Non usano input(), print() e non aprono file.
    3. INTERFACCIA  Menu e funzioni chiedi_*: solo dialogo con l'utente.
    La funzione crea_sistema() collega i tre livelli.

STATI DELL'ORDINE (vedi StatoOrdine e TRANSIZIONI)
    nuovo      registrato, evasione non ancora tentata
    in attesa  evasione tentata, ma nessuna merce disponibile
    parziale   evaso in parte: il residuo resta aperto
    evaso      completato: stato finale, non si modifica più

REGOLA SULLE QUANTITA'
    Si evade sempre quello che c'è (evasione parziale) e il resto rimane
    aperto sull'ordine. L'unico punto del codice che toglie merce è
    Magazzino.preleva(), che non scende mai sotto zero.

ISTRUZIONI PER L'USO
    python gestionale.py          avvia il programma
    python gestionale.py --test   esegue le prove automatiche
    I dati stanno nella cartella "dati" accanto allo script. Al primo avvio
    vengono creati con dati di esempio; per ripartire da zero basta
    cancellare la cartella "dati".
    Premere Invio senza scrivere nulla annulla l'operazione in corso e
    riporta al menu (dove indicato tra [ ] si usa invece il valore proposto).

SCENARI DI PROVA (partendo dai dati di esempio)
    1. Ordine completo: menu 2, evadere ORD-001 (2 pz di INT001).
       Stato EVASO; nel menu 4 la giacenza di INT001 passa da 40 a 38.
    2. Ordine parziale: menu 2, evadere ORD-002 (20 pz di PRE002, giacenza 8).
       Stato PARZIALE e PRE002 a 0 (ESAURITO). Menu 3: caricare 15 pz di
       PRE002, poi rievadere ORD-002: stato EVASO, giacenza 3.
    3. Punto di riordino: menu 1, ordinare 3 pz di INT002 (giacenza 12,
       riordino 10) ed evaderlo: giacenza 9 e avviso SOTTO RIORDINO.
    4. Giacenza zero: menu 1, ordinare 5 pz di FUS001 (giacenza 0): esito
       INDISPONIBILE; evadendolo lo stato diventa IN ATTESA.
    5. Persistenza: fare qualche operazione, uscire (0), riavviare e
       controllare che giacenze e ordini siano invariati.
"""

import json
import os
import shutil
import sys
import tempfile
from collections import Counter
from datetime import date, datetime
from enum import StrEnum

try:
    CARTELLA_SCRIPT = os.path.dirname(os.path.abspath(__file__))
except NameError:                        # es. Jupyter/Colab, dove __file__ non esiste
    CARTELLA_SCRIPT = os.getcwd()
CARTELLA_DATI = os.path.join(CARTELLA_SCRIPT, "dati")


# ===================================================================
# MODELLO - stati dell'ordine e regole comuni
# ===================================================================

class StatoOrdine(StrEnum):
    """Stati possibili di un ordine. Con StrEnum ogni stato è anche una
    stringa, quindi nel JSON viene salvato come "nuovo", "parziale", ..."""
    NUOVO = "nuovo"
    IN_ATTESA = "in attesa"
    PARZIALE = "parziale"
    EVASO = "evaso"


# Per ogni stato, gli stati in cui è permesso passare.
TRANSIZIONI = {
    StatoOrdine.NUOVO: {StatoOrdine.IN_ATTESA, StatoOrdine.PARZIALE, StatoOrdine.EVASO},
    StatoOrdine.IN_ATTESA: {StatoOrdine.IN_ATTESA, StatoOrdine.PARZIALE, StatoOrdine.EVASO},
    StatoOrdine.PARZIALE: {StatoOrdine.PARZIALE, StatoOrdine.EVASO},
    StatoOrdine.EVASO: set(),            # stato finale: nessuna uscita
}

PRIORITA = ["ALTA", "MEDIA", "BASSA"]    # indice 0 = più urgente


class ErroreGestionale(Exception):
    """Operazione rifiutata per dati non validi. Il messaggio è per l'utente."""


def stato_dopo_evasione(righe):
    """UNICO punto che decide lo stato in base alle quantità evase."""
    if all(riga["evasa"] == riga["quantita"] for riga in righe):
        return StatoOrdine.EVASO
    if any(riga["evasa"] > 0 for riga in righe):
        return StatoOrdine.PARZIALE
    return StatoOrdine.IN_ATTESA


# ===================================================================
# DATI DI ESEMPIO - usati solo se i file non esistono
# ===================================================================

def prodotto(codice, nome, categoria, giacenza, unita_misura, punto_riordino):
    return {
        "codice": codice,
        "nome": nome,
        "categoria": categoria,
        "giacenza": giacenza,
        "unita_misura": unita_misura,
        "punto_riordino": punto_riordino,
    }


def prodotti_iniziali():
    catalogo = [
        prodotto("CAV001", "Cavo unipolare 1,5 mmq", "Cavi", 500, "m", 100),
        prodotto("CAV002", "Cavo unipolare 2,5 mmq", "Cavi", 300, "m", 100),
        prodotto("CAV003", "Cavo multipolare 3x1,5", "Cavi", 120, "m", 50),
        prodotto("INT001", "Magnetotermico 16A", "Protezioni", 40, "pz", 10),    # scenario 1
        prodotto("INT002", "Differenziale 25A", "Protezioni", 12, "pz", 10),     # scenario 3
        prodotto("FUS001", "Fusibile 10A", "Protezioni", 0, "pz", 20),           # scenario 4
        prodotto("PRE001", "Presa Schuko", "Frutti", 80, "pz", 20),
        prodotto("PRE002", "Presa bipasso 10/16A", "Frutti", 8, "pz", 15),       # scenario 2
        prodotto("SCA001", "Scatola derivazione 100x100", "Accessori", 60, "pz", 15),
        prodotto("NAS001", "Nastro isolante", "Accessori", 25, "rotoli", 10),
    ]
    return {p["codice"]: p for p in catalogo}


def ordini_iniziali():
    """Due ordini pronti per gli scenari 1 (completo) e 2 (parziale)."""
    oggi = date.today().isoformat()
    return [
        {"id": "ORD-001", "cliente": "Officina Rossi", "data": oggi, "priorita": "MEDIA",
         "stato": StatoOrdine.NUOVO, "data_evasione": None,
         "prodotti": [{"codice": "INT001", "quantita": 2, "evasa": 0}]},
        {"id": "ORD-002", "cliente": "Elettro Bianchi", "data": oggi, "priorita": "ALTA",
         "stato": StatoOrdine.NUOVO, "data_evasione": None,
         "prodotti": [{"codice": "PRE002", "quantita": 20, "evasa": 0}]},
    ]


# ===================================================================
# LIVELLO LOGICA - nessun input(), print() o accesso ai file
# ===================================================================

class Magazzino:
    """Catalogo e giacenze. È l'unica classe che modifica le giacenze."""

    def __init__(self, prodotti, registro):
        self.prodotti = prodotti          # dizionario codice -> prodotto
        self.registro = registro          # per tracciare carichi e scarichi

    def trova(self, codice):
        codice = codice.strip().upper()
        if codice not in self.prodotti:
            raise ErroreGestionale(f"Il prodotto {codice} non esiste.")
        return self.prodotti[codice]

    def disponibilita(self, codice, quantita):
        """Confronta richiesta e giacenza senza modificare nulla."""
        giacenza = self.trova(codice)["giacenza"]
        if giacenza >= quantita:
            return "disponibile"
        if giacenza > 0:
            return "parziale"
        return "indisponibile"

    def preleva(self, codice, richiesta, riferimento):
        """REGOLA UNICA per l'uscita della merce: preleva il minimo tra
        richiesta e giacenza e restituisce la quantità prelevata.
        È l'unico punto che diminuisce una giacenza: non può andare sotto zero."""
        if richiesta <= 0:
            raise ErroreGestionale("La quantità da prelevare deve essere maggiore di zero.")
        prodotto = self.trova(codice)
        prelevata = min(richiesta, prodotto["giacenza"])
        if prelevata > 0:
            prodotto["giacenza"] -= prelevata
            self.registro.registra("SCARICO", prodotto["codice"], f"Evasione {riferimento}", -prelevata)
        return prelevata

    def carica(self, codice, quantita):
        """Arrivo di merce: aumenta la giacenza."""
        if quantita <= 0:
            raise ErroreGestionale("La quantità da caricare deve essere maggiore di zero.")
        prodotto = self.trova(codice)
        prodotto["giacenza"] += quantita
        self.registro.registra("CARICO", prodotto["codice"], "Arrivo merce", quantita)
        return prodotto

    def avviso_scorta(self, prodotto):
        if prodotto["giacenza"] == 0:
            return "ESAURITO"
        if prodotto["giacenza"] <= prodotto["punto_riordino"]:
            return "SOTTO RIORDINO"
        return ""

    def categorie(self):
        return sorted({p["categoria"] for p in self.prodotti.values()})

    def filtra(self, codice="", categoria="", soglia=None):
        """Filtri combinabili; un filtro vuoto non viene applicato."""
        risultato = []
        for p in self.prodotti.values():
            if codice and codice.strip().upper() not in p["codice"]:
                continue
            if categoria and categoria.strip().lower() != p["categoria"].lower():
                continue
            if soglia is not None and p["giacenza"] > soglia:
                continue
            risultato.append(p)
        return risultato


class GestioneOrdini:
    """Registrazione ed evasione degli ordini."""

    def __init__(self, ordini, magazzino, registro):
        self.ordini = ordini
        self.magazzino = magazzino
        self.registro = registro

    def prossimo_id(self):
        """Numero più alto già usato + 1: gli ID non si ripetono dopo un riavvio."""
        numeri = [int(o["id"][4:]) for o in self.ordini]      # "ORD-007" -> 7
        return f"ORD-{max(numeri, default=0) + 1:03d}"

    def trova(self, id_ordine):
        id_ordine = id_ordine.strip().upper()
        for ordine in self.ordini:
            if ordine["id"] == id_ordine:
                return ordine
        raise ErroreGestionale(f"L'ordine {id_ordine} non esiste.")

    def registra_ordine(self, cliente, righe, data_ordine, priorita):
        """righe: lista di (codice, quantità); data_ordine: AAAA-MM-GG.
        Restituisce (ordine, esito complessivo, esito per prodotto)."""
        cliente = cliente.strip()
        if not cliente:
            raise ErroreGestionale("Il nome del cliente è obbligatorio.")
        if priorita not in PRIORITA:
            raise ErroreGestionale(f"Priorità non valida: {priorita}.")
        if not righe:
            raise ErroreGestionale("L'ordine deve contenere almeno un prodotto.")

        quantita_per_codice = {}          # stesso codice inserito due volte: si sommano
        for codice, quantita in righe:
            codice = self.magazzino.trova(codice)["codice"]
            if quantita <= 0:
                raise ErroreGestionale(f"Quantità non valida per {codice}.")
            quantita_per_codice[codice] = quantita_per_codice.get(codice, 0) + quantita

        esiti = {c: self.magazzino.disponibilita(c, q) for c, q in quantita_per_codice.items()}
        if all(e == "disponibile" for e in esiti.values()):
            esito = "disponibile"
        elif all(e == "indisponibile" for e in esiti.values()):
            esito = "indisponibile"
        else:
            esito = "parziale"

        ordine = {
            "id": self.prossimo_id(),
            "cliente": cliente,
            "data": data_ordine,
            "priorita": priorita,
            "stato": StatoOrdine.NUOVO,
            "data_evasione": None,
            "prodotti": [{"codice": c, "quantita": q, "evasa": 0}
                         for c, q in quantita_per_codice.items()],
        }
        self.ordini.append(ordine)
        self.registro.registra("REGISTRAZIONE_ORDINE", ordine["id"],
                               f"Cliente {cliente}, disponibilità {esito}")
        return ordine, esito, esiti

    def evadi_ordine(self, id_ordine):
        """Evade tutto ciò che è disponibile.
        Restituisce (ordine, lista di (codice, quantità prelevata), avvisi di scorta)."""
        ordine = self.trova(id_ordine)
        if not TRANSIZIONI[ordine["stato"]]:
            raise ErroreGestionale(f"L'ordine {ordine['id']} è {ordine['stato']}: non si può evadere.")

        movimenti, avvisi = [], []
        for riga in ordine["prodotti"]:
            residuo = riga["quantita"] - riga["evasa"]
            if residuo == 0:
                continue
            prelevata = self.magazzino.preleva(riga["codice"], residuo, ordine["id"])
            riga["evasa"] += prelevata
            if prelevata > 0:
                movimenti.append((riga["codice"], prelevata))
                avviso = self.magazzino.avviso_scorta(self.magazzino.trova(riga["codice"]))
                if avviso:
                    avvisi.append(f"{riga['codice']}: {avviso}")

        self._cambia_stato(ordine, stato_dopo_evasione(ordine["prodotti"]))
        if ordine["stato"] == StatoOrdine.EVASO:
            ordine["data_evasione"] = date.today().isoformat()
        self.registro.registra("EVASIONE", ordine["id"], f"Stato risultante: {ordine['stato']}")
        return ordine, movimenti, avvisi

    def _cambia_stato(self, ordine, nuovo_stato):
        """Unico punto che assegna uno stato: controlla che la transizione sia ammessa."""
        if nuovo_stato not in TRANSIZIONI[ordine["stato"]]:
            raise ErroreGestionale(f"Passaggio non ammesso: da {ordine['stato']} a {nuovo_stato}.")
        ordine["stato"] = nuovo_stato

    def ordini_aperti(self):
        """Ordini non evasi, dal più urgente e più vecchio."""
        aperti = [o for o in self.ordini if TRANSIZIONI[o["stato"]]]
        return sorted(aperti, key=lambda o: (PRIORITA.index(o["priorita"]), o["data"], o["id"]))


def genera_report(giorno, gestione, magazzino, registro):
    """Dati del report di un giorno (AAAA-MM-GG), calcolati da ordini e log."""
    venduti = Counter()                   # codice -> quantità uscita
    variazioni = Counter()                # codice -> variazione netta di giacenza
    for voce in registro.del_giorno(giorno):
        if voce.get("quantita") is None:  # voce senza movimento di merce
            continue
        variazioni[voce["risorsa"]] += voce["quantita"]
        if voce["operazione"] == "SCARICO":
            venduti[voce["risorsa"]] -= voce["quantita"]     # gli scarichi sono negativi
    return {
        "data": giorno,
        "ricevuti": sum(1 for o in gestione.ordini if o["data"] == giorno),
        "evasi": sum(1 for o in gestione.ordini if o["data_evasione"] == giorno),
        "in_sospeso": len(gestione.ordini_aperti()),         # situazione attuale
        "piu_venduti": venduti.most_common(5),
        "variazioni": sorted(variazioni.items()),
        "da_riordinare": [(p["codice"], p["nome"], p["giacenza"], magazzino.avviso_scorta(p))
                          for p in magazzino.prodotti.values() if magazzino.avviso_scorta(p)],
    }


def testo_report(report):
    """Impagina il report: lo stesso testo va a video e nel file."""
    righe = ["=" * 55,
             f"REPORT GIORNALIERO LOGISERVE - {data_italiana(report['data'])}",
             "=" * 55,
             f"Ordini ricevuti         : {report['ricevuti']}",
             f"Ordini evasi            : {report['evasi']}",
             f"Ordini in sospeso (ora) : {report['in_sospeso']}",
             "", "Prodotti più venduti:"]
    righe += [f"  {c:<8} {q:>6}" for c, q in report["piu_venduti"]] or ["  nessuna vendita"]
    righe += ["", "Variazione giacenze:"]
    righe += [f"  {c:<8} {v:>+6}" for c, v in report["variazioni"]] or ["  nessun movimento"]
    righe += ["", "Prodotti da riordinare:"]
    righe += [f"  {c:<8} {n:<28} {g:>6}  {a}" for c, n, g, a in report["da_riordinare"]] or ["  nessuno"]
    return "\n".join(righe)


# ===================================================================
# LIVELLO DATI - lettura e scrittura dei file
# ===================================================================

def _intero(valore, minimo):
    """True se valore è un numero intero (non True/False) maggiore o uguale a minimo."""
    return type(valore) is int and valore >= minimo


def _testo(valore):
    return isinstance(valore, str) and valore.strip() != ""


def valida_prodotto(record):
    """Restituisce il prodotto ripulito, oppure None se un campo manca o è sbagliato."""
    if not isinstance(record, dict):
        return None
    if not all(_testo(record.get(campo)) for campo in ("codice", "nome", "categoria", "unita_misura")):
        return None
    if not (_intero(record.get("giacenza"), 0) and _intero(record.get("punto_riordino"), 0)):
        return None                       # giacenze negative rifiutate già in lettura
    return prodotto(record["codice"].strip().upper(), record["nome"], record["categoria"],
                    record["giacenza"], record["unita_misura"], record["punto_riordino"])


def valida_ordine(record, prodotti):
    """Restituisce l'ordine ripulito, oppure None se è malformato o incoerente."""
    try:
        id_ordine = record["id"]
        if not (id_ordine.startswith("ORD-") and id_ordine[4:].isdigit()):
            return None
        if not _testo(record["cliente"]) or record["priorita"] not in PRIORITA:
            return None
        date.fromisoformat(record["data"])                # errore se la data non esiste
        stato = StatoOrdine(record["stato"])              # errore se lo stato è sconosciuto
        if record["data_evasione"] is not None:
            date.fromisoformat(record["data_evasione"])
        righe = []
        for riga in record["prodotti"]:
            if riga["codice"] not in prodotti:
                return None
            if not (_intero(riga["quantita"], 1) and _intero(riga["evasa"], 0)):
                return None
            if riga["evasa"] > riga["quantita"]:
                return None
            righe.append({"codice": riga["codice"], "quantita": riga["quantita"], "evasa": riga["evasa"]})
    except (KeyError, TypeError, ValueError, AttributeError):
        return None                       # campo mancante o del tipo sbagliato
    if not righe:
        return None
    # Lo stato salvato deve essere coerente con le quantità evase.
    if stato == StatoOrdine.NUOVO:
        coerente = all(r["evasa"] == 0 for r in righe)
    else:
        coerente = stato == stato_dopo_evasione(righe)
    if not coerente:
        return None
    return {"id": id_ordine, "cliente": record["cliente"], "data": record["data"],
            "priorita": record["priorita"], "stato": stato,
            "data_evasione": record["data_evasione"], "prodotti": righe}


class Archivio:
    """Carica e salva prodotti e ordini in JSON. Non fa mai crashare il programma:
    i problemi trovati finiscono nella lista self.avvisi."""

    def __init__(self, cartella):
        os.makedirs(cartella, exist_ok=True)
        self.cartella = cartella
        self.file_prodotti = os.path.join(cartella, "prodotti.json")
        self.file_ordini = os.path.join(cartella, "ordini.json")
        self.file_log = os.path.join(cartella, "log_operazioni.jsonl")
        self.avvisi = []                  # messaggi da mostrare all'utente

    def _copia_di_sicurezza(self, percorso, motivo, sposta):
        """Conserva una copia del file problematico, così nessun dato va perso."""
        copia = f"{percorso}.{motivo}-{datetime.now():%Y%m%d-%H%M%S}"
        (os.replace if sposta else shutil.copyfile)(percorso, copia)
        return os.path.basename(copia)

    def _leggi_lista(self, percorso):
        """Contenuto del file come lista, oppure None se manca, è vuoto o è illeggibile."""
        nome = os.path.basename(percorso)
        if not os.path.exists(percorso):
            self.avvisi.append(f"{nome} non trovato: creato con dati di esempio.")
            return None
        try:
            with open(percorso, encoding="utf-8") as file:
                testo = file.read()
            if not testo.strip():
                self.avvisi.append(f"{nome} era vuoto: ricreato con dati di esempio.")
                return None
            dati = json.loads(testo)
            if not isinstance(dati, list):
                raise ValueError("il file non contiene un elenco")
            return dati
        except (OSError, ValueError):
            copia = self._copia_di_sicurezza(percorso, "corrotto", sposta=True)
            self.avvisi.append(f"{nome} era danneggiato (copia in {copia}): uso i dati di esempio.")
            return None

    def _scrivi_json(self, percorso, dati):
        """Scrittura sicura: prima su un file temporaneo, poi sostituzione in un colpo solo."""
        temporaneo = percorso + ".tmp"
        with open(temporaneo, "w", encoding="utf-8") as file:
            json.dump(dati, file, indent=2, ensure_ascii=False)
        os.replace(temporaneo, percorso)

    def _carica_elenco(self, percorso, valida, crea_esempio):
        """Schema comune: legge, valida record per record, scarta e segnala i record rotti."""
        dati = self._leggi_lista(percorso)
        if dati is None:
            elenco = crea_esempio()
            self._scrivi_json(percorso, elenco)
            return elenco
        elenco, scartati = [], 0
        for record in dati:
            pulito = valida(record, elenco)
            if pulito is None:
                scartati += 1
            else:
                elenco.append(pulito)
        if scartati:
            copia = self._copia_di_sicurezza(percorso, "originale", sposta=False)
            self.avvisi.append(f"{os.path.basename(percorso)}: {scartati} record non validi ignorati "
                               f"(file originale conservato in {copia}).")
        return elenco

    def carica_prodotti(self):
        def valida(record, gia_letti):
            p = valida_prodotto(record)
            if p is None or any(x["codice"] == p["codice"] for x in gia_letti):
                return None               # malformato o codice duplicato
            return p
        elenco = self._carica_elenco(self.file_prodotti, valida,
                                     lambda: list(prodotti_iniziali().values()))
        return {p["codice"]: p for p in elenco}

    def carica_ordini(self, prodotti):
        def valida(record, gia_letti):
            o = valida_ordine(record, prodotti)
            if o is None or any(x["id"] == o["id"] for x in gia_letti):
                return None               # malformato o ID duplicato
            return o
        return self._carica_elenco(self.file_ordini, valida, ordini_iniziali)

    def carica_log(self):
        """Legge lo storico (una voce JSON per riga). Le righe rotte vengono saltate."""
        voci, scartate = [], 0
        if not os.path.exists(self.file_log):
            return voci
        with open(self.file_log, encoding="utf-8", errors="replace") as file:
            for riga in file:
                if not riga.strip():
                    continue
                try:
                    voce = json.loads(riga)
                    if not (_testo(voce["timestamp"]) and _testo(voce["operazione"])):
                        raise ValueError
                    voci.append(voce)
                except (ValueError, KeyError, TypeError):
                    scartate += 1
        if scartate:
            self.avvisi.append(f"log_operazioni.jsonl: {scartate} righe illeggibili ignorate.")
        return voci

    def salva(self, magazzino, gestione):
        self._scrivi_json(self.file_prodotti, list(magazzino.prodotti.values()))
        self._scrivi_json(self.file_ordini, gestione.ordini)

    def salva_report(self, giorno, testo):
        percorso = os.path.join(self.cartella, f"report_{giorno}.txt")
        with open(percorso, "w", encoding="utf-8") as file:
            file.write(testo)
        return percorso


class Registro:
    """Storico append-only: ogni voce viene aggiunta in fondo al file e mai modificata."""

    def __init__(self, percorso, operatore, voci):
        self.percorso = percorso
        self.operatore = operatore
        self.voci = voci                  # voci già presenti, caricate all'avvio

    def registra(self, operazione, risorsa, dettaglio, quantita=None):
        """quantita: positiva per i carichi, negativa per gli scarichi."""
        voce = {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "operatore": self.operatore,
            "operazione": operazione,
            "risorsa": risorsa,
            "dettaglio": dettaglio,
            "quantita": quantita,
        }
        self.voci.append(voce)
        with open(self.percorso, "a", encoding="utf-8") as file:    # "a" = aggiungi in fondo
            file.write(json.dumps(voce, ensure_ascii=False) + "\n")

    def del_giorno(self, giorno):
        return [v for v in self.voci if v["timestamp"].startswith(giorno)]


def crea_sistema(cartella, operatore):
    """Collega dati e logica. Usata sia dal programma sia dai test."""
    archivio = Archivio(cartella)
    registro = Registro(archivio.file_log, operatore, archivio.carica_log())
    magazzino = Magazzino(archivio.carica_prodotti(), registro)
    gestione = GestioneOrdini(archivio.carica_ordini(magazzino.prodotti), magazzino, registro)
    return archivio, registro, magazzino, gestione


# ===================================================================
# LIVELLO INTERFACCIA - input sicuri e menu
# ===================================================================

def data_italiana(data_iso):
    """'2026-11-16' -> '16/11/2026'."""
    return date.fromisoformat(data_iso).strftime("%d/%m/%Y")


def chiedi_testo(domanda):
    """Testo inserito, oppure None se l'utente preme solo Invio (= annulla)."""
    return input(domanda).strip() or None


def chiedi_intero(domanda, minimo=1):
    """Intero >= minimo; richiede di nuovo finché non è valido. Invio = None."""
    while True:
        risposta = input(domanda).strip()
        if risposta == "":
            return None
        try:
            numero = int(risposta)
        except ValueError:
            numero = None
        if numero is not None and numero >= minimo:
            return numero
        print(f"  Serve un numero intero da {minimo} in su (es. 5). Invio per annullare.")


def chiedi_data(domanda):
    """Data GG/MM/AAAA; Invio = oggi. Restituisce AAAA-MM-GG."""
    while True:
        risposta = input(domanda).strip()
        if risposta == "":
            return date.today().isoformat()
        try:
            return datetime.strptime(risposta, "%d/%m/%Y").date().isoformat()
        except ValueError:
            print("  Data non valida: usa GG/MM/AAAA (es. 16/11/2026) oppure Invio per oggi.")


def chiedi_priorita():
    """Priorità per numero o per nome; Invio = MEDIA."""
    while True:
        risposta = input("Priorità 1=ALTA 2=MEDIA 3=BASSA [Invio = MEDIA]: ").strip().upper()
        if risposta == "":
            return "MEDIA"
        if risposta in ("1", "2", "3"):
            return PRIORITA[int(risposta) - 1]
        if risposta in PRIORITA:
            return risposta
        print("  Scegli 1, 2 o 3 (oppure scrivi alta, media, bassa).")


def conferma(domanda):
    return input(f"{domanda} (s/N): ").strip().lower() == "s"


class Menu:
    """Strato sottile: chiede i dati, chiama la logica, mostra il risultato."""

    def __init__(self, archivio, registro, magazzino, gestione):
        self.archivio = archivio
        self.registro = registro
        self.magazzino = magazzino
        self.gestione = gestione

    def avvia(self):
        voci = {
            "1": ("Registra nuovo ordine", self.registra_ordine),
            "2": ("Evadi ordine", self.evadi_ordine),
            "3": ("Carica merce in magazzino", self.carica_merce),
            "4": ("Consulta magazzino", self.consulta_magazzino),
            "5": ("Elenco ordini", self.elenco_ordini),
            "6": ("Report giornaliero", self.report_giornaliero),
            "7": ("Ultime operazioni (log)", self.visualizza_log),
        }
        while True:
            print("\n========== LOGISERVE - MENU PRINCIPALE ==========")
            for tasto, (descrizione, _) in voci.items():
                print(f"  {tasto}. {descrizione}")
            print("  0. Esci")
            try:
                scelta = input("Scelta: ").strip()
            except (KeyboardInterrupt, EOFError):
                scelta = "0"
            if scelta == "0":
                break
            if scelta not in voci:
                print("Scelta non valida: digita un numero da 0 a 7.")
                continue
            try:
                voci[scelta][1]()
            except ErroreGestionale as errore:          # dati non validi: messaggio chiaro
                print(f"\nATTENZIONE: {errore}")
            except (KeyboardInterrupt, EOFError):       # Ctrl+C durante un'operazione
                print("\nOperazione annullata.")
            except Exception as errore:                 # imprevisto: lo registriamo e si continua
                self.registro.registra("ERRORE", "programma", repr(errore))
                print(f"\nSi è verificato un problema imprevisto: {errore}")
            self.salva()                                # salvataggio dopo ogni operazione
        self.salva()
        self.registro.registra("USCITA", "programma", "Fine sessione")
        print("Dati salvati. Arrivederci!")

    def salva(self):
        try:
            self.archivio.salva(self.magazzino, self.gestione)
        except OSError as errore:
            print(f"ATTENZIONE: impossibile salvare i dati ({errore}).")

    def stampa_ordine(self, ordine):
        print(f"{ordine['id']}  {data_italiana(ordine['data'])}  {ordine['cliente']:<20} "
              f"{ordine['priorita']:<6} {ordine['stato'].upper()}")
        for riga in ordine["prodotti"]:
            print(f"      {riga['codice']:<8} richiesti {riga['quantita']:>5}   evasi {riga['evasa']:>5}")

    def registra_ordine(self):
        print("\n--- NUOVO ORDINE (Invio a vuoto per annullare) ---")
        cliente = chiedi_testo("Cliente: ")
        if cliente is None:
            print("Operazione annullata.")
            return
        righe = []
        print("Inserisci i prodotti (codice vuoto per terminare l'elenco).")
        while True:
            codice = chiedi_testo("  Codice prodotto: ")
            if codice is None:
                break
            try:
                p = self.magazzino.trova(codice)
            except ErroreGestionale as errore:
                print(f"  {errore} Riprova.")
                continue
            quantita = chiedi_intero(f"  Quantità di '{p['nome']}' "
                                     f"(disponibili {p['giacenza']} {p['unita_misura']}): ")
            if quantita is None:
                print("  Riga saltata.")
                continue
            righe.append((p["codice"], quantita))
        if not righe:
            print("Nessun prodotto inserito: ordine annullato.")
            return
        data_ordine = chiedi_data("Data GG/MM/AAAA [Invio = oggi]: ")
        priorita = chiedi_priorita()
        if not conferma(f"Registrare l'ordine di {cliente} ({len(righe)} righe)?"):
            print("Ordine annullato.")
            return
        ordine, esito, esiti = self.gestione.registra_ordine(cliente, righe, data_ordine, priorita)
        print(f"\nOrdine {ordine['id']} registrato. Disponibilità: {esito.upper()}")
        for codice, esito_riga in esiti.items():
            print(f"  {codice}: {esito_riga}")

    def evadi_ordine(self):
        aperti = self.gestione.ordini_aperti()
        if not aperti:
            print("Non ci sono ordini da evadere.")
            return
        print("\n--- ORDINI DA EVADERE (per priorità e data) ---")
        for ordine in aperti:
            self.stampa_ordine(ordine)
        id_ordine = chiedi_testo("ID ordine da evadere (Invio per annullare): ")
        if id_ordine is None:
            return
        ordine, movimenti, avvisi = self.gestione.evadi_ordine(id_ordine)
        for codice, quantita in movimenti:
            p = self.magazzino.trova(codice)
            print(f"  Prelevati {quantita} {p['unita_misura']} di {codice}; giacenza ora {p['giacenza']}")
        print(f"Ordine {ordine['id']}: stato {ordine['stato'].upper()}")
        if ordine["stato"] == StatoOrdine.IN_ATTESA:
            print("Nessun prodotto disponibile: l'ordine resta in attesa di merce.")
        for avviso in avvisi:
            print(f"  ATTENZIONE scorta - {avviso}")

    def carica_merce(self):
        codice = chiedi_testo("Codice prodotto da caricare (Invio per annullare): ")
        if codice is None:
            return
        p = self.magazzino.trova(codice)
        quantita = chiedi_intero(f"Quantità da caricare ({p['unita_misura']}): ")
        if quantita is None:
            return
        self.magazzino.carica(codice, quantita)
        print(f"Nuova giacenza di {p['codice']}: {p['giacenza']} {p['unita_misura']}")

    def consulta_magazzino(self):
        print("\n--- CONSULTA MAGAZZINO (Invio = nessun filtro) ---")
        codice = input("Codice (anche parziale): ").strip()
        categoria = input(f"Categoria ({', '.join(self.magazzino.categorie())}): ").strip()
        soglia = chiedi_intero("Solo prodotti con giacenza fino a: ", minimo=0)
        prodotti = self.magazzino.filtra(codice, categoria, soglia)
        if not prodotti:
            print("Nessun prodotto corrisponde ai filtri.")
            return
        print(f"\n{'CODICE':<8} {'NOME':<28} {'CATEGORIA':<11} {'GIAC.':>6} {'RIORD.':>6} {'U.M.':<7} AVVISO")
        for p in prodotti:
            print(f"{p['codice']:<8} {p['nome']:<28} {p['categoria']:<11} {p['giacenza']:>6} "
                  f"{p['punto_riordino']:>6} {p['unita_misura']:<7} {self.magazzino.avviso_scorta(p)}")

    def elenco_ordini(self):
        print("\n--- ELENCO ORDINI ---")
        if not self.gestione.ordini:
            print("Nessun ordine registrato.")
        for ordine in self.gestione.ordini:
            self.stampa_ordine(ordine)

    def report_giornaliero(self):
        giorno = chiedi_data("Data del report GG/MM/AAAA [Invio = oggi]: ")
        testo = testo_report(genera_report(giorno, self.gestione, self.magazzino, self.registro))
        print("\n" + testo)
        if conferma("\nEsportare il report in un file di testo?"):
            percorso = self.archivio.salva_report(giorno, testo)
            self.registro.registra("EXPORT_REPORT", os.path.basename(percorso), "Report giornaliero")
            print(f"Creato: {percorso}")

    def visualizza_log(self):
        print("\n--- ULTIME 20 OPERAZIONI ---")
        for v in self.registro.voci[-20:]:
            quantita = "" if v.get("quantita") is None else f"({v['quantita']:+})"
            print(f"{v['timestamp']}  {str(v.get('operatore')):<10} {v['operazione']:<21} "
                  f"{str(v.get('risorsa')):<10} {v.get('dettaglio', '')} {quantita}")


# ===================================================================
# TEST AUTOMATICI - python gestionale.py --test
# ===================================================================

def esegui_test():
    """Ripete gli scenari di prova su una cartella temporanea (i dati veri non si toccano)."""
    oggi = date.today().isoformat()

    def deve_fallire(azione):
        try:
            azione()
        except ErroreGestionale:
            return
        raise AssertionError("un'operazione non valida è stata accettata")

    with tempfile.TemporaryDirectory() as cartella:
        archivio, registro, magazzino, gestione = crea_sistema(cartella, "test")
        giacenza = lambda codice: magazzino.trova(codice)["giacenza"]

        ordine, _, _ = gestione.evadi_ordine("ORD-001")
        assert ordine["stato"] == StatoOrdine.EVASO and giacenza("INT001") == 38
        print("OK  1. ordine evaso completamente")

        ordine, _, avvisi = gestione.evadi_ordine("ORD-002")
        assert ordine["stato"] == StatoOrdine.PARZIALE and giacenza("PRE002") == 0
        assert avvisi == ["PRE002: ESAURITO"]
        magazzino.carica("PRE002", 15)
        ordine, _, _ = gestione.evadi_ordine("ORD-002")
        assert ordine["stato"] == StatoOrdine.EVASO and giacenza("PRE002") == 3
        print("OK  2. ordine parziale, poi completato dopo il carico")

        ordine, esito, _ = gestione.registra_ordine("Test", [("INT002", 3)], oggi, "MEDIA")
        assert esito == "disponibile"
        _, _, avvisi = gestione.evadi_ordine(ordine["id"])
        assert giacenza("INT002") == 9 and avvisi == ["INT002: SOTTO RIORDINO"]
        print("OK  3. superamento del punto di riordino")

        ordine, esito, _ = gestione.registra_ordine("Test", [("fus001", 5)], oggi, "BASSA")
        assert esito == "indisponibile"
        ordine, movimenti, _ = gestione.evadi_ordine(ordine["id"])
        assert ordine["stato"] == StatoOrdine.IN_ATTESA and movimenti == [] and giacenza("FUS001") == 0
        print("OK  4. giacenza zero: ordine in attesa")

        deve_fallire(lambda: gestione.evadi_ordine("ORD-001"))                      # già evaso
        deve_fallire(lambda: gestione.registra_ordine("Test", [("XXX999", 1)], oggi, "MEDIA"))
        deve_fallire(lambda: gestione.registra_ordine("Test", [("CAV001", 0)], oggi, "MEDIA"))
        deve_fallire(lambda: gestione.registra_ordine("  ", [("CAV001", 1)], oggi, "MEDIA"))
        deve_fallire(lambda: magazzino.preleva("CAV001", -5, "test"))
        assert all(p["giacenza"] >= 0 for p in magazzino.prodotti.values())
        print("OK  5. operazioni non valide rifiutate, nessuna giacenza negativa")

        report = genera_report(oggi, gestione, magazzino, registro)
        assert report["ricevuti"] == 4 and report["evasi"] == 3 and report["in_sospeso"] == 1
        assert report["piu_venduti"][0] == ("PRE002", 20)
        print("OK  6. report giornaliero")

        archivio.salva(magazzino, gestione)
        with open(archivio.file_ordini, encoding="utf-8") as file:
            prima = file.read()
        archivio2, registro2, magazzino2, gestione2 = crea_sistema(cartella, "test")
        assert magazzino2.prodotti == magazzino.prodotti
        assert gestione2.ordini == gestione.ordini
        assert registro2.voci == registro.voci
        assert gestione2.prossimo_id() == "ORD-005"
        archivio2.salva(magazzino2, gestione2)
        with open(archivio.file_ordini, encoding="utf-8") as file:
            assert file.read() == prima       # salvare di nuovo non cambia nulla
        print("OK  7. salvataggio e ricaricamento identici")

    with tempfile.TemporaryDirectory() as cartella:
        archivio = Archivio(cartella)
        open(archivio.file_prodotti, "w").close()                                   # file vuoto
        with open(archivio.file_ordini, "w", encoding="utf-8") as file:
            json.dump(ordini_iniziali()[:1] + [{"id": "ORD-002", "stato": "boh"}, "spazzatura"], file)
        with open(archivio.file_log, "w", encoding="utf-8") as file:
            file.write('{"timestamp": "2026-01-01 10:00:00", "operazione": "AVVIO"}\nriga rotta\n')
        archivio, registro, magazzino, gestione = crea_sistema(cartella, "test")
        assert len(magazzino.prodotti) == 10
        assert [o["id"] for o in gestione.ordini] == ["ORD-001"]
        assert len(registro.voci) == 1 and len(archivio.avvisi) == 3

        with open(archivio.file_prodotti, "w", encoding="utf-8") as file:
            file.write("{{{ non è JSON")                                             # file corrotto
        archivio, _, magazzino, _ = crea_sistema(cartella, "test")
        assert len(magazzino.prodotti) == 10
        assert any(nome.startswith("prodotti.json.corrotto") for nome in os.listdir(cartella))
        print("OK  8. file vuoti, corrotti o con record malformati gestiti senza crash")

    print("\nTutti i test superati.")


# ===================================================================
# AVVIO
# ===================================================================

def main():
    if "--test" in sys.argv[1:]:
        esegui_test()
        return
    print("Benvenuto nel gestionale LogiServe.")
    try:
        operatore = None
        while operatore is None:
            operatore = chiedi_testo("Nome operatore: ")
    except (KeyboardInterrupt, EOFError):
        print("\nUscita.")
        return
    archivio, registro, magazzino, gestione = crea_sistema(CARTELLA_DATI, operatore)
    for avviso in archivio.avvisi:
        print(f"  {avviso}")
    registro.registra("AVVIO", "programma", "Inizio sessione")
    Menu(archivio, registro, magazzino, gestione).avvia()


if __name__ == "__main__":
    main()
