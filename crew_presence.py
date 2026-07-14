"""Определение "вместе / врозь" для экипажей с 2+ одновременно активными сменами.

Зачем: если оба члена экипажа одновременно на смене (оба нажали "Начать
смену"), у нас 2 независимых потока GPS от одной физической машины (или,
реже — от двух разных мест, если кто-то не на рабочем месте). Наивное
суммирование total_km по всем активным сменам экипажа задваивает пробег/расход
топлива, если оба в одной машине, и маскирует ситуацию, если они разошлись.

Используется и в main.py (дашборд), и в telegram_bot.py (уведомления) —
поэтому вынесено в общий модуль, а не продублировано.
"""
from datetime import datetime, timezone
from math import radians, sin, cos, sqrt, atan2

TOGETHER_RADIUS_M = 500  # порог "одна машина" — запас на погрешность GPS и разные места в салоне
FRESHNESS_MINUTES = 15   # точка старше этого — не считается для сравнения (телефон мог давно не слать батч)


def haversine_m(lat1, lng1, lat2, lng2):
    R = 6371000
    dlat = radians(lat2 - lat1)
    dlng = radians(lng2 - lng1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlng / 2) ** 2
    return R * 2 * atan2(sqrt(a), sqrt(1 - a))


def _point_age_minutes(point, now):
    recorded = datetime.fromisoformat(point["recorded_at"].replace("Z", "+00:00"))
    return (now - recorded).total_seconds() / 60


def classify_crew_presence(shifts_with_points, now=None):
    """
    shifts_with_points: список {"shift": {...с полями id/total_km...}, "last_point": {"lat","lng","recorded_at"} | None}
    Ожидается, что это ТОЛЬКО активные (active/break/tech) смены экипажа — уже
    завершённые сюда не передаются, они не могут "разойтись" ни с кем прямо сейчас.

    Возвращает:
      {"state": "solo"|"together"|"divergent",
       "clusters": [[shift_id, ...], ...],   # группы смен по физической близости
       "primary_shift_id": str | None}         # чей трек считать "главным" (max total_km) — None при divergent
    """
    if now is None:
        now = datetime.now(timezone.utc)

    if len(shifts_with_points) <= 1:
        primary_id = shifts_with_points[0]["shift"]["id"] if shifts_with_points else None
        return {
            "state": "solo",
            "clusters": [[s["shift"]["id"] for s in shifts_with_points]] if shifts_with_points else [],
            "primary_shift_id": primary_id,
        }

    fresh, stale = [], []
    for sp in shifts_with_points:
        lp = sp.get("last_point")
        if lp and _point_age_minutes(lp, now) <= FRESHNESS_MINUTES:
            fresh.append(sp)
        else:
            stale.append(sp)

    # Меньше 2 свежих точек — сравнивать не с чем (либо у всех кроме одного нет
    # свежих данных). Не можем подтвердить расхождение — считаем как SOLO,
    # "главный" — тот, у кого больше накопленный пробег.
    if len(fresh) <= 1:
        primary = max(shifts_with_points, key=lambda sp: float(sp["shift"].get("total_km") or 0))
        return {
            "state": "solo",
            "clusters": [[s["shift"]["id"] for s in shifts_with_points]],
            "primary_shift_id": primary["shift"]["id"],
        }

    # Кластеризация свежих точек по близости (union-find, порог TOGETHER_RADIUS_M)
    n = len(fresh)
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for i in range(n):
        for j in range(i + 1, n):
            pi, pj = fresh[i]["last_point"], fresh[j]["last_point"]
            if haversine_m(pi["lat"], pi["lng"], pj["lat"], pj["lng"]) <= TOGETHER_RADIUS_M:
                union(i, j)

    groups = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(fresh[i])

    # Смены без свежих данных не можем подтвердить как "вместе" ни с кем — каждая своя отдельная группа
    cluster_groups = list(groups.values()) + [[sp] for sp in stale]

    if len(cluster_groups) == 1:
        cluster = cluster_groups[0]
        primary = max(cluster, key=lambda sp: float(sp["shift"].get("total_km") or 0))
        return {
            "state": "together",
            "clusters": [[s["shift"]["id"] for s in cluster]],
            "primary_shift_id": primary["shift"]["id"],
        }

    return {
        "state": "divergent",
        "clusters": [[s["shift"]["id"] for s in c] for c in cluster_groups],
        "primary_shift_id": None,
    }
