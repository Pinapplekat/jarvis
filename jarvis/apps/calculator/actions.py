import ast
import math
import operator

from jarvis.contract.api import ActionError, AppContext, Result

MAX_EXPONENT = 1000
MAX_DIGITS = 1000

CONSTANTS = {"pi": math.pi, "e": math.e}
# name -> (function, fewest args, most args)
FUNCTIONS = {
    "sqrt": (math.sqrt, 1, 1), "sin": (math.sin, 1, 1), "cos": (math.cos, 1, 1), "tan": (math.tan, 1, 1),
    "asin": (math.asin, 1, 1), "acos": (math.acos, 1, 1), "atan": (math.atan, 1, 1),
    "log": (math.log, 1, 2), "log10": (math.log10, 1, 1), "exp": (math.exp, 1, 1),
    "abs": (abs, 1, 1), "round": (round, 1, 2), "floor": (math.floor, 1, 1), "ceil": (math.ceil, 1, 1),
}
BINARY = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod,
}


def _too_many_digits(n: int) -> bool:
    return n.bit_length() > MAX_DIGITS * 3.33


def _power(base, exp):
    if abs(exp) > MAX_EXPONENT:
        raise ActionError(f"That exponent is too large (the limit is {MAX_EXPONENT}).")
    # Check the size before computing, so a huge power never gets built.
    if isinstance(base, int) and isinstance(exp, int) and exp > 0 and abs(base) > 1:
        if exp * math.log10(abs(base)) >= MAX_DIGITS:
            raise ActionError(f"That result would have more than {MAX_DIGITS} digits.")
    return base**exp


def _sqrt(x):
    if isinstance(x, int) and x >= 0 and math.isqrt(x) ** 2 == x:
        return math.isqrt(x)  # perfect squares stay exact integers
    return math.sqrt(x)


def _eval(node):
    if isinstance(node, ast.Expression):
        return _eval(node.body)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise ActionError("Only numbers are allowed in an expression.")
        return node.value
    if isinstance(node, ast.Name):
        if node.id not in CONSTANTS:
            raise ActionError(f"I don't know '{node.id}'. The constants are pi and e.")
        return CONSTANTS[node.id]
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        value = _eval(node.operand)
        return -value if isinstance(node.op, ast.USub) else value
    if isinstance(node, ast.BinOp):
        left, right = _eval(node.left), _eval(node.right)
        if isinstance(node.op, ast.Pow):
            result = _power(left, right)
        elif type(node.op) in BINARY:
            result = BINARY[type(node.op)](left, right)
        else:
            raise ActionError("That operator isn't supported. Use + - * / // % ** and parentheses.")
        if isinstance(result, int) and _too_many_digits(result):
            raise ActionError(f"That result would have more than {MAX_DIGITS} digits.")
        return result
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id not in FUNCTIONS or node.keywords:
            raise ActionError("I can only call these functions: " + " ".join(FUNCTIONS) + ".")
        fn, low, high = FUNCTIONS[node.func.id]
        args = [_eval(a) for a in node.args]
        if not low <= len(args) <= high:
            raise ActionError(f"{node.func.id} takes {low if low == high else f'{low} or {high}'} argument(s).")
        return _sqrt(*args) if node.func.id == "sqrt" else fn(*args)
    raise ActionError("That isn't an arithmetic expression I can evaluate.")


def _show(value, precision: int) -> str:
    if isinstance(value, int):
        return str(value)
    return format(value, f".{precision}g")


def evaluate(ctx: AppContext, expression: str) -> Result:
    try:
        tree = ast.parse(expression.strip(), mode="eval")
    except (SyntaxError, ValueError, RecursionError):
        raise ActionError(f"'{expression}' isn't a valid expression.") from None
    try:
        value = _eval(tree)
    except ZeroDivisionError:
        raise ActionError("That divides by zero.") from None
    except OverflowError:
        raise ActionError("That number is too large to work with.") from None
    except (ValueError, TypeError) as e:
        raise ActionError(f"That isn't defined ({e}).") from None
    except RecursionError:
        raise ActionError("That expression is nested too deeply.") from None
    if isinstance(value, complex):
        raise ActionError("That result isn't a real number.")
    text = _show(value, ctx.settings["precision"])
    return Result(text, data={"expression": expression, "result": text})
