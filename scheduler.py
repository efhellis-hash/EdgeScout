"""
scheduler.py — Runner de EdgeScout para Railway.

Corre el analisis una vez al dia (y al arrancar). Usa APScheduler, igual que
tus otros proyectos. En Railway el start command es: python scheduler.py
"""
import os
import json
from apscheduler.schedulers.blocking import BlockingScheduler

from analyst import correr_dia
import clv

BANKROLL = float(os.environ.get("BANKROLL", "1000"))
SPORT = os.environ.get("SPORT", "MLB")
RUN_HOUR = int(os.environ.get("RUN_HOUR_UTC", "20"))


def job():
    print(f"[EdgeScout] corriendo {SPORT} | banca ${BANKROLL}")
    resultados = correr_dia(SPORT, BANKROLL)
    print(json.dumps(resultados, indent=2, ensure_ascii=False))
    print("[EdgeScout] CLV acumulado:",
          json.dumps(clv.resumen_clv(), ensure_ascii=False))


if __name__ == "__main__":
    sched = BlockingScheduler(timezone="UTC")
    sched.add_job(job, "cron", hour=RUN_HOUR, minute=0)
    print(f"[EdgeScout] programado diario a las {RUN_HOUR}:00 UTC")
    job()  # corre una vez al iniciar
    sched.start()
