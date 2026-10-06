import random
from datetime import datetime, timedelta

import pytest

ID_POOL = ["night", "weekend", "special", "a", "b", "中", "夜", "z9"]


def make_case(seed: int):
    """生成一个随机的不超过 36 小时区间 + 至多 8 条随机周重复规则。"""
    rng = random.Random(seed)
    start = (datetime(2026, 9, 14)
             + timedelta(days=rng.randrange(14),
                         hours=rng.randrange(24),
                         minutes=rng.choice([0, 15, 30, 45])))
    length = rng.randrange(1, 36 * 60 + 1)
    end = start + timedelta(minutes=length)

    rules = []
    n_rules = rng.randrange(0, 9)
    for i in range(n_rules):
        s = rng.randrange(0, 1440)
        e = rng.choice([m for m in range(1, 1441) if m != s])
        days = rng.sample(range(7), rng.randrange(1, 4))
        rules.append({"id": ID_POOL[i], "weekdays": days,
                      "start_minute": s, "end_minute": e,
                      "priority": rng.randrange(0, 5),
                      "basis_points": rng.choice([5000, 10000, 12000, 15000, 20000])})
    return start, end, rng.choice([1, 100, 6000, 12345]), rules


def pytest_generate_tests(metafunc):
    if "random_case" in metafunc.fixturenames:
        cases = [make_case(20260923 + i) for i in range(40)]
        metafunc.parametrize("random_case", cases, ids=[f"seed{i}" for i in range(len(cases))])
