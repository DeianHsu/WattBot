"""确定性的答案值审计：只修正解释中能明确复核的算术/百分比错误。"""
import re


NUMBER = r"[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?:[eE][+-]?\d+)?"
SIMPLE_NUMBER = r"[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
UNIT = r"[A-Za-z%][A-Za-z%]*"
MULTIPLIER_UNITS = {"time", "multiplier", "x", "fold"}
WORD_LIMIT = re.compile(r"at most\s+(\d+|one|two|three|four|five)\s+words?", re.I)
NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5}
LOW_INFORMATION = {"energy", "full", "general", "general-purpose", "overall", "complete", "total"}


def _number(text):
    return float(text.replace(",", ""))


def _format(value):
    if abs(value - round(value)) < 1e-9:
        return str(int(round(value)))
    return format(value, ".15g")


def _normalize_unit(unit):
    text = (unit or "").strip().lower().strip(".,;:")
    if len(text) > 2 and text.endswith("s") and not text.endswith("ss"):
        text = text[:-1]
    return text


def _numeric_unit(unit):
    return bool(unit and _normalize_unit(unit) not in ("is_blank", "nan", "none"))


def _unit_matches(candidate, expected):
    return not candidate or _normalize_unit(candidate) == _normalize_unit(expected)


def _same(a, b):
    return abs(a - b) <= max(abs(b) * 1e-3, 1e-3)


def _last_number(explanation):
    matches = list(re.finditer(rf"({NUMBER})\s*(?:percent|%)?", explanation or "", re.I))
    return _number(matches[-1].group(1)) if matches else None


def _explicit_answer(explanation):
    """只接受解释明确写 answer_value 的最终值，避免把普通 ratio 当修正。"""
    pattern = re.compile(
        rf"answer_value\s*(?:should be|is|:|=)?\s*({NUMBER})\s*({UNIT})?",
        re.I,
    )
    matches = list(pattern.finditer(explanation or ""))
    if not matches:
        return None
    return _number(matches[-1].group(1)), matches[-1].group(2)


def _last_equation(explanation):
    """返回最后一个完整的“数 op 数 = 数 单位”算式。"""
    pattern = re.compile(
        rf"({SIMPLE_NUMBER})\s*({UNIT})?\s*(×|x|\*|/|÷|\+|-)\s*"
        rf"({SIMPLE_NUMBER})\s*({UNIT})?\s*=\s*({SIMPLE_NUMBER})\s*({UNIT})?",
        re.I,
    )
    found = None
    text = explanation or ""
    for match in pattern.finditer(text):
        # 结果必须带单位，且算式不能是 10^7、a×b×c 等更长表达式的一段。
        if not match.group(7):
            continue
        before = text[match.start() - 1:match.start()] if match.start() else ""
        if before and (before in "^×*/+-(0123456789." or before.isdigit()):
            continue
        found = match
    return found


def _compute(left, operator, right):
    if operator in ("×", "x", "*"):
        return left * right
    if operator in ("/", "÷"):
        return right and left / right
    if operator == "+":
        return left + right
    if operator == "-":
        return left - right
    return None


def normalize_categorical_answer(value, question):
    """按题目的词数限制和末尾从句收短类别答案；只处理明确可删的修饰语。"""
    text = (value or "").strip()
    if not text or text.lower() == "is_blank" or re.fullmatch(NUMBER, text):
        return None
    original = text

    # 末尾用途/范围从句不是核心术语：required for、of/that/which 引导的说明。
    text = re.sub(r"\s+(?:required|used|needed)\s+for\b.*$", "", text, flags=re.I).strip()
    clause = re.match(r"(.+?)\s+(?:of|that|which)\s+.+$", text, re.I)
    if clause and len(clause.group(1).split()) >= 2:
        text = clause.group(1).strip()

    match = WORD_LIMIT.search(question or "")
    if match:
        raw = match.group(1).lower()
        limit = int(raw) if raw.isdigit() else NUMBER_WORDS.get(raw)
        if limit and len(text.split()) > limit:
            # 末尾括号缩写通常只是重复说明；题目有词数限制时才删除。
            text = re.sub(r"\s*\([A-Za-z0-9 .-]{2,10}\)\s*$", "", text).strip()
            if len(text.split()) > limit:
                words = [word for word in text.split()
                         if word.lower().strip(",.") not in LOW_INFORMATION]
                if len(words) <= limit:
                    text = " ".join(words)
                else:
                    return None

    return text if text and text != original else None


def audit_numeric_answer(question, answer_value, explanation, unit):
    """返回建议的数值字符串；没有明确可复核的修正时返回 None。"""
    if not answer_value or answer_value.lower() == "is_blank" or not _numeric_unit(unit):
        return None
    try:
        current = float(answer_value)
    except ValueError:
        return None
    if not explanation:
        return None

    # 1) 640 percent 被写成 640 times 时，按倍率/百分比换算。
    if _normalize_unit(unit) in MULTIPLIER_UNITS and current > 100:
        percent = list(re.finditer(rf"({NUMBER})\s*(?:percent|%)", explanation, re.I))
        if percent:
            candidate = _number(percent[-1].group(1)) / 100
            if not _same(candidate, current):
                return _format(candidate)

    # 2) 解释明确给出 answer_value 时，优先采用该值。
    explicit = _explicit_answer(explanation)
    if explicit:
        candidate, candidate_unit = explicit
        if _unit_matches(candidate_unit, unit) and not _same(candidate, current):
            return _format(candidate)

    # 3) 简单算式：只有算式结果就是解释最后一个数字时才采用可复核值。
    match = _last_equation(explanation)
    if match:
        result = _number(match.group(6))
        last = _last_number(explanation)
        if last is None or not _same(last, result):
            return None
        if not _unit_matches(match.group(7), unit):
            return None
        computed = _compute(_number(match.group(1)), match.group(3), _number(match.group(4)))
        if computed is None:
            return None
        # 只修正明显错误，避免把三位有效数字的舍入改成中间精度。
        if abs(computed - current) > max(abs(current) * 0.005, 1e-3):
            return _format(computed)
    return None
