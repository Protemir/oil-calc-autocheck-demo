"""Расчёты мок-продукта: упрощённые инженерные и экономические модели.

Это не «правильная нефтянка», а правдоподобные формулы, на которых удобно
показать сверку. Флаг rc включает регрессию в слое ответа (см. ipr).
"""
import math


def ipr(inp, rc=False):
    """Композитная индикаторная кривая (линейная выше Pнас, Вогель ниже)."""
    pr = float(inp["p_res_atm"])
    pb = min(float(inp["p_bubble_atm"]), pr)
    qt = float(inp["q_test_m3d"])
    pwt = float(inp["pwf_test_atm"])
    if pwt >= pb:
        j = qt / (pr - pwt)
    else:
        x = pwt / pb
        # Регрессия релиза-кандидата: потерян квадрат в формуле Вогеля.
        vogel = 1 - 0.2 * x - 0.8 * (x if rc else x * x)
        j = qt / ((pr - pb) + pb / 1.8 * vogel)
    qb = j * (pr - pb)
    aof = qb + j * pb / 1.8

    def q(pwf):
        if pwf >= pb:
            return j * (pr - pwf)
        x = pwf / pb
        return qb + j * pb / 1.8 * (1 - 0.2 * x - 0.8 * x * x)

    curve = [{"pwf_atm": float(p), "q_m3d": q(float(p))} for p in inp["pwf_points_atm"]]
    return {"pi_m3d_atm": j, "q_bubble_m3d": qb, "aof_m3d": aof, "curve": curve}


def stoiip(inp, rc=False):
    """Объёмный метод: геологические и извлекаемые запасы."""
    area = float(inp["area_km2"]) * 1e6
    v = area * float(inp["h_net_m"]) * float(inp["porosity"]) * (1 - float(inp["sw"])) / float(inp["bo"])
    m3 = v / 1e6
    t = m3 * float(inp["density_t_m3"])
    return {
        "pore_volume_mln_m3": area * float(inp["h_net_m"]) * float(inp["porosity"]) / 1e6,
        "stoiip_mln_m3": m3,
        "stoiip_mln_t": t,
        "recoverable_mln_t": t * float(inp["rf"]),
    }


def _arps_cum(qi_year, di, b, t):
    if t <= 0:
        return 0.0
    if b == 0:
        return qi_year / di * (1 - math.exp(-di * t))
    if abs(b - 1) < 1e-12:
        return qi_year / di * math.log(1 + di * t)
    return qi_year / ((1 - b) * di) * (1 - (1 + b * di * t) ** ((b - 1) / b))


def _arps_rate(qi, di, b, t):
    if b == 0:
        return qi * math.exp(-di * t)
    return qi / (1 + b * di * t) ** (1 / b)


def decline(inp, rc=False):
    """Прогноз добычи по Арпсу, помесячно."""
    qi = float(inp["qi_t_d"])
    di = float(inp["di_per_year"])
    b = float(inp["b"])
    months = int(inp["months"])
    qi_year = qi * 365.25
    rows = []
    prev = 0.0
    for m in range(1, months + 1):
        t = m / 12.0
        cum = _arps_cum(qi_year, di, b, t)
        rows.append({
            "month": m,
            "rate_t_d": _arps_rate(qi, di, b, t),
            "monthly_t": cum - prev,
            "cum_kt": cum / 1000.0,
        })
        prev = cum
    return {"eur_kt": prev / 1000.0, "rate_end_t_d": rows[-1]["rate_t_d"], "forecast": rows}


def pipe(inp, rc=False):
    """Потери давления в трубе: Дарси–Вейсбах, Свами–Джейн, плюс гидростатика."""
    q = float(inp["q_m3h"]) / 3600.0
    d = float(inp["d_inner_mm"]) / 1000.0
    length = float(inp["length_km"]) * 1000.0
    eps = float(inp["roughness_mm"]) / 1000.0
    rho = float(inp["density_kg_m3"])
    mu = float(inp["viscosity_cp"]) / 1000.0
    dz = float(inp.get("dz_m", 0.0))
    area = math.pi * d * d / 4
    v = q / area
    re = rho * v * d / mu
    if re < 2300:
        f = 64.0 / re
        regime = "laminar"
    else:
        f = 0.25 / math.log10(eps / (3.7 * d) + 5.74 / re ** 0.9) ** 2
        regime = "turbulent"
    dp_f = f * (length / d) * rho * v * v / 2 / 1e5
    dp_s = rho * 9.80665 * dz / 1e5
    return {
        "velocity_m_s": v,
        "reynolds": re,
        "friction_factor": f,
        "regime": regime,
        "dp_friction_bar": dp_f,
        "dp_static_bar": dp_s,
        "dp_total_bar": dp_f + dp_s,
    }


def _npv(flows, r):
    return sum(cf / (1 + r) ** (i + 0.5) for i, cf in enumerate(flows))


def _irr(flows):
    lo, hi = -0.99, 10.0
    f_lo, f_hi = _npv(flows, lo), _npv(flows, hi)
    if f_lo * f_hi > 0:
        return None
    for _ in range(200):
        mid = (lo + hi) / 2
        f_mid = _npv(flows, mid)
        if f_lo * f_mid <= 0:
            hi = mid
        else:
            lo, f_lo = mid, f_mid
    return (lo + hi) / 2


def economics(inp, rc=False):
    """Упрощённая экономика: выручка, НДПИ, рентный налог, OPEX, CAPEX, КПН, NPV/IRR."""
    years = list(inp["years"])
    prod = [float(x) for x in inp["production_kt"]]
    capex = [float(x) for x in inp["capex_musd"]]
    price = float(inp["price_usd_bbl"])
    bbl_t = float(inp["bbl_per_t"])
    opex_t = float(inp["opex_usd_t"])
    met = float(inp["met_rate"])
    rent = float(inp["rent_tax_rate"])
    export_share = float(inp["export_share"])
    cit = float(inp["cit_rate"])
    r = float(inp["discount_rate"])
    depr = sum(capex) / len(years)
    rows, flows = [], []
    cum = 0.0
    payback = None
    for i, y in enumerate(years):
        revenue = prod[i] * 1000 * bbl_t * price / 1e6
        met_v = revenue * met
        rent_v = revenue * export_share * rent
        opex_v = prod[i] * 1000 * opex_t / 1e6
        taxable = revenue - met_v - rent_v - opex_v - depr
        cit_v = max(0.0, taxable) * cit
        net = revenue - met_v - rent_v - opex_v - capex[i] - cit_v
        disc = net / (1 + r) ** (i + 0.5)
        if payback is None and cum < 0 <= cum + net:
            payback = i + (-cum / net if net else 0.0)
        cum += net
        flows.append(net)
        rows.append({
            "year": int(y),
            "revenue_musd": revenue,
            "met_musd": met_v,
            "rent_tax_musd": rent_v,
            "opex_musd": opex_v,
            "capex_musd": capex[i],
            "cit_musd": cit_v,
            "net_cf_musd": net,
            "disc_cf_musd": disc,
        })
    npv = _npv(flows, r)
    pv_capex = sum(c / (1 + r) ** (i + 0.5) for i, c in enumerate(capex))
    irr = _irr(flows)
    return {
        "npv_musd": npv,
        "irr_pct": irr * 100 if irr is not None else None,
        "payback_years": payback,
        "pi": 1 + npv / pv_capex if pv_capex else None,
        "cashflow": rows,
    }


CALCS = {
    "ipr": ipr,
    "stoiip": stoiip,
    "decline": decline,
    "pipe": pipe,
    "economics": economics,
}
