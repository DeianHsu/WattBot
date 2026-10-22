"""答案值的通用约束检查：数量审计、词数限制和类别格式。

规则只依赖题目显式约束、答案类型和数值代数，不依赖题号、论文或特定短语。
"""
import ast
import math
import re


NUMBER = r"[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?:[eE][+-]?\d+)?"
UNIT = r"[A-Za-z%][A-Za-z%]*"
MULTIPLIER_UNITS = {"time", "multiplier", "x", "fold"}
WORD_LIMIT = re.compile(r"at most\s+(\d+|one|two|three|four|five)\s+words?", re.I)
NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5}


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


def _word_limit(question):
    match = WORD_LIMIT.search(question or "")
    if not match:
        return None
    raw = match.group(1).lower()
    return int(raw) if raw.isdigit() else NUMBER_WORDS.get(raw)


def _is_acronym(prefix, candidate):
    """括号内容是否是前面词的缩写；只对真正的缩写做删除。"""
    initials = "".join(word[0] for word in prefix.split() if word and word[0].isalpha())
    return len(candidate) >= 2 and candidate.isalpha() and initials.lower() == candidate.lower()


def normalize_categorical_answer(value, question):
    """只按题目显式词数限制删除末尾重复的缩写；不做短语表或语义截断。"""
    text = (value or "").strip()
    limit = _word_limit(question)
    if not limit or not text or text.lower() == "is_blank":
        return None
    if len(text.split()) <= limit or re.fullmatch(NUMBER, text):
        return None
    match = re.search(r"^(.*?)\s*\(([A-Za-z0-9]{2,10})\)$", text)
    if match and _is_acronym(match.group(1), match.group(2)):
        candidate = match.group(1).strip()
        if candidate and len(candidate.split()) <= limit:
            return candidate
    # 只超一个词时，删除紧邻中心词的普通形容词修饰语；不改实体、版本或首词。
    words = text.split()
    if len(words) == limit + 1 and words[-2].isalpha() and words[-2].islower():
        candidate = " ".join([*words[:-2], words[-1]])
        if len(candidate.split()) <= limit:
            return candidate
    return None


def answer_constraint_violations(value, question, unit):
    """返回题目显式约束被违反的通用原因，供格式重写或诊断使用。"""
    text = (value or "").strip()
    if not text or text.lower() == "is_blank":
        return []
    reasons = []
    limit = _word_limit(question)
    if limit and len(text.split()) > limit:
        reasons.append(f"word_limit:{limit}")
    if re.match(r"^\s*true\s+or\s+false\b", question or "", re.I) and text not in ("0", "1"):
        reasons.append("boolean_format")
    if re.search(r"\b(?:name both|which two|two (?:values|models|gpus|names|numbers))\b",
                 question or "", re.I) and not text.startswith(("(", "[")):
        reasons.append("two_values")
    return reasons


def calculate(expression):
    """安全执行只含数字和算术函数的表达式，用于程序化数值题。"""
    if not expression or len(expression) > 500:
        return None
    try:
        tree = ast.parse(expression, mode="eval")
        value = _eval_arithmetic(tree.body)
    except (SyntaxError, ValueError, TypeError, ZeroDivisionError, OverflowError):
        return None
    if not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    if abs(value) > 1e18:
        return None
    return float(value)


def _eval_arithmetic(node):
    if isinstance(node, ast.Constant) and type(node.value) in (int, float):
        return node.value
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        value = _eval_arithmetic(node.operand)
        return value if isinstance(node.op, ast.UAdd) else -value
    if isinstance(node, ast.BinOp):
        left, right = _eval_arithmetic(node.left), _eval_arithmetic(node.right)
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            return left * right
        if isinstance(node.op, ast.Div):
            return left / right
        if isinstance(node.op, ast.Mod):
            return left % right
        if isinstance(node.op, ast.Pow):
            if abs(right) > 10 or abs(left) > 1e6:
                raise ValueError("exponent too large")
            return left ** right
    if isinstance(node, (ast.List, ast.Tuple)):
        return [_eval_arithmetic(item) for item in node.elts]
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        functions = {"sum": sum, "min": min, "max": max, "abs": abs, "round": round}
        function = functions.get(node.func.id)
        if function is None or node.keywords:
            raise ValueError("unsupported function")
        args = [_eval_arithmetic(item) for item in node.args]
        return function(*args)
    raise ValueError("unsupported expression")


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


def audit_numeric_answer(question, answer_value, explanation, unit):
    """只做高置信修正：显式 answer_value 或百分比→倍率；不改普通解释算式。"""
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
    return None
