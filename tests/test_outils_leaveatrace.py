"""Decision logic of the LeaveATrace watcher (no network, no sound)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import outils_leaveatrace as lt  # noqa: E402

VEILLE = {"minutes": 15, "silence": [22, 8], "nouveautes": ["inscriptions", "capsules_ouvertes"]}
JOBS = {"jobs_echoues": "3 job(s) en echec — php artisan queue:failed pour la cause."}


def _activite(genere_le="2026-11-22T10:00:00+00:00", **depuis):
    resume = {"genere_le": genere_le}
    if depuis:
        resume["depuis"] = depuis
    return resume


def test_new_anomaly_is_announced_once_then_reminded_after_six_hours():
    etat = {}
    first = lt.analyser(etat, JOBS, False, None, VEILLE, 1000.0)
    assert first == ["Alerte LeaveATrace : 3 jobs en echec."]
    assert lt.analyser(etat, JOBS, False, None, VEILLE, 1000.0 + 3600) == []
    later = lt.analyser(etat, JOBS, False, None, VEILLE, 1000.0 + lt.RAPPEL_ANOMALIE_S)
    assert later == ["Alerte LeaveATrace, toujours en cours : 3 jobs en echec."]


def test_resolved_anomaly_is_announced():
    etat = {}
    lt.analyser(etat, JOBS, False, None, VEILLE, 1000.0)
    assert lt.analyser(etat, {}, False, None, VEILLE, 2000.0) == [
        "Bonne nouvelle pour LeaveATrace : problème résolu, tâches en échec."
    ]
    assert lt.analyser(etat, {}, False, None, VEILLE, 3000.0) == []


def test_single_missed_check_is_not_an_alert_but_two_are():
    etat = {}
    assert lt.analyser(etat, None, True, None, VEILLE, 1000.0) == []
    assert lt.analyser(etat, None, True, None, VEILLE, 2000.0) == [
        "Alerte LeaveATrace : serveur injoignable."
    ]
    assert lt.analyser(etat, None, True, None, VEILLE, 3000.0) == []
    back = lt.analyser(etat, {}, False, None, VEILLE, 4000.0)
    assert back == ["Bonne nouvelle pour LeaveATrace : problème résolu, serveur injoignable."]
    assert etat["echecs"] == 0


def test_unreachable_server_does_not_declare_other_anomalies_resolved():
    etat = {}
    lt.analyser(etat, JOBS, False, None, VEILLE, 1000.0)
    lt.analyser(etat, None, True, None, VEILLE, 2000.0)
    phrases = lt.analyser(etat, None, True, None, VEILLE, 3000.0)
    assert phrases == ["Alerte LeaveATrace : serveur injoignable."]
    assert "jobs_echoues" in etat["anomalies"]


def test_first_pass_only_sets_the_starting_point():
    etat = {}
    assert lt.analyser(etat, {}, False, _activite(inscriptions=4), VEILLE, 1000.0) == []
    assert etat["dernier_passage"] == "2026-11-22T10:00:00+00:00"


def test_news_since_last_pass_follow_the_configured_list():
    etat = {"dernier_passage": "2026-11-22T09:45:00+00:00"}
    activite = _activite(inscriptions=2, leaves_crees=7, capsules_ouvertes=1,
                         nouveaux_inscrits=["Alice", "Bob"])
    assert lt.analyser(etat, {}, False, activite, VEILLE, 1000.0) == [
        "LeaveATrace : 2 nouvelles inscriptions et 1 capsule ouverte (Alice, Bob)."
    ]
    assert etat["dernier_passage"] == "2026-11-22T10:00:00+00:00"


def test_no_news_says_nothing():
    etat = {"dernier_passage": "x"}
    assert lt.analyser(etat, {}, False, _activite(inscriptions=0, leaves_crees=3), VEILLE, 1.0) == []


def test_quiet_hours_wrap_around_midnight():
    assert lt.heures_calmes([22, 8], 23) is True
    assert lt.heures_calmes([22, 8], 3) is True
    assert lt.heures_calmes([22, 8], 8) is False
    assert lt.heures_calmes([22, 8], 14) is False
    assert lt.heures_calmes([13, 14], 13) is True
    assert lt.heures_calmes([0, 0], 5) is False
    assert lt.heures_calmes(None, 5) is False


def test_counts_in_plain_french():
    assert lt.compter_en_clair({"inscriptions": 1}) == "1 nouvelle inscription"
    assert lt.compter_en_clair({"inscriptions": 2, "leaves_crees": 1, "contributions": 3}) == (
        "2 nouvelles inscriptions, 1 leave créé et 3 contributions aux capsules"
    )
    assert lt.compter_en_clair({}) == ""


def test_technical_instruction_is_not_read_aloud():
    assert lt.detail_anomalie("Moins de 15% de disque libre (3 Go restants).") == (
        "Moins de 15% de disque libre (3 Go restants)."
    )
    assert "artisan" not in lt.detail_anomalie(JOBS["jobs_echoues"])
