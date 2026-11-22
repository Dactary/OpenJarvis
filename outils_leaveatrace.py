# outils_leaveatrace.py : supervision d'une application Laravel distante par
# OpenJarvis (ici LeaveATrace), exposée par le protocole MCP.
#
# Deux rôles :
#   1. des outils que le modèle appelle à la demande (« comment va
#      LeaveATrace ? », « quoi de neuf cette semaine ? ») ;
#   2. une veille en tâche de fond qui annonce à voix haute les nouvelles
#      anomalies et les nouveautés (inscriptions, capsules ouvertes...).
#
# Tout passe par SSH, avec deux commandes fixes et en lecture seule côté
# serveur : « php artisan health:check --json » et « php artisan
# activite:resume --json ». Ce script ne peut rien modifier sur le serveur.
#
# Réglages (hors dépôt) : ~/.openjarvis/leaveatrace.json
#   {
#     "serveur": {"hote": "utilisateur@adresse", "cle": "~/.ssh/ma-cle",
#                 "chemin": "/var/www/.../leave-a-trace-api"},
#     "veille": {"minutes": 15, "silence": [22, 8],
#                "nouveautes": ["inscriptions", "capsules_ouvertes"]}
#   }
#
# Test hors OpenJarvis :
#   python outils_leaveatrace.py --essai leaveatrace_etat
#   python outils_leaveatrace.py --essai leaveatrace_activite periode=7j
#   python outils_leaveatrace.py --veille-une-fois     (un passage, sans parler)
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime

import outils_quotidien as base

FICHIER = base.DOSSIER / "leaveatrace.json"
NOM = "LeaveATrace"

VEILLE_DEFAUT = {"minutes": 15, "silence": [22, 8],
                 "nouveautes": ["inscriptions", "capsules_ouvertes"]}
RAPPEL_ANOMALIE_S = 6 * 3600      # une anomalie qui dure est redite au plus toutes les 6 h
ECHECS_AVANT_ALERTE = 2           # passages ratés d'affilée avant « serveur injoignable »
PORT_VERROU = 8772                # une seule veille à la fois sur cette machine
JOURNAL_MAX = 30

# Nom parlé de chaque anomalie renvoyée par health:check.
ANOMALIES = {
    "livraison": "leaves non livrés",
    "jobs_echoues": "tâches en échec",
    "jobs_en_attente": "file de tâches bloquée",
    "disque": "disque presque plein",
    "miniatures": "photos sans miniature",
    "videos": "vidéos non converties",
    "sauvegarde": "sauvegarde trop ancienne",
    "injoignable": "serveur injoignable",
}

# Compteurs d'activité : (singulier, pluriel).
COMPTEURS = {
    "inscriptions": ("nouvelle inscription", "nouvelles inscriptions"),
    "leaves_crees": ("leave créé", "leaves créés"),
    "leaves_envoyes": ("leave envoyé", "leaves envoyés"),
    "leaves_ouverts": ("leave ouvert", "leaves ouverts"),
    "capsules_creees": ("capsule créée", "capsules créées"),
    "capsules_ouvertes": ("capsule ouverte", "capsules ouvertes"),
    "contributions": ("contribution à une capsule", "contributions aux capsules"),
}

_VERROU = threading.Lock()


# ------------------------------------------------------------------ réglages

def lire_fichier():
    try:
        donnees = json.loads(FICHIER.read_text(encoding="utf-8"))
        return donnees if isinstance(donnees, dict) else {}
    except (OSError, ValueError):
        return {}


def ecrire_fichier(donnees):
    base.DOSSIER.mkdir(parents=True, exist_ok=True)
    provisoire = FICHIER.with_suffix(".tmp")
    provisoire.write_text(json.dumps(donnees, ensure_ascii=False, indent=2), encoding="utf-8")
    provisoire.replace(FICHIER)


def modifier_etat(fonction):
    """Relit, modifie et réécrit la partie « etat » du fichier, sous verrou."""
    with _VERROU:
        donnees = lire_fichier()
        etat = donnees.setdefault("etat", {})
        resultat = fonction(etat)
        ecrire_fichier(donnees)
        return resultat


def reglages_veille():
    veille = dict(VEILLE_DEFAUT)
    veille.update({k: v for k, v in (lire_fichier().get("veille") or {}).items() if k in VEILLE_DEFAUT})
    return veille


# ----------------------------------------------------------------------- SSH

class Injoignable(Exception):
    pass


def _ssh(commande_artisan):
    """Lance une commande artisan fixe sur le serveur et renvoie son JSON."""
    serveur = lire_fichier().get("serveur") or {}
    hote, chemin = serveur.get("hote"), serveur.get("chemin")
    if not hote or not chemin:
        raise Injoignable(f"réglages du serveur absents de {FICHIER}")
    if not re.fullmatch(r"[\w./-]+", chemin):
        raise Injoignable("chemin du serveur invalide dans les réglages")
    ligne = ["ssh", "-n", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
             "-o", "StrictHostKeyChecking=accept-new"]
    if serveur.get("cle"):
        ligne += ["-i", os.path.expanduser(serveur["cle"])]
    ligne += [hote, f"cd {chemin} && php artisan {commande_artisan}"]
    try:
        sortie = subprocess.run(
            ligne, capture_output=True, timeout=45,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired) as erreur:
        raise Injoignable(type(erreur).__name__) from erreur
    texte = sortie.stdout.decode("utf-8", errors="replace")
    for brut in reversed(texte.splitlines()):
        if brut.lstrip().startswith("{"):
            try:
                return json.loads(brut)
            except ValueError:
                break
    raise Injoignable(f"réponse inattendue du serveur (code {sortie.returncode})")


def lire_sante():
    reponse = _ssh("health:check --json")
    anomalies = reponse.get("anomalies")
    # PHP encode un tableau vide en [] et non en {}.
    return anomalies if isinstance(anomalies, dict) else {}


def lire_activite(depuis=None):
    commande = "activite:resume --json"
    if depuis and re.fullmatch(r"[0-9T:+.Z-]{10,40}", depuis):
        commande += f" --depuis={depuis}"
    return _ssh(commande)


# -------------------------------------------------------------------- phrases

def nom_anomalie(cle):
    return ANOMALIES.get(cle, cle.replace("_", " "))


def detail_anomalie(message):
    """Garde le constat chiffré, retire la consigne technique (« — php artisan... »)."""
    return re.split(r"\s+[—–-]\s+", str(message), maxsplit=1)[0].replace("(s)", "s").replace("(e)", "e")


def compter_en_clair(compteurs, cles=None):
    morceaux = []
    for cle, (un, plusieurs) in COMPTEURS.items():
        if cles is not None and cle not in cles:
            continue
        n = int(compteurs.get(cle) or 0)
        if n:
            morceaux.append(f"{n} {un if n == 1 else plusieurs}")
    if len(morceaux) > 1:
        return ", ".join(morceaux[:-1]) + " et " + morceaux[-1]
    return morceaux[0] if morceaux else ""


# --------------------------------------------------------------------- outils

def leaveatrace_etat():
    try:
        anomalies = lire_sante()
    except Injoignable as erreur:
        return f"Je n'arrive pas à joindre le serveur {NOM} ({erreur})."
    if not anomalies:
        return f"{NOM} va bien : aucune anomalie détectée."
    details = " ; ".join(detail_anomalie(m) for m in anomalies.values())
    n = len(anomalies)
    return f"{NOM} a {n} anomalie{'s' if n > 1 else ''} : {details}."


def leaveatrace_activite(periode="24h"):
    fenetre = "7j" if str(periode or "").strip().lower() in ("7j", "7 j", "semaine", "7") else "24h"
    try:
        resume = lire_activite()
    except Injoignable as erreur:
        return f"Je n'arrive pas à joindre le serveur {NOM} ({erreur})."
    quand = "ces 7 derniers jours" if fenetre == "7j" else "ces dernières 24 heures"
    activite = compter_en_clair(resume.get(fenetre) or {})
    total = (f"{resume.get('utilisateurs', 0)} utilisateurs inscrits au total, "
             f"dont {resume.get('actifs_7j', 0)} actifs sur 7 jours")
    if not activite:
        return f"Aucune activité sur {NOM} {quand}. {total.capitalize()}."
    return f"Sur {NOM} {quand} : {activite}. {total.capitalize()}."


def leaveatrace_alertes():
    etat = lire_fichier().get("etat") or {}
    journal = etat.get("journal") or []
    dernier = etat.get("dernier_passage_local")
    veille = reglages_veille()
    suivi = (f"La veille passe toutes les {veille['minutes']} minutes"
             + (f", dernier passage à {dernier}" if dernier else ", pas encore de passage") + ".")
    attente = etat.get("en_attente") or []
    if attente:
        suivi += f" {len(attente)} annonce(s) gardée(s) pour la fin des heures calmes."
    if not journal:
        return f"Aucune alerte {NOM} récente. {suivi}"
    lignes = [f"{e.get('quand', '?')} : {e.get('texte', '')}" for e in journal[-5:]]
    return f"Dernières alertes {NOM} : " + " | ".join(lignes) + f" {suivi}"


# --------------------------------------------------------------------- veille

def heures_calmes(silence, heure):
    """True quand il ne faut pas parler (ex. [22, 8] = de 22 h à 8 h)."""
    try:
        debut, fin = int(silence[0]), int(silence[1])
    except (TypeError, ValueError, IndexError):
        return False
    if debut == fin:
        return False
    return debut <= heure < fin if debut < fin else (heure >= debut or heure < fin)


def analyser(etat, anomalies, injoignable, activite, veille, maintenant):
    """Décide quoi annoncer et met `etat` à jour. Aucun accès réseau ni son ici.

    anomalies, activite : réponses du serveur, None s'il n'a pas répondu.
    Renvoie la liste des phrases nouvelles.
    """
    phrases = []
    annoncees = etat.setdefault("anomalies", {})   # clé -> instant de la dernière annonce

    def signaler(cle, message):
        rappel = cle in annoncees
        if rappel and maintenant - float(annoncees[cle]) < RAPPEL_ANOMALIE_S:
            return
        annoncees[cle] = maintenant
        detail = detail_anomalie(message) or nom_anomalie(cle)
        phrases.append(f"Alerte {NOM}{', toujours en cours' if rappel else ''} : {detail}.")

    if injoignable:
        # Un seul raté peut être une coupure du réseau local : on attend le
        # suivant. Et sans réponse, on ne sait rien des autres anomalies : on
        # ne les déclare donc pas résolues.
        etat["echecs"] = int(etat.get("echecs") or 0) + 1
        if etat["echecs"] >= ECHECS_AVANT_ALERTE:
            signaler("injoignable", "")
    else:
        etat["echecs"] = 0
        for cle, message in (anomalies or {}).items():
            signaler(cle, message)
        for cle in [c for c in annoncees if c not in (anomalies or {})]:
            del annoncees[cle]
            phrases.append(f"Bonne nouvelle pour {NOM} : problème résolu, {nom_anomalie(cle)}.")

    if activite is not None:
        if etat.get("dernier_passage"):
            nouveautes = activite.get("depuis") or {}
            texte = compter_en_clair(nouveautes, veille.get("nouveautes") or [])
            if texte:
                noms = [str(n) for n in nouveautes.get("nouveaux_inscrits") or [] if n]
                if noms and "inscriptions" in (veille.get("nouveautes") or []):
                    texte += " (" + ", ".join(noms) + ")"
                phrases.append(f"{NOM} : {texte}.")
        # Le prochain passage repart de l'horloge du serveur, pas de celle du PC.
        etat["dernier_passage"] = activite.get("genere_le") or etat.get("dernier_passage")

    return phrases


def passage(parler=True):
    """Un tour de veille : interroge le serveur, annonce ce qui est nouveau."""
    veille = reglages_veille()
    depuis = (lire_fichier().get("etat") or {}).get("dernier_passage")
    anomalies, activite, injoignable = None, None, False
    try:
        anomalies = lire_sante()
        activite = lire_activite(depuis)
    except Injoignable as erreur:
        injoignable = True
        print(f"veille {NOM} : {erreur}", file=sys.stderr)

    ici = datetime.now()
    calme = heures_calmes(veille.get("silence"), ici.hour)

    def mettre_a_jour(etat):
        nouvelles = analyser(etat, anomalies, injoignable, activite, veille, time.time())
        etat["dernier_passage_local"] = ici.strftime("%H:%M")
        if nouvelles:
            journal = etat.setdefault("journal", [])
            journal.extend({"quand": ici.strftime("%d/%m %H:%M"), "texte": t} for t in nouvelles)
            del journal[:-JOURNAL_MAX]
        a_dire = (etat.get("en_attente") or []) + nouvelles
        if calme or not parler:
            etat["en_attente"] = a_dire      # gardé pour le prochain passage parlé
            return [], nouvelles
        etat["en_attente"] = []
        return a_dire, nouvelles

    a_dire, nouvelles = modifier_etat(mettre_a_jour)
    if parler and a_dire:
        with base._VERROU_SON:
            base.annoncer(" ".join(a_dire))
    return nouvelles


def veiller():
    """Boucle de fond. Un port local sert de verrou : une seule veille par machine."""
    verrou = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        verrou.bind(("127.0.0.1", PORT_VERROU))
        verrou.listen(1)
    except OSError:
        print(f"veille {NOM} : déjà assurée par un autre processus", file=sys.stderr)
        return
    time.sleep(60)   # laisse le serveur OpenJarvis (la voix) finir de démarrer
    while True:
        try:
            passage()
        except Exception as erreur:   # la veille ne doit jamais s'arrêter sur une erreur
            print(f"veille {NOM} : {type(erreur).__name__}: {erreur}", file=sys.stderr)
        try:
            minutes = max(2, min(float(reglages_veille()["minutes"]), 24 * 60))
        except (TypeError, ValueError):
            minutes = VEILLE_DEFAUT["minutes"]
        time.sleep(minutes * 60)


OUTILS = {
    "leaveatrace_etat": (
        leaveatrace_etat,
        f"État de santé de l'application {NOM} (serveur, tâches en échec, disque, "
        "sauvegardes, leaves non livrés). À utiliser pour « comment va LeaveATrace », "
        "« y a-t-il un problème », « des alertes en cours ».",
        base._schema(),
    ),
    "leaveatrace_activite": (
        leaveatrace_activite,
        f"Activité récente de {NOM} : inscriptions, leaves, capsules, nombre "
        "d'utilisateurs. À utiliser pour « quoi de neuf sur LeaveATrace », "
        "« combien d'inscrits ».",
        base._schema({"periode": {"type": "string", "enum": ["24h", "7j"],
                                  "description": "24h (défaut) ou 7j."}}),
    ),
    "leaveatrace_alertes": (
        leaveatrace_alertes,
        f"Dernières alertes et nouveautés {NOM} déjà annoncées par la veille, et heure "
        "de son dernier passage.",
        base._schema(),
    ),
}


if __name__ == "__main__":
    base.OUTILS = OUTILS
    if len(sys.argv) > 1 and sys.argv[1] == "--veille-une-fois":
        for phrase in passage(parler=False) or ["(rien de nouveau)"]:
            sys.stdout.buffer.write((phrase + "\n").encode("utf-8"))
        sys.exit(0)
    if len(sys.argv) > 2 and sys.argv[1] == "--essai":
        sys.exit(base.essai(sys.argv[2:]))
    threading.Thread(target=veiller, name="veille-leaveatrace", daemon=True).start()
    base.servir()
