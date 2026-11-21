# outils_quotidien.py : outils du quotidien pour OpenJarvis (heure, minuteurs,
# notes, météo), exposés par le protocole MCP sur l'entrée/sortie standard.
#
# OpenJarvis lance ce script lui-même (voir ~/.openjarvis/mcp_servers.json) et
# propose ses outils au modèle. Aucune dépendance en dehors de la bibliothèque
# standard pour le protocole ; le son du minuteur utilise sounddevice/soundfile,
# déjà présents dans l'environnement du projet.
#
# Réseau : seule la météo sort de la machine, vers Open-Meteo (sans clé, sans
# compte). La ville n'est jamais devinée par l'adresse IP : elle est donnée à
# la voix, puis mémorisée dans ~/.openjarvis/quotidien.json.
#
# Test hors OpenJarvis :
#   python outils_quotidien.py --essai heure_et_date
#   python outils_quotidien.py --essai meteo ville=Lyon
import io
import json
import os
import sys
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

DOSSIER = Path(os.environ.get("OPENJARVIS_HOME") or Path.home() / ".openjarvis")
FICHIER_REGLAGES = DOSSIER / "quotidien.json"
DOSSIER_NOTES = DOSSIER / "notes"
API_JARVIS = os.environ.get("JARVIS_API", "http://127.0.0.1:8000")

JOURS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
MOIS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août",
        "septembre", "octobre", "novembre", "décembre"]

# Codes météo WMO renvoyés par Open-Meteo.
CODES_METEO = {
    0: "ciel dégagé", 1: "plutôt dégagé", 2: "partiellement nuageux", 3: "couvert",
    45: "brouillard", 48: "brouillard givrant",
    51: "bruine légère", 53: "bruine", 55: "bruine dense",
    56: "bruine verglaçante", 57: "bruine verglaçante dense",
    61: "pluie faible", 63: "pluie", 65: "pluie forte",
    66: "pluie verglaçante", 67: "pluie verglaçante forte",
    71: "neige faible", 73: "neige", 75: "neige forte", 77: "grains de neige",
    80: "averses faibles", 81: "averses", 82: "fortes averses",
    85: "averses de neige", 86: "fortes averses de neige",
    95: "orage", 96: "orage avec grêle", 99: "orage violent avec grêle",
}


# ------------------------------------------------------------------ réglages

def lire_reglages():
    try:
        donnees = json.loads(FICHIER_REGLAGES.read_text(encoding="utf-8"))
        return donnees if isinstance(donnees, dict) else {}
    except (OSError, ValueError):
        return {}


def ecrire_reglages(reglages):
    DOSSIER.mkdir(parents=True, exist_ok=True)
    provisoire = FICHIER_REGLAGES.with_suffix(".tmp")
    provisoire.write_text(json.dumps(reglages, ensure_ascii=False, indent=2), encoding="utf-8")
    provisoire.replace(FICHIER_REGLAGES)


# ---------------------------------------------------------------------- heure

def date_en_clair(moment):
    jour = "1er" if moment.day == 1 else str(moment.day)
    return f"{JOURS[moment.weekday()]} {jour} {MOIS[moment.month - 1]} {moment.year}"


def heure_en_clair(moment):
    return f"{moment.hour} heures" if moment.minute == 0 else f"{moment.hour} heures {moment.minute:02d}"


def heure_et_date():
    maintenant = datetime.now()
    return f"Nous sommes le {date_en_clair(maintenant)} et il est {heure_en_clair(maintenant)}."


# ------------------------------------------------------------------ minuteurs

_MINUTEURS = {}            # numéro -> {"libelle", "fin", "fil"}
_VERROU = threading.Lock()
_PROCHAIN = [1]


def duree_en_clair(secondes):
    secondes = int(round(secondes))
    heures, reste = divmod(secondes, 3600)
    minutes, secs = divmod(reste, 60)
    morceaux = []
    if heures:
        morceaux.append(f"{heures} heure{'s' if heures > 1 else ''}")
    if minutes:
        morceaux.append(f"{minutes} minute{'s' if minutes > 1 else ''}")
    if secs or not morceaux:
        morceaux.append(f"{secs} seconde{'s' if secs > 1 else ''}")
    return " et ".join(morceaux[:2])


def _jouer(audio, taux):
    import sounddevice as sd

    sd.play(audio, taux)
    sd.wait()


def _bips():
    import numpy as np

    t = np.arange(int(24000 * 0.18)) / 24000
    bip = 0.25 * np.sin(2 * np.pi * 880 * t) * np.hanning(t.size)
    silence = np.zeros(int(24000 * 0.12))
    _jouer(np.concatenate([bip, silence, bip, silence, bip]).astype("float32"), 24000)


def annoncer(texte):
    """Trois bips puis la phrase, lue par la voix du serveur OpenJarvis."""
    try:
        _bips()
    except Exception as erreur:
        print(f"minuteur : bips indisponibles ({erreur})", file=sys.stderr)
    try:
        import soundfile as sf

        requete = urllib.request.Request(
            f"{API_JARVIS}/v1/speech/synthesize",
            data=json.dumps({"text": texte}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(requete, timeout=30) as reponse:
            audio, taux = sf.read(io.BytesIO(reponse.read()), dtype="float32")
        _jouer(audio, taux)
    except Exception as erreur:   # serveur arrêté : les bips suffisent à prévenir
        print(f"minuteur : annonce vocale indisponible ({erreur})", file=sys.stderr)


def _sonner(numero):
    with _VERROU:
        minuteur = _MINUTEURS.pop(numero, None)
    if minuteur is None:
        return  # annulé entre-temps
    libelle = minuteur["libelle"]
    annoncer(f"Le minuteur {libelle} est terminé." if libelle else "Le minuteur est terminé.")


def lancer_minuteur(secondes=0, minutes=0, libelle=""):
    try:
        total = float(secondes or 0) + 60 * float(minutes or 0)
    except (TypeError, ValueError):
        return "Je n'ai pas compris la durée du minuteur."
    if total < 1:
        return "Il me faut une durée d'au moins une seconde."
    if total > 24 * 3600:
        return "Je ne lance pas de minuteur de plus de vingt-quatre heures."
    libelle = str(libelle or "").strip()
    with _VERROU:
        numero = _PROCHAIN[0]
        _PROCHAIN[0] += 1
        fil = threading.Timer(total, _sonner, args=(numero,))
        fil.daemon = True
        _MINUTEURS[numero] = {"libelle": libelle, "fin": time.time() + total, "fil": fil}
        fil.start()
    nom = f" « {libelle} »" if libelle else ""
    return f"Minuteur{nom} lancé pour {duree_en_clair(total)}."


def lister_minuteurs():
    with _VERROU:
        actifs = sorted(_MINUTEURS.items())
    if not actifs:
        return "Aucun minuteur en cours."
    lignes = []
    for numero, m in actifs:
        nom = f" « {m['libelle']} »" if m["libelle"] else ""
        lignes.append(f"minuteur {numero}{nom} : encore {duree_en_clair(max(0, m['fin'] - time.time()))}")
    return "Minuteurs en cours : " + " ; ".join(lignes) + "."


def annuler_minuteurs(libelle=""):
    cible = str(libelle or "").strip().lower()
    with _VERROU:
        choisis = [n for n, m in _MINUTEURS.items() if not cible or cible in m["libelle"].lower()]
        for numero in choisis:
            _MINUTEURS.pop(numero)["fil"].cancel()
    if not choisis:
        return "Aucun minuteur à annuler."
    return f"{len(choisis)} minuteur{'s' if len(choisis) > 1 else ''} annulé{'s' if len(choisis) > 1 else ''}."


# ---------------------------------------------------------------------- notes

def noter(contenu=""):
    contenu = " ".join(str(contenu or "").split())
    if not contenu:
        return "Il n'y a rien à noter."
    maintenant = datetime.now()
    DOSSIER_NOTES.mkdir(parents=True, exist_ok=True)
    fichier = DOSSIER_NOTES / f"{maintenant:%Y-%m-%d}.md"
    with fichier.open("a", encoding="utf-8") as f:
        f.write(f"- {maintenant:%H:%M} {contenu}\n")
    return f"C'est noté : {contenu}"


def lire_notes(jours=1):
    try:
        jours = max(1, min(int(jours or 1), 31))
    except (TypeError, ValueError):
        jours = 1
    aujourd_hui = datetime.now().date()
    blocs = []
    for recul in range(jours):
        jour = aujourd_hui - timedelta(days=recul)
        fichier = DOSSIER_NOTES / f"{jour:%Y-%m-%d}.md"
        try:
            lignes = [ligne[2:].strip() for ligne in fichier.read_text(encoding="utf-8").splitlines()
                      if ligne.startswith("- ")]
        except OSError:
            continue
        if lignes:
            titre = "Aujourd'hui" if recul == 0 else ("Hier" if recul == 1 else date_en_clair(
                datetime.combine(jour, datetime.min.time())))
            blocs.append(f"{titre} : " + " ; ".join(lignes))
    if not blocs:
        return "Aucune note aujourd'hui." if jours == 1 else f"Aucune note sur les {jours} derniers jours."
    return " | ".join(blocs)


# ---------------------------------------------------------------------- météo

def _json(url):
    requete = urllib.request.Request(url, headers={"User-Agent": "outils-quotidien"})
    with urllib.request.urlopen(requete, timeout=8) as reponse:
        return json.loads(reponse.read().decode("utf-8"))


def meteo(ville="", jours=1):
    reglages = lire_reglages()
    ville = str(ville or "").strip() or str(reglages.get("ville") or "").strip()
    if not ville:
        return ("Je ne connais pas encore votre ville. Demandez par exemple "
                "« quelle est la météo à Lyon » : je la retiendrai pour la suite.")
    try:
        jours = max(1, min(int(jours or 1), 7))
    except (TypeError, ValueError):
        jours = 1
    try:
        lieux = _json("https://geocoding-api.open-meteo.com/v1/search?count=1&language=fr&name="
                      + urllib.parse.quote(ville)).get("results") or []
        if not lieux:
            return f"Je ne trouve pas la ville « {ville} »."
        lieu = lieux[0]
        donnees = _json(
            "https://api.open-meteo.com/v1/forecast?timezone=auto"
            f"&latitude={lieu['latitude']}&longitude={lieu['longitude']}"
            "&current=temperature_2m,apparent_temperature,weather_code,wind_speed_10m"
            "&daily=weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max"
            f"&forecast_days={jours}")
    except Exception as erreur:
        return f"La météo est indisponible pour le moment ({type(erreur).__name__})."

    nom = lieu.get("name", ville)
    if reglages.get("ville") != nom:
        reglages["ville"] = nom
        try:
            ecrire_reglages(reglages)
        except OSError:
            pass

    actuel = donnees.get("current", {})
    quotidien = donnees.get("daily", {})
    phrases = [
        f"À {nom}, il fait {round(actuel.get('temperature_2m', 0))} degrés, "
        f"{CODES_METEO.get(actuel.get('weather_code'), 'temps variable')}, "
        f"vent à {round(actuel.get('wind_speed_10m', 0))} kilomètres-heure."
    ]
    dates = quotidien.get("time", [])
    for i, jour in enumerate(dates[:jours]):
        quand = "Aujourd'hui" if i == 0 else ("Demain" if i == 1 else JOURS[datetime.fromisoformat(jour).weekday()].capitalize())
        pluie = quotidien.get("precipitation_probability_max", [None] * len(dates))[i]
        risque = f", {pluie} % de risque de pluie" if pluie is not None else ""
        phrases.append(
            f"{quand} : {CODES_METEO.get(quotidien['weather_code'][i], 'temps variable')}, "
            f"de {round(quotidien['temperature_2m_min'][i])} à {round(quotidien['temperature_2m_max'][i])} degrés{risque}.")
    return " ".join(phrases)


# --------------------------------------------------------------------- outils

def _schema(proprietes=None, requis=()):
    return {"type": "object", "properties": proprietes or {}, "required": list(requis)}


OUTILS = {
    "heure_et_date": (
        heure_et_date,
        "Donne l'heure et la date actuelles. À utiliser pour toute question sur l'heure, "
        "le jour ou la date : ne jamais deviner.",
        _schema(),
    ),
    "lancer_minuteur": (
        lancer_minuteur,
        "Lance un minuteur qui sonnera et sera annoncé à voix haute à la fin. "
        "Donner la durée en minutes et/ou en secondes, et un libellé facultatif (ex. « pâtes »).",
        _schema({
            "minutes": {"type": "number", "description": "Durée en minutes."},
            "secondes": {"type": "number", "description": "Durée en secondes (s'ajoute aux minutes)."},
            "libelle": {"type": "string", "description": "Nom du minuteur, facultatif."},
        }),
    ),
    "lister_minuteurs": (
        lister_minuteurs,
        "Indique les minuteurs en cours et le temps restant.",
        _schema(),
    ),
    "annuler_minuteurs": (
        annuler_minuteurs,
        "Annule les minuteurs en cours. Sans libellé, les annule tous.",
        _schema({"libelle": {"type": "string", "description": "Nom du minuteur à annuler, facultatif."}}),
    ),
    "noter": (
        noter,
        "Enregistre une note dictée par l'utilisateur (idée, course, rappel écrit).",
        _schema({"contenu": {"type": "string", "description": "Le texte à noter."}}, ["contenu"]),
    ),
    "lire_notes": (
        lire_notes,
        "Relit les notes enregistrées aujourd'hui, ou sur les derniers jours.",
        _schema({"jours": {"type": "integer", "description": "Nombre de jours à relire (1 = aujourd'hui)."}}),
    ),
    "meteo": (
        meteo,
        "Donne la météo actuelle et les prévisions d'une ville. Sans ville, utilise la "
        "dernière ville demandée.",
        _schema({
            "ville": {"type": "string", "description": "Nom de la ville, facultatif."},
            "jours": {"type": "integer", "description": "Nombre de jours de prévision (1 à 7)."},
        }),
    ),
}


def appeler(nom, arguments):
    if nom not in OUTILS:
        return f"Outil inconnu : {nom}", True
    fonction = OUTILS[nom][0]
    connus = OUTILS[nom][2]["properties"]
    try:
        return fonction(**{k: v for k, v in (arguments or {}).items() if k in connus}), False
    except Exception as erreur:
        return f"L'outil {nom} a échoué : {type(erreur).__name__}: {erreur}", True


# ------------------------------------------------------------------- protocole

def _decoder(octets):
    """OpenJarvis écrit dans l'encodage de Windows, d'autres clients en UTF-8."""
    try:
        return octets.decode("utf-8")
    except UnicodeDecodeError:
        return octets.decode("cp1252", errors="replace")


def repondre(identifiant, resultat=None, erreur=None):
    message = {"jsonrpc": "2.0", "id": identifiant}
    if erreur is not None:
        message["error"] = erreur
    else:
        message["result"] = resultat
    # ensure_ascii : la ligne reste lisible quel que soit l'encodage du client.
    sys.stdout.write(json.dumps(message, ensure_ascii=True) + "\n")
    sys.stdout.flush()


def servir():
    for brut in sys.stdin.buffer:
        ligne = _decoder(brut).strip()
        if not ligne:
            continue
        try:
            requete = json.loads(ligne)
        except ValueError:
            continue
        if not isinstance(requete, dict) or "id" not in requete:
            continue  # notification : aucune réponse attendue
        identifiant = requete["id"]
        methode = requete.get("method")
        parametres = requete.get("params") or {}
        if methode == "initialize":
            repondre(identifiant, {
                "protocolVersion": parametres.get("protocolVersion", "2025-03-26"),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "outils-quotidien", "version": "1.0.0"},
            })
        elif methode == "ping":
            repondre(identifiant, {})
        elif methode == "tools/list":
            repondre(identifiant, {"tools": [
                {"name": nom, "description": description, "inputSchema": schema}
                for nom, (_, description, schema) in OUTILS.items()
            ]})
        elif methode == "tools/call":
            texte, en_erreur = appeler(parametres.get("name"), parametres.get("arguments"))
            repondre(identifiant, {"content": [{"type": "text", "text": texte}], "isError": en_erreur})
        else:
            repondre(identifiant, erreur={"code": -32601, "message": f"Méthode inconnue : {methode}"})


def essai(arguments):
    """Appel direct d'un outil depuis le terminal : --essai nom cle=valeur ..."""
    nom, *paires = arguments
    valeurs = dict(paire.split("=", 1) for paire in paires)
    texte, en_erreur = appeler(nom, valeurs)
    sys.stdout.buffer.write((texte + "\n").encode("utf-8"))
    if nom == "lancer_minuteur" and not en_erreur:
        while _MINUTEURS:          # laisse sonner avant de quitter
            time.sleep(0.2)
        time.sleep(0.5)
    return 1 if en_erreur else 0


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--essai":
        sys.exit(essai(sys.argv[2:]))
    servir()
