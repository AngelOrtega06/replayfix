"""Validate the small repair language used by this demo; never execute it here.

This is a scope check, not a general-purpose Python sandbox. Generated code
still runs only inside the restricted Docker container.
"""

import ast
import re


class PatchRejected(ValueError):
    pass


ALLOWED_NODES = (
    ast.Module, ast.FunctionDef, ast.arguments, ast.arg, ast.Expr, ast.Constant,
    ast.Assign, ast.Name, ast.Load, ast.Store, ast.Call, ast.Attribute,
    ast.Subscript, ast.Tuple, ast.List, ast.If, ast.Compare, ast.Is, ast.IsNot,
    ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.UnaryOp, ast.Not,
    ast.BoolOp, ast.And, ast.Or, ast.Try, ast.ExceptHandler, ast.Raise,
    ast.Return, ast.Pass,
)
SQL_FORMS = {
    "BEGINIMMEDIATE", "BEGINIMMEDIATETRANSACTION", "COMMIT", "ROLLBACK",
    "INSERTINTOORDERS(EVENT_ID,CUSTOMER_ID)VALUES(?,?)",
}
SELECT_FORM = re.compile(
    r"SELECT(?:1|\*|EVENT_ID)FROMORDERSWHEREEVENT_ID=\?(?:LIMIT1)?"
)


def validate_function(text):
    if not isinstance(text, str) or len(text) > 16000:
        raise PatchRejected("Return a replacement function under 16,000 characters.")
    text = text.strip()
    fence = re.fullmatch(r"```(?:python|py)?\s*\n(.*?)\n```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError):
        raise PatchRejected("Return only valid Python for def handle_webhook(db, event).")
    if len(tree.body) != 1 or not isinstance(tree.body[0], ast.FunctionDef):
        raise PatchRejected("Return exactly one function, with no other top-level code.")
    fn = tree.body[0]
    args = fn.args
    if (fn.name != "handle_webhook" or fn.decorator_list or fn.returns
            or getattr(fn, "type_params", []) or args.posonlyargs or args.kwonlyargs
            or args.vararg or args.kwarg or args.defaults or args.kw_defaults
            or [a.arg for a in args.args] != ["db", "event"]
            or any(a.annotation for a in args.args)):
        raise PatchRejected("Use exactly def handle_webhook(db, event), without annotations or decorators.")
    nodes = list(ast.walk(tree))
    if len(nodes) > 600:
        raise PatchRejected("Keep the replacement small (under 600 syntax nodes).")
    locals_ = set()
    for node in nodes:
        if not isinstance(node, ALLOWED_NODES) or (isinstance(node, ast.FunctionDef) and node is not fn):
            raise PatchRejected("Use simple assignments, if, try/except, and SQLite calls; no imports or loops.")
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if not isinstance(target, ast.Name) or target.id in {"db", "event", "Exception"}:
                    raise PatchRejected("Assign only to local names; keep db and event unchanged.")
                locals_.add(target.id)
        if isinstance(node, ast.ExceptHandler):
            if node.type is not None and not (isinstance(node.type, ast.Name) and node.type.id == "Exception"):
                raise PatchRejected("Use except Exception for rollback, if needed.")
            if node.name:
                if node.name in {"db", "event", "Exception"}:
                    raise PatchRejected("Do not shadow db, event, or Exception.")
                locals_.add(node.name)
    for node in nodes:
        if isinstance(node, ast.Name):
            if node.id.startswith("_") or node.id not in locals_ | {"db", "event", "Exception"}:
                raise PatchRejected("Use only db, event, Exception, and local variables.")
        if isinstance(node, ast.Attribute) and node.attr not in {"execute", "fetchone", "commit", "rollback", "in_transaction"}:
            raise PatchRejected("Only SQLite execute/fetchone/commit/rollback and db.in_transaction are allowed.")
        if isinstance(node, ast.Call):
            method = node.func
            if not isinstance(method, ast.Attribute) or node.keywords:
                raise PatchRejected("Only the allowed SQLite methods may be called, with positional arguments.")
            if method.attr == "execute":
                if not isinstance(method.value, ast.Name) or method.value.id != "db":
                    raise PatchRejected("Run SQL using db.execute directly.")
                if len(node.args) not in {1, 2} or not isinstance(node.args[0], ast.Constant) or not isinstance(node.args[0].value, str):
                    raise PatchRejected("Pass literal SQL to db.execute and bind values with ? parameters.")
                sql = re.sub(r"\s+", "", node.args[0].value.strip().rstrip(";").upper())
                if sql not in SQL_FORMS and not SELECT_FORM.fullmatch(sql):
                    literal = " ".join(node.args[0].value.split())
                    detail = repr(literal[:180]) + (" (truncated)" if len(literal) > 180 else "")
                    raise PatchRejected(
                        "Unsupported SQL at function line " + str(node.lineno) + ": " + detail + ". "
                        "Allowed: BEGIN IMMEDIATE; SELECT 1 FROM orders WHERE event_id = ? "
                        "(also SELECT * or SELECT event_id; optional LIMIT 1); "
                        "INSERT INTO orders (event_id, customer_id) VALUES (?, ?); COMMIT; ROLLBACK. "
                        "BEGIN IMMEDIATE TRANSACTION is also accepted. No schema changes."
                    )
            elif method.attr in {"commit", "rollback"}:
                if node.args or not isinstance(method.value, ast.Name) or method.value.id != "db":
                    raise PatchRejected("Use db.commit() or db.rollback() without arguments.")
            elif method.attr != "fetchone" or node.args:
                raise PatchRejected("Only db.execute(...), cursor.fetchone(), db.commit(), and db.rollback() may be called.")
    return text + "\n"


def replace_function(source, function):
    """Create an export/diff while preserving every line outside the function."""
    tree = ast.parse(source)
    target = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "handle_webhook")
    lines = source.splitlines(keepends=True)
    return "".join(lines[:target.lineno - 1]) + function + "".join(lines[target.end_lineno:])
