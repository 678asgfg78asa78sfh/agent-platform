"""Math tools: safe calculator, calendar math, geometry, stats, algebra and units."""

import ast
import calendar as calmod
import json
import math
import os
import statistics
import sys
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, getcontext
from fractions import Fraction
from zoneinfo import ZoneInfo


MODULE = {
    "name": "math_tools",
    "description": "Sichere Mathe-Tools: Rechner, Kalender/Datum, Geometrie, Statistik, Algebra und Einheiten.",
    "version": "1.0",
    "settings": {
        "precision": {"type": "number", "label": "Nachkommastellen", "default": 10},
        "timezone": {"type": "string", "label": "Zeitzone", "default": "Europe/Berlin"},
        "max_output_chars": {"type": "number", "label": "Max Ausgabezeichen", "default": 6000},
    },
    "tools": [
        {
            "name": "math_tools.calc",
            "description": "Sicherer Rechner fuer einfache/erweiterte Mathematik. Param: Ausdruck oder JSON {expr}. Beispiel: sin(pi/2)+sqrt(16).",
            "params": ["expression_json"],
        },
        {
            "name": "math_tools.calendar",
            "description": "Kalender- und Datumsrechnung. JSON: {action:now|add|diff|weekday|business_days|month, ...}.",
            "params": ["query_json"],
        },
        {
            "name": "math_tools.geometry",
            "description": "Geometrie fuer Formen. JSON: {shape:circle|rectangle|triangle|sphere|cylinder|cone|regular_polygon, ...}.",
            "params": ["shape_json"],
        },
        {
            "name": "math_tools.stats",
            "description": "Statistik fuer Zahlenlisten. JSON: {data:[...]} oder Komma-Liste.",
            "params": ["data_json"],
        },
        {
            "name": "math_tools.algebra",
            "description": "Algebra-Loeser: linear, quadratic, system2. JSON: {type, coefficients, constants}.",
            "params": ["query_json"],
        },
        {
            "name": "math_tools.convert",
            "description": "Einheiten umrechnen: Laenge, Masse, Zeit, Flaeche, Volumen, Winkel, Temperatur. JSON: {value, from, to}.",
            "params": ["query_json"],
        },
        {
            "name": "math_tools.fraction",
            "description": "Bruch-/Prozentrechnung. JSON: {value} oder {op:add|sub|mul|div, values:[...]}.",
            "params": ["query_json"],
        },
    ],
}


CONSTANTS = {
    "pi": math.pi,
    "e": math.e,
    "tau": math.tau,
    "inf": math.inf,
}

FUNCTIONS = {
    "abs": abs,
    "round": round,
    "sqrt": math.sqrt,
    "cbrt": lambda x: math.copysign(abs(x) ** (1.0 / 3.0), x),
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
    "asin": math.asin,
    "acos": math.acos,
    "atan": math.atan,
    "atan2": math.atan2,
    "sinh": math.sinh,
    "cosh": math.cosh,
    "tanh": math.tanh,
    "radians": math.radians,
    "degrees": math.degrees,
    "log": math.log,
    "log10": math.log10,
    "log2": math.log2,
    "ln": math.log,
    "exp": math.exp,
    "floor": math.floor,
    "ceil": math.ceil,
    "trunc": math.trunc,
    "factorial": math.factorial,
    "gcd": math.gcd,
    "lcm": math.lcm,
    "comb": math.comb,
    "perm": math.perm,
    "hypot": math.hypot,
}


def handle_tool(tool_name, params, config):
    try:
        if tool_name == "math_tools.calc":
            return _calc(params, config)
        if tool_name == "math_tools.calendar":
            return _calendar(params, config)
        if tool_name == "math_tools.geometry":
            return _geometry(params, config)
        if tool_name == "math_tools.stats":
            return _stats(params, config)
        if tool_name == "math_tools.algebra":
            return _algebra(params, config)
        if tool_name == "math_tools.convert":
            return _convert(params, config)
        if tool_name == "math_tools.fraction":
            return _fraction(params, config)
        return fail(f"Unbekanntes Tool: {tool_name}")
    except Exception as exc:
        return fail(f"Math Fehler: {exc}")


def _calc(params, config):
    payload = parse_payload(params, "expr")
    expr = first_text(payload, "expr", "expression", "term", "query")
    if not expr:
        return fail("Kein Ausdruck. Beispiel: math_tools.calc(sin(pi/2)+sqrt(16))")
    if len(expr) > 1000:
        return fail("Ausdruck zu lang (>1000 Zeichen).")
    result = SafeEval().eval(expr)
    lines = ["MATH_CALC", f"expr: {expr}", f"result: {fmt(result, config)}"]
    if isinstance(result, float):
        lines.append(f"scientific: {result:.10e}")
    return ok(truncate("\n".join(lines), config))


def _calendar(params, config):
    payload = parse_payload(params)
    action = (payload.get("action") or payload.get("op") or infer_calendar_action(payload)).strip().lower()
    tz = get_tz(config, payload)
    today = datetime.now(tz).date()

    if action == "now":
        now = datetime.now(tz).replace(microsecond=0)
        return ok(f"CALENDAR_NOW\ntimezone: {tz.key if hasattr(tz, 'key') else tz}\nnow: {now.isoformat()}\ndate: {now.date().isoformat()}\nweekday: {weekday_name(now.date())}")

    if action == "add":
        base = parse_date(payload.get("date") or payload.get("start") or today)
        amount = int(float(payload.get("amount", payload.get("value", 0))))
        unit = str(payload.get("unit") or "days").lower()
        out = add_date(base, amount, unit)
        return ok("\n".join(["CALENDAR_ADD", f"date: {base}", f"amount: {amount}", f"unit: {unit}", f"result: {out}", f"weekday: {weekday_name(out)}"]))

    if action == "diff":
        start = parse_date(payload.get("start") or payload.get("from") or today)
        end = parse_date(payload.get("end") or payload.get("to") or today)
        days = (end - start).days
        lines = ["CALENDAR_DIFF", f"start: {start}", f"end: {end}", f"days: {days}", f"weeks: {days / 7:.6g}", f"absolute_days: {abs(days)}"]
        lines.append(f"business_days: {business_days(start, end)}")
        return ok("\n".join(lines))

    if action == "weekday":
        d = parse_date(payload.get("date") or today)
        return ok(f"CALENDAR_WEEKDAY\ndate: {d}\nweekday: {weekday_name(d)}\niso_week: {d.isocalendar().week}\nday_of_year: {d.timetuple().tm_yday}")

    if action == "business_days":
        start = parse_date(payload.get("start") or today)
        end = parse_date(payload.get("end") or today)
        return ok(f"CALENDAR_BUSINESS_DAYS\nstart: {start}\nend: {end}\nbusiness_days: {business_days(start, end)}")

    if action == "month":
        year = int(payload.get("year") or today.year)
        month = int(payload.get("month") or today.month)
        text = calmod.month(year, month)
        return ok(f"CALENDAR_MONTH\nyear: {year}\nmonth: {month}\n{text}")

    return fail("Unbekannte calendar action. Nutze now, add, diff, weekday, business_days oder month.")


def _geometry(params, config):
    payload = parse_payload(params)
    shape = str(payload.get("shape") or payload.get("form") or "").strip().lower()
    if not shape:
        return fail("shape fehlt. Beispiel: {\"shape\":\"circle\",\"radius\":3}")
    p = {k: num(v) for k, v in payload.items() if k not in {"shape", "form"}}
    out = {"shape": shape}

    if shape in {"circle", "kreis"}:
        r = need(p, "radius", "r")
        out.update(radius=r, diameter=2 * r, area=math.pi * r * r, circumference=2 * math.pi * r)
    elif shape in {"rectangle", "rechteck"}:
        w = need(p, "width", "w", "breite")
        h = need(p, "height", "h", "hoehe")
        out.update(width=w, height=h, area=w * h, perimeter=2 * (w + h), diagonal=math.hypot(w, h))
    elif shape in {"square", "quadrat"}:
        s = need(p, "side", "a", "seite")
        out.update(side=s, area=s * s, perimeter=4 * s, diagonal=s * math.sqrt(2))
    elif shape in {"triangle", "dreieck"}:
        if all(k in p for k in ("a", "b", "c")):
            a, b, c = p["a"], p["b"], p["c"]
            semi = (a + b + c) / 2
            area = math.sqrt(max(0, semi * (semi - a) * (semi - b) * (semi - c)))
            out.update(a=a, b=b, c=c, perimeter=a + b + c, area=area)
        else:
            base = need(p, "base", "grundseite", "b")
            height = need(p, "height", "hoehe", "h")
            out.update(base=base, height=height, area=base * height / 2)
    elif shape in {"sphere", "kugel"}:
        r = need(p, "radius", "r")
        out.update(radius=r, surface_area=4 * math.pi * r * r, volume=4 / 3 * math.pi * r ** 3)
    elif shape in {"cylinder", "zylinder"}:
        r = need(p, "radius", "r")
        h = need(p, "height", "h", "hoehe")
        out.update(radius=r, height=h, base_area=math.pi * r * r, lateral_area=2 * math.pi * r * h, surface_area=2 * math.pi * r * (r + h), volume=math.pi * r * r * h)
    elif shape in {"cone", "kegel"}:
        r = need(p, "radius", "r")
        h = need(p, "height", "h", "hoehe")
        slant = math.hypot(r, h)
        out.update(radius=r, height=h, slant_height=slant, surface_area=math.pi * r * (r + slant), volume=math.pi * r * r * h / 3)
    elif shape in {"regular_polygon", "polygon", "regelmaessiges_polygon"}:
        n = int(need(p, "n", "sides"))
        side = need(p, "side", "a", "seite")
        if n < 3:
            return fail("Polygon braucht n >= 3.")
        perimeter = n * side
        area = n * side * side / (4 * math.tan(math.pi / n))
        out.update(n=n, side=side, perimeter=perimeter, area=area)
    else:
        return fail(f"Unbekannte Form: {shape}")

    return ok("GEOMETRY\n" + "\n".join(f"{k}: {fmt(v, config)}" for k, v in out.items()))


def _stats(params, config):
    payload = parse_payload(params, "data")
    data = parse_numbers(payload.get("data") or payload.get("values") or payload.get("query") or "")
    if not data:
        return fail("Keine Daten. Beispiel: math_tools.stats(1,2,3,4)")
    sorted_data = sorted(data)
    n = len(data)
    lines = [
        "STATS",
        f"count: {n}",
        f"sum: {fmt(sum(data), config)}",
        f"min: {fmt(min(data), config)}",
        f"max: {fmt(max(data), config)}",
        f"range: {fmt(max(data) - min(data), config)}",
        f"mean: {fmt(statistics.fmean(data), config)}",
        f"median: {fmt(statistics.median(data), config)}",
    ]
    if n >= 2:
        lines.extend(
            [
                f"variance_sample: {fmt(statistics.variance(data), config)}",
                f"stdev_sample: {fmt(statistics.stdev(data), config)}",
                f"q1: {fmt(percentile(sorted_data, 25), config)}",
                f"q3: {fmt(percentile(sorted_data, 75), config)}",
                f"p90: {fmt(percentile(sorted_data, 90), config)}",
            ]
        )
    return ok("\n".join(lines))


def _algebra(params, config):
    payload = parse_payload(params)
    typ = str(payload.get("type") or payload.get("kind") or "").strip().lower()
    coeffs = payload.get("coefficients") or payload.get("coeffs") or payload.get("a")
    constants = payload.get("constants") or payload.get("b")

    if typ in {"linear", "lineare"}:
        values = parse_numbers(coeffs if coeffs is not None else payload.get("values"))
        if len(values) != 2:
            return fail("linear braucht coefficients [a,b] fuer a*x+b=0.")
        a, b = values
        if a == 0:
            return fail("a darf nicht 0 sein.")
        x = -b / a
        return ok(f"ALGEBRA_LINEAR\nequation: {a}*x + {b} = 0\nx: {fmt(x, config)}")

    if typ in {"quadratic", "quadratisch"}:
        values = parse_numbers(coeffs if coeffs is not None else payload.get("values"))
        if len(values) != 3:
            return fail("quadratic braucht coefficients [a,b,c] fuer a*x^2+b*x+c=0.")
        a, b, c = values
        if a == 0:
            return fail("a darf nicht 0 sein. Nutze type=linear.")
        disc = b * b - 4 * a * c
        if disc >= 0:
            root = math.sqrt(disc)
            x1 = (-b + root) / (2 * a)
            x2 = (-b - root) / (2 * a)
        else:
            root = math.sqrt(-disc)
            x1 = complex(-b / (2 * a), root / (2 * a))
            x2 = complex(-b / (2 * a), -root / (2 * a))
        return ok(f"ALGEBRA_QUADRATIC\ndiscriminant: {fmt(disc, config)}\nx1: {fmt(x1, config)}\nx2: {fmt(x2, config)}")

    if typ in {"system2", "linear_system_2"}:
        matrix = payload.get("matrix") or payload.get("coefficients")
        rhs = payload.get("rhs") or constants
        if not isinstance(matrix, list) or len(matrix) != 2 or not isinstance(rhs, list) or len(rhs) != 2:
            return fail("system2 braucht matrix [[a,b],[c,d]] und rhs [e,f].")
        a, b = float(matrix[0][0]), float(matrix[0][1])
        c, d = float(matrix[1][0]), float(matrix[1][1])
        e, f = float(rhs[0]), float(rhs[1])
        det = a * d - b * c
        if det == 0:
            return fail("Determinante ist 0; System hat keine eindeutige Loesung.")
        x = (e * d - b * f) / det
        y = (a * f - e * c) / det
        return ok(f"ALGEBRA_SYSTEM2\ndeterminant: {fmt(det, config)}\nx: {fmt(x, config)}\ny: {fmt(y, config)}")

    return fail("Unbekannter Algebra-Typ. Nutze linear, quadratic oder system2.")


def _convert(params, config):
    payload = parse_payload(params)
    value = num(payload.get("value"))
    src = normalize_unit(payload.get("from") or payload.get("src") or payload.get("unit"))
    dst = normalize_unit(payload.get("to") or payload.get("dst") or payload.get("target"))
    if not src or not dst:
        return fail("from/to fehlen. Beispiel: {\"value\":10,\"from\":\"km\",\"to\":\"mi\"}")
    converted = convert_value(value, src, dst)
    return ok(f"UNIT_CONVERT\nvalue: {fmt(value, config)} {src}\nresult: {fmt(converted, config)} {dst}")


def _fraction(params, config):
    payload = parse_payload(params, "value")
    op = str(payload.get("op") or "").strip().lower()
    if op:
        values = payload.get("values") or []
        fracs = [to_fraction(v) for v in values]
        if not fracs:
            return fail("values fehlen.")
        result = fracs[0]
        for f in fracs[1:]:
            if op in {"add", "+"}:
                result += f
            elif op in {"sub", "-"}:
                result -= f
            elif op in {"mul", "*"}:
                result *= f
            elif op in {"div", "/"}:
                result /= f
            else:
                return fail("op muss add/sub/mul/div sein.")
    else:
        result = to_fraction(payload.get("value") or payload.get("query") or "")
    dec = float(result)
    return ok(f"FRACTION\nfraction: {result}\ndecimal: {fmt(dec, config)}\npercent: {fmt(dec * 100, config)}%")


class SafeEval:
    def eval(self, expr):
        tree = ast.parse(expr, mode="eval")
        return self.visit(tree.body)

    def visit(self, node):
        if isinstance(node, ast.Constant):
            if isinstance(node.value, (int, float)):
                return node.value
            raise ValueError("Nur Zahlen sind als Literal erlaubt.")
        if isinstance(node, ast.Name):
            if node.id in CONSTANTS:
                return CONSTANTS[node.id]
            raise ValueError(f"Unbekannter Name: {node.id}")
        if isinstance(node, ast.UnaryOp):
            value = self.visit(node.operand)
            if isinstance(node.op, ast.UAdd):
                return +value
            if isinstance(node.op, ast.USub):
                return -value
        if isinstance(node, ast.BinOp):
            left = self.visit(node.left)
            right = self.visit(node.right)
            if isinstance(node.op, ast.Add):
                return left + right
            if isinstance(node.op, ast.Sub):
                return left - right
            if isinstance(node.op, ast.Mult):
                return left * right
            if isinstance(node.op, ast.Div):
                return left / right
            if isinstance(node.op, ast.FloorDiv):
                return left // right
            if isinstance(node.op, ast.Mod):
                return left % right
            if isinstance(node.op, ast.Pow):
                if abs(right) > 10000:
                    raise ValueError("Exponent zu gross.")
                return left ** right
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in FUNCTIONS:
                raise ValueError("Nur freigegebene Mathefunktionen sind erlaubt.")
            name = node.func.id
            args = [self.visit(arg) for arg in node.args]
            if name == "factorial" and (len(args) != 1 or args[0] > 2000 or args[0] < 0):
                raise ValueError("factorial erlaubt 0..2000.")
            if name in {"comb", "perm"} and args and max(args) > 100000:
                raise ValueError("comb/perm Argumente zu gross.")
            return FUNCTIONS[name](*args)
        raise ValueError(f"Nicht erlaubter Ausdruck: {type(node).__name__}")


def parse_payload(params, default_key="query"):
    if isinstance(params, dict):
        return dict(params)
    if not params:
        return {}
    raw = params[0]
    if isinstance(raw, dict):
        return dict(raw)
    text = str(raw or "").strip()
    if text.startswith("{") and text.endswith("}"):
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass
    return {default_key: text}


def first_text(payload, *keys):
    for key in keys:
        value = payload.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def parse_numbers(value):
    if value is None:
        return []
    if isinstance(value, list):
        return [float(x) for x in value]
    if isinstance(value, (int, float)):
        return [float(value)]
    text = str(value)
    return [float(x) for x in text.replace(";", ",").split(",") if x.strip()]


def num(value):
    if isinstance(value, (int, float)):
        return float(value)
    if value is None or str(value).strip() == "":
        raise ValueError("Zahlenwert fehlt.")
    return float(str(value).strip().replace(",", "."))


def need(payload, *keys):
    for key in keys:
        if key in payload:
            return num(payload[key])
    raise ValueError(f"Fehlender Parameter: {'/'.join(keys)}")


def fmt(value, config):
    precision = int(config.get("precision", 10) or 10)
    if isinstance(value, complex):
        return f"{fmt(value.real, config)} {'+' if value.imag >= 0 else '-'} {fmt(abs(value.imag), config)}i"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return str(value)
        return f"{value:.{precision}g}"
    return str(value)


def truncate(text, config):
    limit = int(config.get("max_output_chars", 6000) or 6000)
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 30)] + "\n...[gekuerzt]"


def ok(data):
    return {"success": True, "data": data}


def fail(data):
    return {"success": False, "data": data}


def get_tz(config, payload):
    name = str(payload.get("timezone") or config.get("timezone") or "Europe/Berlin")
    try:
        return ZoneInfo(name)
    except Exception:
        return timezone.utc


def parse_date(value):
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    text = str(value).strip()
    if text.lower() in {"today", "heute", "now"}:
        return date.today()
    return datetime.fromisoformat(text.replace("Z", "+00:00")).date()


def weekday_name(d):
    names = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]
    return names[d.weekday()]


def add_date(base, amount, unit):
    unit = unit.lower()
    if unit.startswith("day") or unit.startswith("tag"):
        return base + timedelta(days=amount)
    if unit.startswith("week") or unit.startswith("woch"):
        return base + timedelta(weeks=amount)
    if unit.startswith("month") or unit.startswith("monat"):
        month = base.month - 1 + amount
        year = base.year + month // 12
        month = month % 12 + 1
        day = min(base.day, calmod.monthrange(year, month)[1])
        return date(year, month, day)
    if unit.startswith("year") or unit.startswith("jahr"):
        year = base.year + amount
        day = min(base.day, calmod.monthrange(year, base.month)[1])
        return date(year, base.month, day)
    raise ValueError(f"Unbekannte Einheit: {unit}")


def business_days(start, end):
    step = 1 if end >= start else -1
    count = 0
    cur = start
    while cur != end:
        if cur.weekday() < 5:
            count += step
        cur += timedelta(days=step)
    return count


def infer_calendar_action(payload):
    if "start" in payload and "end" in payload:
        return "diff"
    if "amount" in payload or "unit" in payload:
        return "add"
    if "year" in payload and "month" in payload:
        return "month"
    if "date" in payload:
        return "weekday"
    return "now"


def percentile(sorted_data, p):
    if not sorted_data:
        return 0
    if len(sorted_data) == 1:
        return sorted_data[0]
    k = (len(sorted_data) - 1) * (p / 100)
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return sorted_data[int(k)]
    return sorted_data[f] * (c - k) + sorted_data[c] * (k - f)


UNIT_GROUPS = {
    "length": {"m": 1, "meter": 1, "km": 1000, "cm": 0.01, "mm": 0.001, "mi": 1609.344, "mile": 1609.344, "yd": 0.9144, "ft": 0.3048, "in": 0.0254},
    "mass": {"kg": 1, "g": 0.001, "mg": 0.000001, "lb": 0.45359237, "oz": 0.028349523125, "t": 1000},
    "time": {"s": 1, "sec": 1, "min": 60, "h": 3600, "hour": 3600, "day": 86400, "week": 604800},
    "area": {"m2": 1, "sqm": 1, "km2": 1_000_000, "cm2": 0.0001, "ha": 10000, "acre": 4046.8564224, "ft2": 0.09290304},
    "volume": {"m3": 1, "l": 0.001, "ml": 0.000001, "cm3": 0.000001, "gal": 0.003785411784, "ft3": 0.028316846592},
    "angle": {"rad": 1, "deg": math.pi / 180, "degree": math.pi / 180},
}


def normalize_unit(unit):
    return str(unit or "").strip().lower().replace("²", "2").replace("³", "3")


def convert_value(value, src, dst):
    if src in {"c", "celsius", "°c"} or dst in {"c", "celsius", "°c", "f", "fahrenheit", "°f", "k", "kelvin"}:
        return convert_temp(value, src, dst)
    for _group, units in UNIT_GROUPS.items():
        if src in units and dst in units:
            return value * units[src] / units[dst]
    raise ValueError(f"Nicht kompatible oder unbekannte Einheiten: {src} -> {dst}")


def convert_temp(value, src, dst):
    if src in {"c", "celsius", "°c"}:
        c = value
    elif src in {"f", "fahrenheit", "°f"}:
        c = (value - 32) * 5 / 9
    elif src in {"k", "kelvin"}:
        c = value - 273.15
    else:
        raise ValueError(f"Unbekannte Temperaturquelle: {src}")
    if dst in {"c", "celsius", "°c"}:
        return c
    if dst in {"f", "fahrenheit", "°f"}:
        return c * 9 / 5 + 32
    if dst in {"k", "kelvin"}:
        return c + 273.15
    raise ValueError(f"Unbekanntes Temperaturziel: {dst}")


def to_fraction(value):
    if isinstance(value, Fraction):
        return value
    text = str(value).strip()
    if not text:
        raise ValueError("Bruchwert fehlt.")
    if text.endswith("%"):
        return Fraction(Decimal(text[:-1])) / 100
    return Fraction(text)


if __name__ == "__main__":
    for line in sys.stdin:
        try:
            req = json.loads(line.strip())
            if req.get("action") == "describe":
                print(json.dumps(MODULE), flush=True)
            elif req.get("action") == "handle_tool":
                result = handle_tool(req["tool"], req.get("params", []), req.get("config", {}))
                print(json.dumps(result), flush=True)
            else:
                print(json.dumps({"error": f"Unknown action: {req.get('action')}"}), flush=True)
        except Exception as exc:
            print(json.dumps({"error": str(exc)}), flush=True)
