"""Bounded, pure Python lexical facts, never a resolver or evidence authority."""
from __future__ import annotations

import ast
import symtable
import time
from typing import Any

MAX_BINDING_SOURCE_BYTES = 1_000_000
MAX_BINDING_NODES = 100_000
MAX_BINDING_CALLS = 8_192
MAX_BINDING_SECONDS = .25


def source_bindings(content: str) -> tuple[dict[Any, Any], dict[str, int], frozenset[str] | None]:
    """Return call bindings and unmodified, unconditional module declarations.

    Unsupported/dynamic scopes stay unknown. Results contain syntax facts only;
    callers must validate source blobs and resolve targets in their pinned index.
    """
    if len(content) > MAX_BINDING_SOURCE_BYTES or len(content.encode('utf-8')) > MAX_BINDING_SOURCE_BYTES:
        return {}, {}, None
    deadline = time.process_time() + MAX_BINDING_SECONDS
    scopes: list[dict[str, Any]] = []
    calls: list[tuple[int, int, str, str | None]] = []
    nodes = 0

    def scope_type(table: Any) -> str:
        kind = table.get_type()
        return getattr(kind, 'value', kind)

    def scan(body: list[ast.stmt], table: Any, parent: int | None, owner: int, depth: int = 0) -> None:
        if depth > 64:
            raise ValueError('scope depth')
        position = len(scopes)
        scope = {'table': table, 'parent': parent, 'owner': owner, 'bindings': {}, 'dynamic': False}
        scopes.append(scope)
        children: list[tuple[Any, Any]] = []
        child_tables: dict[tuple[str, int], list[Any]] = {}
        for child in table.get_children():
            if scope_type(child) in {'function', 'class'}:
                child_tables.setdefault((child.get_name(), child.get_lineno()), []).append(child)

        def bind(name: str, value: Any) -> None:
            scope['bindings'][name] = value if name not in scope['bindings'] else None
            symbol = table.lookup(name)
            if parent is not None and symbol.is_global():
                scopes[0]['bindings'][name] = None
            elif symbol.is_nonlocal():
                outer = parent
                while outer is not None:
                    if scope_type(scopes[outer]['table']) != 'class' and name in scopes[outer]['bindings']:
                        scopes[outer]['bindings'][name] = None
                        break
                    outer = scopes[outer]['parent']

        class Collect(ast.NodeVisitor):
            conditional = 0

            def visit(self, node: ast.AST) -> None:
                nonlocal nodes
                nodes += 1
                if nodes > MAX_BINDING_NODES or time.process_time() >= deadline:
                    raise ValueError('binding budget')
                super().visit(node)

            def generic_visit(self, node: ast.AST) -> None:
                conditional = isinstance(node, (ast.If, ast.For, ast.AsyncFor, ast.While, ast.Try,
                                                ast.TryStar, ast.With, ast.AsyncWith, ast.Match))
                self.conditional += int(conditional)
                super().generic_visit(node)
                self.conditional -= int(conditional)

            def visit_FunctionDef(self, node: Any) -> None:
                bind(node.name, None if self.conditional or node.decorator_list else
                     ('definition', '', 0, node.name, node.lineno))
                matching = child_tables.get((node.name, node.lineno), [])
                if len(matching) == 1:
                    children.append((node, matching[0]))
                # Defaults/decorators run in the enclosing scope, not the body.
                for expression in [*node.decorator_list,
                                   *getattr(node, 'bases', []),
                                   *(item.value for item in getattr(node, 'keywords', [])),
                                   *getattr(getattr(node, 'args', None), 'defaults', []),
                                   *(item for item in getattr(getattr(node, 'args', None), 'kw_defaults', []) if item)]:
                    self.visit(expression)

            visit_AsyncFunctionDef = visit_FunctionDef
            visit_ClassDef = visit_FunctionDef

            def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
                for alias in node.names:
                    if alias.name == '*':
                        scope['dynamic'] = True
                    else:
                        bind(alias.asname or alias.name, None if self.conditional else
                             ('import', node.module or '', node.level, alias.name, 0))

            def visit_Import(self, node: ast.Import) -> None:
                for alias in node.names:
                    bind(alias.asname or alias.name.split('.')[0], None if self.conditional else
                         ('module', alias.name if alias.asname else alias.name.split('.')[0], 0, alias.name, 0))

            def visit_Name(self, node: ast.Name) -> None:
                if isinstance(node.ctx, (ast.Store, ast.Del)):
                    bind(node.id, None)

            def visit_Attribute(self, node: Any) -> None:
                if isinstance(node.ctx, (ast.Store, ast.Del)):
                    value = node.value
                    while isinstance(value, (ast.Attribute, ast.Subscript)):
                        value = value.value
                    if isinstance(value, ast.Name):
                        bind(value.id, None)
                self.generic_visit(node)

            visit_Subscript = visit_Attribute

            def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
                if node.name:
                    bind(node.name, None)
                self.generic_visit(node)

            def visit_MatchAs(self, node: ast.MatchAs) -> None:
                if node.name:
                    bind(node.name, None)
                self.generic_visit(node)

            visit_MatchStar = visit_MatchAs

            def visit_MatchMapping(self, node: ast.MatchMapping) -> None:
                if node.rest:
                    bind(node.rest, None)
                self.generic_visit(node)

            def visit_Call(self, node: ast.Call) -> None:
                name, receiver = '', None
                if isinstance(node.func, ast.Name):
                    name = node.func.id
                    if name in {'exec', 'eval', 'globals', 'locals', '__import__', 'setattr', 'delattr', 'vars'}:
                        scope['dynamic'] = True
                elif isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
                    name, receiver = node.func.attr, node.func.value.id
                elif (isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Attribute)
                      and isinstance(node.func.value.value, ast.Name)):
                    name = node.func.attr
                    receiver = node.func.value.value.id + '.' + node.func.value.attr
                if name:
                    if len(calls) >= MAX_BINDING_CALLS:
                        raise ValueError('call budget')
                    calls.append((position, node.lineno, name, receiver))
                self.generic_visit(node)

            def visit_Lambda(self, node: ast.AST) -> None:
                nonlocal nodes
                # Their implicit scopes (and escaping walrus assignments) are
                # not modelled; never infer their calls from the outer scope.
                for child in ast.walk(node):
                    nodes += 1
                    if nodes > MAX_BINDING_NODES or time.process_time() >= deadline:
                        raise ValueError('binding budget')
                    if isinstance(child, ast.NamedExpr):
                        self.visit(child.target)

            visit_ListComp = visit_Lambda
            visit_SetComp = visit_Lambda
            visit_DictComp = visit_Lambda
            visit_GeneratorExp = visit_Lambda

        collector = Collect()
        for statement in body:
            collector.visit(statement)
        for node, child in children:
            scan(node.body, child, position, node.lineno, depth + 1)

    def binding(position: int, name: str) -> Any:
        scope = scopes[position]
        if scope['dynamic']:
            return None
        try:
            symbol = scope['table'].lookup(name)
        except KeyError:
            return None
        if symbol.is_parameter() or symbol.is_nonlocal():
            return None
        if position and symbol.is_global():
            return binding(0, name)
        if symbol.is_free():
            parent = scope['parent']
            while parent is not None and scope_type(scopes[parent]['table']) == 'class':
                parent = scopes[parent]['parent']
            return binding(parent, name) if parent is not None else None
        return scope['bindings'].get(name)

    try:
        tree = ast.parse(content)
        table = symtable.symtable(content, '<pinned source>', 'exec')
        scan(tree.body, table, None, 0)
        result, seen_calls = {}, set()
        for position, line, name, receiver in calls:
            key = (scopes[position]['owner'], line, name, receiver.rsplit('.', 1)[-1] if receiver else None)
            # The persisted callsite records only the immediate receiver. Two
            # different qualified receivers on one line cannot prove one edge.
            if key in seen_calls:
                result.pop(key, None)
                continue
            seen_calls.add(key)
            value = binding(position, receiver.split('.')[0] if receiver else name)
            if value is None:
                continue
            if receiver is not None:
                if value[0] == 'module':
                    if '.' in receiver:
                        module = value[1] + '.' + receiver.split('.', 1)[1]
                        if module != value[3]:
                            continue
                        value = ('module-member', module, 0, name, 0)
                    else:
                        value = ('import', value[1], 0, name, 0)
                elif value[0] == 'import' and '.' not in receiver:
                    value = ('module-member', '.'.join(part for part in (value[1], value[3]) if part), value[2], name, 0)
                else:
                    continue
            elif value[0] == 'module':
                continue
            result[key] = value
        declarations = {name: value[4] for name, value in scopes[0]['bindings'].items()
                        if value is not None and value[0] == 'definition' and not scopes[0]['dynamic']}
        namespace = frozenset(scopes[0]['bindings']) if not scopes[0]['dynamic'] else None
        return (result, declarations, namespace) if time.process_time() < deadline else ({}, {}, None)
    except (SyntaxError, ValueError, RecursionError, KeyError, TypeError, OverflowError, MemoryError):
        return {}, {}, None
