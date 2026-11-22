# hey_jarvis.py : réveille OpenJarvis en disant « Hey Jarvis ».
#
# Écoute le micro en continu avec openWakeWord (modèle local, rien ne sort de la
# machine). Deux usages :
#
#   python hey_jarvis.py            # mode GUI : prévient la page « Voix » du GUI,
#                                   #   qui se met à écouter (jarvis gui doit tourner)
#   python hey_jarvis.py --chat     # mode terminal : ouvre « jarvis chat --voice »
#   python hey_jarvis.py --test f.wav   # hors micro : note un fichier audio
#
# Options : --seuil 0.5 (sensibilité, 0.3 = facile, 0.7 = strict), --sans-bip,
#           --port 8771, --micro <index sounddevice>.
#
# À lancer avec le Python du projet :
#   D:\source\Jarvis\.venv\Scripts\python.exe hey_jarvis.py
import argparse
import json
import os
import queue
import shlex
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import numpy as np

SR = 16000             # openWakeWord travaille en 16 kHz mono
BLOC = 1280            # 80 ms par lecture, la taille attendue par le modèle
SEUIL = 0.5            # score minimal pour considérer que « Hey Jarvis » a été dit
PAUSE = 2.0            # secondes sans nouveau réveil après une détection
PORT = 8771            # le GUI écoute les réveils sur http://127.0.0.1:8771/events
ORIGINES = {"127.0.0.1", "localhost"}   # seules pages autorisées à s'abonner

# Commande du Jarvis sur ce système (mode --chat).
JARVIS_CMD = r"D:\source\Jarvis\.venv\Scripts\jarvis.exe chat --voice"
JARVIS_DIR = r"D:\source\Jarvis"


def charger_modele():
    """Charge le modèle « hey_jarvis » fourni avec openWakeWord."""
    import openwakeword
    from openwakeword.model import Model

    chemin = Path(openwakeword.__file__).parent / "resources" / "models" / "hey_jarvis_v0.1.onnx"
    return Model(wakeword_model_paths=[str(chemin)])


def bip(frequence=880, duree=0.12):
    """Petit signal sonore : Jarvis a entendu son nom."""
    try:
        import sounddevice as sd

        t = np.arange(int(24000 * duree)) / 24000
        son = 0.2 * np.sin(2 * np.pi * frequence * t) * np.hanning(t.size)
        sd.play(son.astype(np.float32), 24000)
    except Exception as erreur:  # pas de sortie audio : on continue sans bip
        print(f"(bip indisponible : {erreur})")


# ----------------------------------------------------------------- diffusion

class Diffuseur:
    """Relaie les réveils aux pages abonnées (Server-Sent Events)."""

    def __init__(self):
        self._abonnes = set()
        self._verrou = threading.Lock()

    def abonner(self):
        file = queue.Queue(maxsize=8)
        with self._verrou:
            self._abonnes.add(file)
        return file

    def desabonner(self, file):
        with self._verrou:
            self._abonnes.discard(file)

    def publier(self, message):
        with self._verrou:
            abonnes = list(self._abonnes)
        for file in abonnes:
            try:
                file.put_nowait(message)
            except queue.Full:
                pass  # page figée : elle rattrapera au prochain réveil
        return len(abonnes)


def _origine_autorisee(origine):
    """N'accepte que les pages servies depuis cette machine."""
    if not origine:
        return None
    try:
        return origine if urlparse(origine).hostname in ORIGINES else None
    except ValueError:
        return None


def creer_serveur(diffuseur, port):
    class Gestionnaire(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):  # silence : pas un journal web
            pass

        def _cors(self):
            origine = _origine_autorisee(self.headers.get("Origin"))
            if origine:
                self.send_header("Access-Control-Allow-Origin", origine)
                self.send_header("Vary", "Origin")

        def do_GET(self):
            if self.path == "/health":
                corps = json.dumps({"ok": True, "mot": "hey jarvis"}).encode()
                self.send_response(200)
                self._cors()
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(corps)))
                self.end_headers()
                self.wfile.write(corps)
                return
            if self.path != "/events":
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return

            self.send_response(200)
            self._cors()
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            file = diffuseur.abonner()
            try:
                self.wfile.write(b": pret\n\n")
                self.wfile.flush()
                while True:
                    try:
                        message = file.get(timeout=15)
                        trame = f"event: wake\ndata: {json.dumps(message)}\n\n"
                    except queue.Empty:
                        trame = ": ping\n\n"   # garde la connexion ouverte
                    self.wfile.write(trame.encode())
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionError, OSError):
                pass  # la page a été fermée
            finally:
                diffuseur.desabonner(file)
                self.close_connection = True

    serveur = ThreadingHTTPServer(("127.0.0.1", port), Gestionnaire)
    serveur.daemon_threads = True
    return serveur


# -------------------------------------------------------------------- écoute

def ouvrir_jarvis():
    cmd = shlex.split(os.environ.get("JARVIS_CMD", JARVIS_CMD), posix=os.name != "nt")
    dossier = os.environ.get("JARVIS_DIR") or JARVIS_DIR
    print("Ouverture :", " ".join(cmd))
    subprocess.run(cmd, cwd=dossier, shell=os.name == "nt")


def ecouter(au_reveil, seuil, micro, avec_bip, bloquant, debug=False):
    """Boucle micro : appelle au_reveil(score) à chaque « Hey Jarvis ».

    bloquant=True : au_reveil garde la main longtemps (chat vocal) ; le micro est
    alors relâché pendant ce temps, puis rouvert.
    debug=True : affiche chaque seconde le niveau du micro et le meilleur score.
    """
    import sounddevice as sd

    modele = charger_modele()
    print(f'Prêt. Dites « Hey Jarvis » (seuil {seuil}). Ctrl+C pour quitter.')
    while True:
        dernier = 0.0
        reveille = False
        score = 0.0
        try:
            with sd.InputStream(samplerate=SR, blocksize=BLOC, channels=1, dtype="int16",
                                device=micro) as flux:
                blocs = 0
                pic_niveau = 0.0
                pic_score = 0.0
                while True:
                    donnees, _ = flux.read(BLOC)
                    score = float(max(modele.predict(donnees[:, 0]).values()))
                    if debug:
                        blocs += 1
                        pic_niveau = max(pic_niveau, float(np.abs(donnees).max()) / 32768)
                        pic_score = max(pic_score, score)
                        if blocs >= SR // BLOC:      # ~1 seconde
                            print(f"  [debug] niveau {pic_niveau:.3f}  score {pic_score:.3f}")
                            blocs, pic_niveau, pic_score = 0, 0.0, 0.0
                    maintenant = time.monotonic()
                    if score < seuil or maintenant - dernier < PAUSE:
                        continue
                    dernier = maintenant
                    modele.reset()
                    print(f"  Hey Jarvis détecté (score {score:.2f})")
                    if bloquant:
                        reveille = True
                        break
                    au_reveil(score)
                    if avec_bip:
                        # Dans un fil à part : jouer un son ne doit jamais
                        # retarder ni bloquer la lecture du micro.
                        threading.Thread(target=bip, daemon=True).start()
        except KeyboardInterrupt:
            raise
        except Exception as erreur:
            # Micro débranché, périphérique par défaut changé, flux interrompu :
            # on rouvre le micro au lieu de rester figé.
            print(f"  (micro interrompu : {erreur} ; nouvelle tentative dans 2 s)")
            time.sleep(2)
            continue
        if reveille:
            if avec_bip:
                bip()
            au_reveil(score)          # micro relâché pendant le chat
            modele.reset()
            print("\nDe nouveau à l'écoute de « Hey Jarvis »…")


def tester(chemin, seuil):
    """Passe un fichier audio dans le détecteur et affiche le meilleur score."""
    import soundfile as sf

    audio, taux = sf.read(chemin, dtype="float32", always_2d=True)
    audio = audio[:, 0]
    if taux != SR:
        from math import gcd

        from scipy.signal import resample_poly

        d = gcd(SR, taux)
        audio = resample_poly(audio, SR // d, taux // d)
    # Une seconde de silence avant et après, comme un micro ouvert en continu.
    audio = np.concatenate([np.zeros(SR, np.float32), audio, np.zeros(SR, np.float32)])
    pcm = (np.clip(audio, -1, 1) * 32767).astype(np.int16)
    modele = charger_modele()
    meilleur = 0.0
    for debut in range(0, len(pcm) - BLOC + 1, BLOC):
        meilleur = max(meilleur, max(modele.predict(pcm[debut:debut + BLOC]).values()))
    verdict = "DÉTECTÉ" if meilleur >= seuil else "non détecté"
    print(f"{Path(chemin).name} : score max {meilleur:.3f} -> {verdict} (seuil {seuil})")
    return meilleur >= seuil


def main():
    analyseur = argparse.ArgumentParser(description="Mot d'activation « Hey Jarvis » pour OpenJarvis.")
    analyseur.add_argument("--chat", action="store_true",
                           help="ouvre « jarvis chat --voice » au lieu de prévenir le GUI")
    analyseur.add_argument("--test", metavar="FICHIER", help="note un fichier audio et quitte")
    analyseur.add_argument("--seuil", type=float, default=SEUIL)
    analyseur.add_argument("--port", type=int, default=PORT)
    analyseur.add_argument("--micro", type=int, default=None,
                           help="index du micro (sounddevice) ; défaut = micro du système")
    analyseur.add_argument("--sans-bip", action="store_true")
    analyseur.add_argument("--debug", action="store_true",
                           help="affiche chaque seconde le niveau du micro et le score")
    options = analyseur.parse_args()

    if options.test:
        sys.exit(0 if tester(options.test, options.seuil) else 1)

    if options.chat:
        ecouter(lambda _score: ouvrir_jarvis(), options.seuil, options.micro,
                not options.sans_bip, bloquant=True, debug=options.debug)
        return

    diffuseur = Diffuseur()
    try:
        serveur = creer_serveur(diffuseur, options.port)
    except OSError as erreur:
        sys.exit(f"Port {options.port} indisponible ({erreur}). hey_jarvis.py tourne "
                 "peut-être déjà ; sinon choisissez un autre port avec --port.")
    threading.Thread(target=serveur.serve_forever, name="reveil-sse", daemon=True).start()
    print(f"Réveils diffusés sur http://127.0.0.1:{options.port}/events "
          "(page « Voix » du GUI).")

    def prevenir(score):
        pages = diffuseur.publier({"score": round(score, 3), "t": time.time()})
        if not pages:
            print("  (aucune page « Voix » ouverte : rien à réveiller)")

    ecouter(prevenir, options.seuil, options.micro, not options.sans_bip, bloquant=False,
            debug=options.debug)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nAu revoir.")
