"""Tools for the Software Development employee (agent_type 'developer').

THE BOUNDARY THIS MODULE ENFORCES
---------------------------------
The Developer employee never modifies project code. Read the tool list: there
is no tool that writes to a path, runs a command, stages a change, commits,
merges, pushes or deploys. That absence is the design. An instruction in a
prompt not to touch the code is a request; a missing tool is a guarantee, and
only the second one survives a model that misunderstands its brief.

Every generation tool therefore ends the same way. It writes a
``CodeArtifact`` row -- kind, title, language, content, explanation, suggested
path -- and returns that content along with the artefact id. The artefact is a
proposal about code, not a change to code. A human developer reads it, judges
it, and applies it by hand if they agree. ``suggested_path`` is advice about
where it might belong; nothing is ever written there.

The tools that reach GitHub, Jira, Slack or Drive to *write* return a
``Proposal`` and stop, exactly as everywhere else in the platform. The tools
that only read from those services act at once, because reading changes
nothing.

WHY THE GENERATORS ARE DETERMINISTIC
------------------------------------
No tool here calls a language model. The scaffolding is built from the
description by real parsing -- nouns become names, verbs become methods, a
field description becomes Django field lines, a traceback is read with regular
expressions -- and the explanation says plainly that a human developer
completes it. The employee's own chat reply supplies the prose; the artefact
supplies the record, and the record is the same every time for the same input.

WHAT ``identify_bugs`` ACTUALLY DOES
-----------------------------------
It runs a fixed set of static checks that find real defects: bare excepts,
mutable default arguments, comparison against None with ``==``, resources
opened without ``with``, shadowed builtins, an assignment where a comparison
was meant, code after a return, a loop variable read after its loop, string
concatenation inside a loop, and a method missing ``self``. Each finding
carries the line number and the concrete failure it causes. When none of them
fires, it says nothing blocking was found, because that is a legitimate and
useful review outcome and inventing a finding to look thorough is not.
"""

import re

from django.db.models import Q
from django.utils import timezone

from ..models_eng import CodeArtifact, CodeReview, Project, WorkItem
from ..models_platform import ExternalIssue, OutboundMessage
from .base import Proposal, ToolResult, editable, executor, tool


# ===========================================================================
# Schema shorthand
# ===========================================================================

def _obj(required=(), **props):
    return {'type': 'object', 'properties': props, 'required': list(required)}


def _s(description):
    return {'type': 'string', 'description': description}


def _i(description):
    return {'type': 'integer', 'description': description}


def _a(description):
    return {'type': 'array', 'items': {'type': 'string'}, 'description': description}


def _enum(description, values):
    return {'type': 'string', 'enum': list(values), 'description': description}


# ===========================================================================
# Small conversions and guarded lookups
# ===========================================================================

def _as_list(value):
    if value in (None, ''):
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(v).strip() for v in value if str(v).strip()]
    return [part.strip() for part in str(value).replace(';', ',').split(',')
            if part.strip()]


def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _agent_of(ctx):
    return ctx.agent if getattr(ctx, 'agent', None) else None


def _call(provider_key, operation, **kwargs):
    """Reach one integration. Imported locally, as in the engineering module."""
    from .. import integrations
    return integrations.call(provider_key, operation, **kwargs)


def _find_item(work_item_id):
    return WorkItem.objects.filter(pk=_as_int(work_item_id)).first()


def _find_project(project_id):
    return Project.objects.filter(pk=_as_int(project_id)).first()


def _no_item(work_item_id):
    message = (f'There is no work item with id {work_item_id}. '
               'Call dev.list_my_work_items to see the items that exist.')
    return ToolResult(ok=False, error=message, text=message)


def _needs(what, advice):
    message = f'{what} {advice}'
    return ToolResult(ok=False, error=what, text=message)


def _first(data, *keys, default=''):
    for key in keys:
        value = (data or {}).get(key)
        if value not in (None, '', [], {}):
            return value
    return default


# ===========================================================================
# The artefact boundary
# ===========================================================================

def _save_artifact(ctx, *, kind, title, content, language='python', explanation='',
                   suggested_path='', work_item=None, metadata=None):
    """Write one CodeArtifact row. The only way anything generated is kept.

    Everything this employee produces goes through here, which is what makes
    the guarantee in the module docstring checkable: find every writer of
    generated content in this file and it is this one function, writing to a
    database row.
    """
    project = work_item.project if work_item is not None else None
    return CodeArtifact.objects.create(
        kind=kind, title=str(title)[:250], language=str(language or 'python')[:30],
        content=content, explanation=explanation,
        suggested_path=str(suggested_path or '')[:400],
        repository=(project.repository if project is not None else ''),
        work_item=work_item, project=project, status='draft',
        metadata=metadata or {}, created_by_agent=_agent_of(ctx))


def _artifact_result(artifact, headline, *, extra=None, data=None):
    """One shape for every generation tool's reply.

    The headline names the artefact and repeats the boundary, then the content
    follows so the model can quote it in its reply. The reminder is not
    decoration: without it the next chat turn tends to describe the code as
    though it were now in the project.
    """
    lines = [headline,
             f'Saved as artefact #{artifact.pk} ({artifact.get_kind_display()}, '
             f'{artifact.line_count} lines). Nothing has been written to the '
             f'repository -- a developer applies it if they agree with it.']
    if artifact.suggested_path:
        lines.append(f'Suggested location: {artifact.suggested_path} (advice only).')
    if extra:
        lines.extend(extra)
    if artifact.explanation:
        lines.append('')
        lines.append(artifact.explanation)
    lines.append('')
    lines.append(artifact.content)

    payload = {'artifact_id': artifact.pk, 'kind': artifact.kind,
               'language': artifact.language, 'title': artifact.title,
               'suggested_path': artifact.suggested_path,
               'line_count': artifact.line_count}
    payload.update(data or {})
    return ToolResult(ok=True, text='\n'.join(lines), data=payload,
                      subject_label='marketing.codeartifact', subject_id=artifact.pk)


# ===========================================================================
# Naming: turning a description into identifiers
# ===========================================================================

_ACTION_VERBS = (
    'create', 'add', 'register', 'read', 'get', 'fetch', 'list', 'update', 'edit',
    'delete', 'remove', 'archive', 'send', 'notify', 'validate', 'verify',
    'calculate', 'compute', 'import', 'export', 'sync', 'generate', 'parse',
    'save', 'store', 'search', 'filter', 'sort', 'upload', 'download', 'login',
    'logout', 'approve', 'reject', 'schedule', 'assign', 'cancel', 'refund',
    'publish', 'render', 'convert', 'merge', 'split', 'count', 'track',
)

_NAME_STOPWORDS = {
    'the', 'and', 'for', 'with', 'that', 'this', 'from', 'into', 'their', 'have',
    'should', 'must', 'need', 'needs', 'able', 'when', 'then', 'they', 'them',
    'our', 'your', 'also', 'each', 'every', 'some', 'any', 'all', 'new', 'make',
    'sure', 'want', 'would', 'could', 'shall', 'thing', 'things', 'work', 'using',
    'used', 'via', 'about', 'over', 'after', 'before', 'where', 'which', 'while',
    'there', 'here', 'more', 'most', 'less', 'like', 'just', 'only', 'both',
    'than', 'once', 'system', 'application', 'able', 'user', 'users', 'given',
    'takes', 'take', 'does', 'will',
}


def _words(text):
    cleaned = ''.join(c if (c.isalnum() or c.isspace()) else ' '
                      for c in str(text or ''))
    return [word for word in cleaned.split() if word]


def _nouns(text, limit=6):
    """Significant words that are not action verbs. Candidate names."""
    found = []
    for word in _words(text):
        lowered = word.lower()
        if len(lowered) < 3 or lowered in _NAME_STOPWORDS or lowered in _ACTION_VERBS:
            continue
        if lowered.isdigit():
            continue
        if lowered not in found:
            found.append(lowered)
    return found[:limit]


def _verbs(text, limit=6):
    """Action verbs actually present in the description, in order."""
    lowered = f' {str(text or "").lower()} '
    found = []
    for verb in _ACTION_VERBS:
        if f' {verb} ' in lowered or f' {verb}s ' in lowered or f' {verb}d ' in lowered:
            found.append(verb)
    return found[:limit] or ['handle']


def _pascal(text, fallback='Thing'):
    parts = [word for word in _words(text) if word.lower() not in _NAME_STOPWORDS]
    if not parts:
        parts = _words(text)
    if not parts:
        return fallback
    return ''.join(part[:1].upper() + part[1:].lower() for part in parts[:4]) or fallback


def _snake(text, fallback='thing'):
    parts = [word.lower() for word in _words(text)
             if word.lower() not in _NAME_STOPWORDS]
    if not parts:
        parts = [word.lower() for word in _words(text)]
    return '_'.join(parts[:4]) or fallback


def _singular(word):
    if word.endswith('ies') and len(word) > 4:
        return word[:-3] + 'y'
    if word.endswith('ses') or word.endswith('xes'):
        return word[:-2]
    if word.endswith('s') and not word.endswith('ss') and len(word) > 3:
        return word[:-1]
    return word


def _first_sentence(text, limit=110):
    body = str(text or '').strip().replace('\n', ' ')
    for terminator in '.;!?':
        if terminator in body:
            body = body.split(terminator)[0]
            break
    body = body.strip()
    if len(body) > limit:
        body = body[:limit].rsplit(' ', 1)[0] + '...'
    return body or 'the described behaviour'


# ===========================================================================
# The static analysis engine
# ===========================================================================
# These checks are the honest half of this module: they are the part that can
# genuinely find a defect rather than describe one. Every pattern below has
# been chosen because it names a concrete failure, not a style preference, and
# every finding carries the line and the consequence.

_BUILTIN_NAMES = (
    'list', 'dict', 'set', 'str', 'int', 'float', 'bool', 'bytes', 'tuple',
    'id', 'type', 'input', 'sum', 'max', 'min', 'filter', 'map', 'next',
    'object', 'all', 'any', 'len', 'format', 'hash', 'dir', 'vars', 'range',
    'open', 'print', 'file', 'compile', 'eval', 'exec', 'iter', 'zip', 'sorted',
)

_MUTABLE_DEFAULT = re.compile(r'def\s+\w+\s*\([^)]*=\s*(\[\s*\]|\{\s*\}|set\(\)|list\(\)|dict\(\))')
_BARE_EXCEPT = re.compile(r'^\s*except\s*:')
_BROAD_PASS = re.compile(r'^\s*except\s+[\w.,() ]*:\s*$')
_EQ_NONE = re.compile(r'(==|!=)\s*None|None\s*(==|!=)')
_IF_ASSIGN = re.compile(r'^\s*(?:el)?if\s+[\w.\[\]\'"() ]+\s=\s(?!=)')
_WHILE_ASSIGN = re.compile(r'^\s*while\s+[\w.\[\]\'"() ]+\s=\s(?!=)')
_DEF_LINE = re.compile(r'^(\s*)def\s+(\w+)\s*\(([^)]*)')
_CLASS_LINE = re.compile(r'^(\s*)class\s+(\w+)')
_FOR_LINE = re.compile(r'^(\s*)for\s+([\w, ]+?)\s+in\s+')
_OPEN_CALL = re.compile(r'(?<![\w.])open\s*\(')
_CONCAT_IN_LOOP = re.compile(r'^\s*(\w+)\s*\+=\s*(f?[\'"]|str\(|\w+\s*\+\s*[\'"])')
_RANGE_LEN = re.compile(r'for\s+\w+\s+in\s+range\s*\(\s*len\s*\(')
_LEN_ZERO = re.compile(r'len\s*\([^)]*\)\s*(==|!=|>)\s*0')
_EQ_BOOL = re.compile(r'(==|!=)\s*(True|False)\b')
_TYPE_EQ = re.compile(r'type\s*\([^)]*\)\s*(==|!=)\s*')
_HAS_KEY = re.compile(r'\.has_key\s*\(')
_MUTATE_WHILE_ITER = re.compile(r'^\s*for\s+(\w+)\s+in\s+(\w+)\s*:')


def _indent_of(line):
    return len(line) - len(line.lstrip())


def _finding(severity, line, note, suggestion, filename=''):
    return {'severity': severity, 'file': filename, 'line': line,
            'note': note, 'suggestion': suggestion}


def _static_findings(code, filename=''):
    """Run every check over one block of code. Returns a list of findings.

    Ordered by line so a reader can walk the file top to bottom, which is how
    a review is actually read.
    """
    lines = str(code or '').replace('\r', '').split('\n')
    findings = []

    class_stack = []          # (indent, name)
    loop_stack = []           # (indent, variable, start line)
    open_without_with = []
    returned_at = {}          # indent -> line number of a return
    for number, raw in enumerate(lines, start=1):
        line = raw.rstrip()
        if not line.strip():
            continue
        stripped = line.strip()
        indent = _indent_of(line)

        # -- context tracking ------------------------------------------------
        while class_stack and indent <= class_stack[-1][0] and not stripped.startswith('class '):
            if indent <= class_stack[-1][0]:
                class_stack.pop()
            else:
                break
        while loop_stack and indent <= loop_stack[-1][0]:
            finished = loop_stack.pop()
            # A loop variable read after the loop has ended is almost always a
            # mistake, and it is silent: the value is whatever the last
            # iteration left, or a NameError on an empty sequence.
            for later_number in range(number, min(number + 6, len(lines) + 1)):
                later = lines[later_number - 1]
                if not later.strip():
                    continue
                if _indent_of(later) > finished[0]:
                    continue
                if re.search(rf'(?<![\w.]){re.escape(finished[1])}(?![\w])', later) \
                        and not later.strip().startswith(('for ', 'def ', 'class ')):
                    findings.append(_finding(
                        'medium', later_number,
                        f'The loop variable "{finished[1]}" from the loop on line '
                        f'{finished[2]} is read after the loop has finished.',
                        'It holds whatever the last iteration left behind, and raises '
                        'NameError when the sequence was empty. Assign what you need '
                        'inside the loop, or use a named variable.', filename))
                    break

        cleared = [key for key in returned_at if key >= indent]
        for key in cleared:
            if key > indent:
                returned_at.pop(key, None)

        class_match = _CLASS_LINE.match(line)
        if class_match:
            class_stack.append((len(class_match.group(1)), class_match.group(2)))

        for_match = _FOR_LINE.match(line)
        if for_match:
            variables = [part.strip() for part in for_match.group(2).split(',')
                         if part.strip()]
            if variables:
                loop_stack.append((len(for_match.group(1)), variables[0], number))

        # -- unreachable code after a return --------------------------------
        if stripped.startswith(('return', 'raise', 'break', 'continue')):
            returned_at[indent] = number
        elif indent in returned_at and not stripped.startswith(
                ('else', 'elif', 'except', 'finally', 'def', 'class', '#', '@')):
            findings.append(_finding(
                'high', number,
                f'This line can never run: line {returned_at[indent]} at the same '
                f'indentation already returns or raises.',
                'Delete it, or move it above the return. Unreachable code usually '
                'means the return was added later and the intent was lost.', filename))
            returned_at.pop(indent, None)

        # -- bare except ----------------------------------------------------
        if _BARE_EXCEPT.match(line):
            findings.append(_finding(
                'high', number,
                'A bare "except:" catches KeyboardInterrupt and SystemExit as well '
                'as errors, so this code cannot be interrupted and hides every '
                'failure including typos in the block above.',
                'Catch the exceptions you can handle: "except (ValueError, KeyError) '
                'as exc:", or "except Exception as exc:" with a log at minimum.',
                filename))

        # -- swallowed exception --------------------------------------------
        if _BROAD_PASS.match(line):
            following = lines[number] if number < len(lines) else ''
            if following.strip() == 'pass':
                findings.append(_finding(
                    'high', number + 1,
                    'The exception is caught and discarded, so the failure is '
                    'invisible: the caller is told the operation succeeded.',
                    'Log the exception, re-raise it, or return a result that says it '
                    'failed. "pass" is only correct when the failure genuinely does '
                    'not matter, and then it deserves a comment saying why.',
                    filename))

        # -- mutable default argument ---------------------------------------
        if _MUTABLE_DEFAULT.search(line):
            findings.append(_finding(
                'high', number,
                'A mutable default argument is created once, when the function is '
                'defined, and then shared by every call. Appending to it leaks data '
                'between unrelated calls.',
                'Default to None and build the container inside: '
                '"def f(items=None): items = items or []".', filename))

        # -- comparison against None ----------------------------------------
        if _EQ_NONE.search(line):
            findings.append(_finding(
                'medium', number,
                'None is compared with == rather than "is". A class defining '
                '__eq__ can make this true for a value that is not None, and it is '
                'slower besides.',
                'Use "is None" or "is not None".', filename))

        # -- assignment where a comparison was meant ------------------------
        if _IF_ASSIGN.match(line) or _WHILE_ASSIGN.match(line):
            findings.append(_finding(
                'high', number,
                'This condition assigns rather than compares. In Python it is a '
                'SyntaxError, so the module will not import at all.',
                'Use "==" to compare, or ":=" if the assignment was deliberate.',
                filename))

        # -- resource opened without with -----------------------------------
        if _OPEN_CALL.search(line) and not stripped.startswith('with ') \
                and ' with ' not in stripped:
            if '.close()' not in str(code):
                open_without_with.append(number)
                findings.append(_finding(
                    'medium', number,
                    'A file is opened outside a "with" block and never closed. On '
                    'CPython it survives on reference counting; on any other '
                    'implementation, or when an exception is raised first, the '
                    'handle leaks and the write may not be flushed.',
                    'Use "with open(...) as handle:" so it closes on every path.',
                    filename))

        # -- shadowed builtin ------------------------------------------------
        for name in _BUILTIN_NAMES:
            if re.match(rf'^\s*{name}\s*=\s*(?!=)', line) or \
                    re.match(rf'^\s*for\s+{name}\s+in\s', line):
                findings.append(_finding(
                    'medium', number,
                    f'"{name}" is a builtin, and this rebinds it. Any later call to '
                    f'{name}() in this scope fails, usually a long way from here.',
                    f'Rename it -- "{name}_value", "items", "identifier" -- whichever '
                    f'says what it holds.', filename))
                break

        # -- string concatenation in a loop ---------------------------------
        concat = _CONCAT_IN_LOOP.match(line)
        if concat and loop_stack:
            findings.append(_finding(
                'medium', number,
                f'"{concat.group(1)}" is built by repeated concatenation inside a '
                f'loop. Each iteration copies the whole string, so the cost is '
                f'quadratic in the number of iterations.',
                'Append to a list and "".join(...) it once after the loop.',
                filename))

        # -- method missing self --------------------------------------------
        def_match = _DEF_LINE.match(line)
        if def_match and class_stack:
            def_indent = len(def_match.group(1))
            class_indent = class_stack[-1][0]
            arguments = [part.strip() for part in def_match.group(3).split(',')
                         if part.strip()]
            decorator = lines[number - 2].strip() if number >= 2 else ''
            first = arguments[0].split(':')[0].split('=')[0].strip() if arguments else ''
            if def_indent > class_indent and first not in ('self', 'cls') \
                    and not decorator.startswith(('@staticmethod', '@classmethod',
                                                  '@property.setter')):
                findings.append(_finding(
                    'high', number,
                    f'Method "{def_match.group(2)}" on class '
                    f'{class_stack[-1][1]} does not take self, so calling it on an '
                    f'instance raises TypeError about the argument count.',
                    'Add "self" as the first parameter, or mark it @staticmethod if '
                    'it genuinely needs no instance.', filename))

    findings.sort(key=lambda row: (row['line'], row['severity']))
    return findings


_SIMPLIFICATIONS = (
    (_RANGE_LEN, 'low',
     'Iterating "range(len(x))" to index into x reads worse and breaks on any '
     'iterable that is not a sequence.',
     'Use "for item in x" or "for index, item in enumerate(x)".'),
    (_LEN_ZERO, 'low',
     'A length is compared against zero to test emptiness.',
     'An empty container is already falsey: "if not items:" and "if items:".'),
    (_EQ_BOOL, 'low',
     'A value is compared against True or False.',
     'Use the value itself: "if flag:" or "if not flag:".'),
    (_TYPE_EQ, 'medium',
     'type(x) == Y fails for a subclass, which is usually not what was meant.',
     'Use isinstance(x, Y).'),
    (_HAS_KEY, 'high',
     '.has_key() was removed in Python 3, so this raises AttributeError.',
     'Use "key in mapping".'),
)


def _simplification_findings(code, filename=''):
    """Real simplifications only -- each one changes behaviour or removes a bug risk.

    Deliberately short. A review padded with formatting opinions gets skimmed,
    and the correctness findings get skimmed with it.
    """
    findings = []
    lines = str(code or '').replace('\r', '').split('\n')
    for number, raw in enumerate(lines, start=1):
        line = raw.rstrip()
        if not line.strip() or line.strip().startswith('#'):
            continue
        for pattern, severity, note, suggestion in _SIMPLIFICATIONS:
            if pattern.search(line):
                findings.append(_finding(severity, number, note, suggestion, filename))

    # Nesting and length are measured rather than judged.
    deepest, deepest_line = 0, 0
    for number, raw in enumerate(lines, start=1):
        if raw.strip() and not raw.strip().startswith('#'):
            depth = _indent_of(raw)
            if depth > deepest:
                deepest, deepest_line = depth, number
    if deepest >= 24:
        findings.append(_finding(
            'low', deepest_line,
            f'Indentation reaches {deepest} spaces here, which is at least six levels '
            f'of nesting. Code that deep is hard to test because most of its paths '
            f'need several conditions arranged at once.',
            'Return early on the failure cases so the body reads at one level.',
            filename))

    body_lines = [line for line in lines if line.strip()]
    if len(body_lines) > 120:
        findings.append(_finding(
            'low', len(lines),
            f'This block is {len(body_lines)} non-blank lines. A reviewer cannot hold '
            f'that much in mind at once, which is how defects survive review.',
            'Split it along the seams already visible in the code -- usually the '
            'comment headings.', filename))

    findings.sort(key=lambda row: row['line'])
    return findings


# ===========================================================================
# GROUP: Code Generation
# ===========================================================================
# Each tool builds a scaffold from the description and saves it as an
# artefact. The scaffold is structure, signatures, docstrings and marked error
# cases -- the parts that can be derived honestly -- and the explanation says
# which parts a human developer has to finish. It is not a finished
# implementation and it does not pretend to be one.

_COMMENT_STYLES = {
    'python': '#', 'javascript': '//', 'typescript': '//', 'java': '//',
    'csharp': '//', 'c': '//', 'cpp': '//', 'go': '//', 'rust': '//',
    'php': '//', 'ruby': '#', 'sql': '--', 'bash': '#', 'shell': '#',
    'html': '<!--', 'css': '/*',
}


def _python_scaffold(description, context=''):
    """A commented Python skeleton derived from the description.

    Nouns become the class and the parameters, verbs become the methods. The
    error cases are listed as explicit raises rather than left implicit,
    because the missing error path is the most common thing to be wrong with a
    first implementation.
    """
    nouns = _nouns(description)
    verbs = _verbs(description)
    subject = _singular(nouns[0]) if nouns else 'subject'
    class_name = _pascal(' '.join(nouns[:3]) or description, 'Handler')
    parameters = [_snake(noun, noun) for noun in nouns[1:4]] or ['payload']

    lines = [
        f'class {class_name}:',
        f'    """{_first_sentence(description)}',
        '',
        '    Scaffold only. The structure, the signatures and the error cases are',
        '    derived from the requirement; the bodies are for a developer to write.',
        '    """',
        '',
        f'    def __init__(self, {", ".join(parameters)}):',
    ]
    for parameter in parameters:
        lines.append(f'        self.{parameter} = {parameter}')
    lines.append('')

    for verb in verbs[:4]:
        method = f'{verb}_{subject}'
        lines.extend([
            f'    def {method}(self):',
            f'        """{verb.capitalize()} the {subject}.',
            '',
            '        Returns:',
            f'            The {subject} after the operation, or None when there was',
            '            nothing to act on.',
            '',
            '        Raises:',
            '            ValueError: the input did not describe a usable '
            f'{subject}.',
            '        """',
            f'        if not self.{parameters[0]}:',
            f'            # Error case: nothing to work with. Fail here rather than',
            f'            # returning a half-built {subject} the caller cannot check.',
            f'            raise ValueError("{method}() needs a {parameters[0]}")',
            '',
            '        # TODO: the actual work. Steps implied by the requirement:',
        ])
        for step in ('validate the input against the stated rules',
                     f'perform the {verb} itself',
                     'return a result the caller can act on'):
            lines.append(f'        #   - {step}')
        lines.extend([
            '        raise NotImplementedError(',
            f'            "{method}() is a generated scaffold and has no body yet")',
            '',
        ])

    if context:
        lines.extend(['', '# Context supplied with the request:'])
        lines.extend(f'#   {line}' for line in str(context).splitlines()[:8])

    return '\n'.join(lines)


def _generic_scaffold(description, language, context=''):
    """A commented skeleton for a language this module does not model."""
    mark = _COMMENT_STYLES.get(str(language).lower(), '//')
    nouns = _nouns(description)
    verbs = _verbs(description)
    name = _pascal(' '.join(nouns[:3]) or description, 'Handler')

    lines = [
        f'{mark} {name} -- {_first_sentence(description)}',
        f'{mark} Scaffold only. Structure and error cases are derived from the',
        f'{mark} requirement; the bodies are for a developer to write.',
        '',
        f'{mark} Responsibilities:',
    ]
    lines.extend(f'{mark}   - {verb} the {_singular(nouns[0]) if nouns else "subject"}'
                 for verb in verbs[:4])
    lines.extend([
        '',
        f'{mark} Inputs:  ' + ', '.join(nouns[1:4] or ['payload']),
        f'{mark} Outputs: the result of the operation, or an explicit failure',
        '',
        f'{mark} Error cases to handle explicitly:',
        f'{mark}   - the input is missing or empty',
        f'{mark}   - the input is present but does not validate',
        f'{mark}   - the operation partially succeeded (decide: roll back or report)',
        f'{mark}   - a dependency was unavailable',
        '',
        f'{mark} TODO: implement.',
    ])
    if context:
        lines.append('')
        lines.append(f'{mark} Context supplied with the request:')
        lines.extend(f'{mark}   {line}' for line in str(context).splitlines()[:8])
    return '\n'.join(lines)


@tool(name='dev.generate_code', title='Generate a code scaffold',
      description=('Build a code scaffold from a description -- structure, signatures, '
                   'docstrings and marked error cases -- and save it as an artefact '
                   'for a developer to complete. Nothing is written to the repository.'),
      group='Code Generation', agent_types=('developer',), icon='fa-code',
      capability='Generate a code scaffold',
      parameters=_obj(
          ('description',),
          description=_s('What the code has to do.'),
          language=_s("Language. Default 'python'."),
          context=_s('Existing code or conventions to match.'),
          suggested_path=_s('Where it might belong. Advice only.'),
          work_item_id=_i('Work item this belongs to.')))
def generate_code(ctx, description, language='python', context='',
                  suggested_path='', work_item_id=None):
    """Produce a scaffold, and say honestly what is missing from it.

    The explanation names the assumptions and the most likely thing to be
    wrong. That is the useful part: a review that starts from the author's own
    doubts finds problems faster than one that starts from nothing.
    """
    if not str(description or '').strip():
        return _needs('There is nothing to generate.',
                      'Say what the code has to do, in one or two sentences.')

    item = None
    if work_item_id:
        item = _find_item(work_item_id)
        if item is None:
            return _no_item(work_item_id)

    tongue = str(language or 'python').strip().lower() or 'python'
    body = (_python_scaffold(description, context) if tongue == 'python'
            else _generic_scaffold(description, tongue, context))

    nouns = _nouns(description)
    verbs = _verbs(description)
    path = str(suggested_path or '').strip() or (
        f'marketing/{_snake(" ".join(nouns[:2]) or "module", "module")}.py'
        if tongue == 'python' else '')

    explanation = '\n'.join([
        'WHAT THIS IS',
        'A scaffold, not an implementation. The structure, the signatures, the '
        'docstrings and the error cases are derived from the requirement; every '
        'body raises NotImplementedError or is marked TODO. A developer writes '
        'the logic.',
        '',
        'WHAT IT ASSUMED',
        f'- The subject of the work is "{nouns[0] if nouns else "unnamed"}", taken '
        f'from the first significant noun in the description.',
        f'- The operations needed are {", ".join(verbs)}, taken from the verbs in '
        f'the description.',
        '- The caller handles the exceptions raised here rather than ignoring them.',
        '- Existing project conventions were not read; only the context passed in '
        'was available.',
        '',
        'MOST LIKELY THING TO BE WRONG',
        'The decomposition. The names come from the words in the requirement, and '
        'requirements are written in the language of the problem rather than of '
        'the code. If the class boundary looks wrong, it probably is -- that is a '
        'judgement about this codebase that this scaffold cannot make.',
    ])

    artifact = _save_artifact(
        ctx, kind='code', title=f'Scaffold: {_first_sentence(description, 80)}',
        content=body, language=tongue, explanation=explanation,
        suggested_path=path, work_item=item,
        metadata={'nouns': nouns, 'verbs': verbs, 'context_supplied': bool(context)})

    return _artifact_result(
        artifact,
        f'Generated a {tongue} scaffold for "{_first_sentence(description, 60)}"'
        + (f', linked to {item.reference}' if item is not None else '') + '.',
        data={'work_item_id': item.pk if item is not None else None})


# -- Django models ---------------------------------------------------------
# The parser below turns "title char 200, body text, created date" into real
# field lines. It is a genuine parse rather than a template: the type comes
# from the words used, the length from any number present, and null/blank and
# unique from the qualifiers.

_FIELD_TYPES = (
    (('manytomany', 'many to many', 'm2m'), 'ManyToManyField'),
    (('foreignkey', 'foreign key', 'fk', 'reference to', 'belongs to', 'relation'),
     'ForeignKey'),
    (('email',), 'EmailField'),
    (('url', 'link', 'website'), 'URLField'),
    (('slug',), 'SlugField'),
    (('uuid', 'guid'), 'UUIDField'),
    (('datetime', 'timestamp', 'date and time'), 'DateTimeField'),
    (('duration',), 'DurationField'),
    (('date',), 'DateField'),
    (('time',), 'TimeField'),
    (('boolean', 'bool', 'flag', 'true/false', 'yes/no'), 'BooleanField'),
    (('decimal', 'money', 'price', 'amount', 'cost', 'currency'), 'DecimalField'),
    (('float', 'real'), 'FloatField'),
    (('integer', 'int', 'number', 'count', 'quantity', 'points', 'positive'),
     'IntegerField'),
    (('json', 'dictionary', 'dict', 'array'), 'JSONField'),
    (('image', 'photo', 'picture'), 'ImageField'),
    (('file', 'attachment', 'upload'), 'FileField'),
    (('text', 'description', 'body', 'notes', 'content', 'paragraph', 'long'),
     'TextField'),
    (('char', 'varchar', 'string', 'name', 'title', 'label', 'code'), 'CharField'),
)

_TEXTUAL_FIELDS = ('CharField', 'TextField', 'SlugField', 'EmailField', 'URLField')


def _parse_field(part):
    """One field description into (name, field type, keyword arguments, note)."""
    words = _words(part)
    if not words:
        return None
    name = _snake(words[0], words[0].lower())
    remainder = ' '.join(words[1:]).lower() or words[0].lower()
    whole = part.lower()

    field_type = 'CharField'
    for keywords, chosen in _FIELD_TYPES:
        if any(word in whole for word in keywords):
            field_type = chosen
            break

    numbers = [int(word) for word in words if word.isdigit()]
    arguments = []
    note = ''

    if field_type == 'CharField':
        arguments.append(f'max_length={numbers[0] if numbers else 200}')
    elif field_type == 'SlugField':
        arguments.append(f'max_length={numbers[0] if numbers else 60}')
    elif field_type == 'DecimalField':
        arguments.extend(['max_digits=10', 'decimal_places=2'])
        note = 'Two decimal places assumed; confirm against the currency.'
    elif field_type == 'BooleanField':
        arguments.append('default=False')
    elif field_type == 'JSONField':
        arguments.append('default=dict')
        note = 'Defaults to a dict; use default=list if it holds a sequence.'
    elif field_type in ('ForeignKey', 'ManyToManyField'):
        target = next((_pascal(word) for word in words[1:]
                       if word.lower() not in ('foreignkey', 'foreign', 'key', 'fk',
                                               'reference', 'to', 'belongs', 'relation',
                                               'manytomany', 'm2m', 'many')),
                      _pascal(name))
        if field_type == 'ForeignKey':
            arguments.extend([f"'{target}'", 'on_delete=models.CASCADE'])
            note = ('on_delete=CASCADE assumed. If the parent going away should not '
                    'erase this row, use SET_NULL with null=True.')
        else:
            arguments.extend([f"'{target}'", 'blank=True'])
    elif field_type == 'IntegerField' and 'positive' in whole:
        field_type = 'PositiveIntegerField'
        arguments.append('default=0')

    if 'created' in name or 'added' in name:
        if field_type in ('DateTimeField', 'DateField'):
            arguments.append('auto_now_add=True')
    elif any(word in name for word in ('updated', 'modified', 'changed')):
        if field_type in ('DateTimeField', 'DateField'):
            arguments.append('auto_now=True')

    optional = any(word in whole for word in
                   ('optional', 'nullable', 'may be empty', 'can be empty', 'blank',
                    'not required'))
    if optional:
        if field_type in _TEXTUAL_FIELDS:
            arguments.append('blank=True')
        else:
            arguments.extend(['null=True', 'blank=True'])
    if 'unique' in whole:
        arguments.append('unique=True')
    if 'index' in whole:
        arguments.append('db_index=True')

    return name, field_type, arguments, note


@tool(name='dev.generate_django_model', title='Generate a Django model',
      description=('Turn a field description like "title char 200, body text, created '
                   'date" into real Django field lines with a Meta and __str__, saved '
                   'as an artefact.'),
      group='Code Generation', agent_types=('developer',), icon='fa-database',
      capability='Generate a Django model',
      parameters=_obj(
          ('model_name',),
          model_name=_s('Name of the model.'),
          fields_description=_s('Fields, e.g. "title char 200, body text, created date".'),
          app=_s("Django app the model belongs to. Default 'marketing'.")))
def generate_django_model(ctx, model_name, fields_description='', app='marketing'):
    """Parse a field description into a model. Real parsing, not a template."""
    if not str(model_name or '').strip():
        return _needs('The model needs a name.',
                      'Pass model_name, e.g. "Invoice".')

    name = _pascal(model_name, 'Record')
    parts = [part for part in re.split(r'[,\n;]+', str(fields_description or ''))
             if part.strip()]

    parsed, notes = [], []
    for part in parts:
        field = _parse_field(part)
        if field is None:
            continue
        parsed.append(field)
        if field[3]:
            notes.append(f'{field[0]}: {field[3]}')

    if not parsed:
        parsed = [('name', 'CharField', ['max_length=200'], ''),
                  ('created_at', 'DateTimeField', ['auto_now_add=True'], '')]
        notes.append('No fields were described, so a name and a created timestamp '
                     'were assumed. Say what the model holds and this will be real.')

    first_text = next((field[0] for field in parsed
                       if field[1] in ('CharField', 'SlugField', 'EmailField')),
                      parsed[0][0])
    order_field = next((field[0] for field in parsed
                        if 'created' in field[0] or 'date' in field[0]), first_text)

    lines = [
        'from django.db import models',
        '',
        '',
        f'class {name}(models.Model):',
        f'    """{name}.',
        '',
        '    Generated from a field description. Check the types, the lengths and',
        '    every on_delete before applying it.',
        '    """',
        '',
    ]
    for field_name, field_type, arguments, _note in parsed:
        rendered = ', '.join(arguments)
        lines.append(f'    {field_name} = models.{field_type}({rendered})')

    lines.extend([
        '',
        '    class Meta:',
        f"        ordering = ['{'-' if 'created' in order_field else ''}{order_field}']",
        f"        verbose_name = '{name}'",
        f"        verbose_name_plural = '{name}s'",
        '',
        '    def __str__(self):',
        f'        return f"{{self.{first_text}}}"',
    ])

    explanation_lines = [
        'WHAT WAS PARSED',
        f'{len(parsed)} field(s) from the description, with the type taken from the '
        f'words used, the length from any number present, and null/blank/unique from '
        f'the qualifiers.',
        '',
        'WHAT IT ASSUMED',
        f"- Ordering by {order_field}, and __str__ returning {first_text}.",
        '- CharField lengths default to 200 where no number was given.',
        '- Nothing about existing tables: this is a new model, not a migration of '
        'one that exists.',
    ]
    if notes:
        explanation_lines.extend(['', 'DECISIONS WORTH CHECKING'])
        explanation_lines.extend(f'- {note}' for note in notes)
    explanation_lines.extend([
        '',
        'MOST LIKELY THING TO BE WRONG',
        'The relations and the on_delete rules. A ForeignKey with CASCADE where the '
        'business rule wanted SET_NULL deletes real data, and no test will notice '
        'until it has.',
        '',
        'This model needs a migration. Nothing here creates one -- run '
        'makemigrations yourself after reviewing the fields.',
    ])

    artifact = _save_artifact(
        ctx, kind='model', title=f'Django model: {name}',
        content='\n'.join(lines), language='python',
        explanation='\n'.join(explanation_lines),
        suggested_path=f'{str(app or "marketing").strip()}/models.py',
        metadata={'model': name, 'fields': [field[0] for field in parsed]})

    return _artifact_result(
        artifact,
        f'Generated the Django model {name} with {len(parsed)} field(s): '
        f'{", ".join(field[0] for field in parsed)}.',
        data={'model': name, 'field_count': len(parsed)})


# -- API endpoints ---------------------------------------------------------

_METHOD_BODIES = {
    'GET': ('Return the resource, or a 404 when it does not exist.',
            'read'),
    'POST': ('Create the resource from the request body, or 400 when it does not '
             'validate.', 'create'),
    'PUT': ('Replace the resource entirely, or 404 when it does not exist.',
            'replace'),
    'PATCH': ('Update the fields present in the body, leaving the rest alone.',
              'update'),
    'DELETE': ('Remove the resource and return 204, or 404 when it was already gone.',
               'delete'),
}


@tool(name='dev.generate_api_endpoint', title='Generate an API endpoint',
      description=('Scaffold an endpoint for a resource: the view or views for each '
                   'method, the authentication decorator, the error responses and the '
                   'URL entry. Saved as an artefact.'),
      group='Code Generation', agent_types=('developer',), icon='fa-plug',
      capability='Generate an API endpoint',
      parameters=_obj(
          ('resource',),
          resource=_s('The resource the endpoint exposes, e.g. "invoice".'),
          methods=_s("HTTP methods, e.g. 'GET,POST'."),
          framework=_enum('Framework.', ('django', 'drf', 'flask', 'fastapi')),
          auth=_s("Authentication requirement, e.g. 'login_required' or 'none'.")))
def generate_api_endpoint(ctx, resource, methods='GET,POST', framework='django',
                          auth='login_required'):
    """Scaffold an endpoint, with a status code for every failure and not only success."""
    if not str(resource or '').strip():
        return _needs('There is no resource to expose.',
                      'Pass resource, e.g. "invoice".')

    subject = _singular(_snake(resource, 'resource'))
    plural = f'{subject}s'
    verbs = [method.strip().upper() for method in _as_list(methods)] or ['GET']
    verbs = [verb for verb in verbs if verb in _METHOD_BODIES] or ['GET']
    shape = str(framework or 'django').strip().lower()
    guard = str(auth or '').strip().lower()
    protected = guard not in ('', 'none', 'public', 'anonymous')

    lines = []
    if shape == 'django':
        lines.extend([
            'import json',
            '',
            'from django.contrib.auth.decorators import login_required',
            'from django.http import JsonResponse',
            'from django.views.decorators.http import require_http_methods',
            '',
            f'# URL entry for {str(framework)}/urls.py:',
            f"#     path('api/{plural}/', {subject}_collection, name='{subject}_collection'),",
            f"#     path('api/{plural}/<int:pk>/', {subject}_detail, name='{subject}_detail'),",
            '',
        ])
        for scope, argument in (('collection', ''), ('detail', ', pk')):
            applicable = [verb for verb in verbs
                          if (scope == 'collection' and verb in ('GET', 'POST'))
                          or (scope == 'detail' and verb in ('GET', 'PUT', 'PATCH',
                                                             'DELETE'))]
            if not applicable:
                continue
            lines.append('')
            if protected:
                lines.append('@login_required')
            lines.append(f'@require_http_methods({applicable!r})')
            lines.append(f'def {subject}_{scope}(request{argument}):')
            lines.append(f'    """{scope.capitalize()} endpoint for {subject}.')
            lines.append('')
            lines.append('    Responses:')
            for verb in applicable:
                purpose, _kind = _METHOD_BODIES[verb]
                lines.append(f'        {verb}: {purpose}')
            lines.append('        401 when unauthenticated, 403 when not permitted.')
            lines.append('        400 when the body does not parse or does not validate.')
            lines.append('    """')
            for verb in applicable:
                lines.extend([
                    f"    if request.method == '{verb}':",
                    f'        # TODO: {_METHOD_BODIES[verb][0]}',
                ])
                if verb in ('POST', 'PUT', 'PATCH'):
                    lines.extend([
                        '        try:',
                        '            payload = json.loads(request.body or "{}")',
                        '        except json.JSONDecodeError:',
                        "            return JsonResponse({'error': 'body is not JSON'}, "
                        'status=400)',
                        '        # TODO: validate payload before touching the database.',
                    ])
                lines.append('        raise NotImplementedError(')
                lines.append(f'            "{subject}_{scope} {verb} has no body yet")')
            lines.append("    return JsonResponse({'error': 'method not allowed'}, "
                         'status=405)')
    elif shape == 'drf':
        lines.extend([
            'from rest_framework import status',
            'from rest_framework.permissions import IsAuthenticated, AllowAny',
            'from rest_framework.response import Response',
            'from rest_framework.views import APIView',
            '',
            '',
            f'class {_pascal(subject)}View(APIView):',
            f'    """Endpoint for {subject}."""',
            '',
            f'    permission_classes = [{"IsAuthenticated" if protected else "AllowAny"}]',
            '',
        ])
        for verb in verbs:
            purpose, _kind = _METHOD_BODIES[verb]
            lines.extend([
                f'    def {verb.lower()}(self, request, pk=None):',
                f'        """{purpose}"""',
                '        # TODO: implement. Validate first, then act, then return.',
                '        raise NotImplementedError(',
                f'            "{verb.lower()}() has no body yet")',
                '',
            ])
    else:
        decorator = '@app.route' if shape == 'flask' else '@app.get'
        lines.extend([
            f'# {shape} endpoint for {subject}',
            f'# Methods: {", ".join(verbs)}',
            f'# Authentication: {"required" if protected else "public"}',
            '',
        ])
        for verb in verbs:
            purpose, _kind = _METHOD_BODIES[verb]
            if shape == 'flask':
                lines.append(f"{decorator}('/api/{plural}', methods=['{verb}'])")
            else:
                lines.append(f"@app.{verb.lower()}('/api/{plural}')")
            lines.extend([
                f'def {verb.lower()}_{subject}():',
                f'    """{purpose}"""',
                '    raise NotImplementedError',
                '',
            ])

    explanation = '\n'.join([
        'WHAT THIS IS',
        f'A scaffold for a {shape} endpoint exposing {subject} over '
        f'{", ".join(verbs)}. Every method has its response codes written down, '
        f'including the failures, because the failure responses are the ones that '
        f'get forgotten and then invented by the client.',
        '',
        'WHAT IT ASSUMED',
        f'- Authentication is {"required" if protected else "not required"}, from '
        f'the auth argument.',
        f'- The resource is addressed by an integer primary key.',
        '- No serialiser or model exists yet; the bodies are marked TODO.',
        '',
        'MOST LIKELY THING TO BE WRONG',
        'Authorisation as distinct from authentication. Being logged in is not the '
        'same as being allowed to touch this particular row, and the check for the '
        'second one is not in this scaffold because it depends on the ownership '
        'rules of your data.',
    ])

    artifact = _save_artifact(
        ctx, kind='api', title=f'{shape} endpoint: {subject}',
        content='\n'.join(lines), language='python',
        explanation=explanation, suggested_path='marketing/views.py',
        metadata={'resource': subject, 'methods': verbs, 'framework': shape,
                  'authenticated': protected})

    return _artifact_result(
        artifact,
        f'Generated a {shape} endpoint scaffold for "{subject}" covering '
        f'{", ".join(verbs)}'
        + (' with authentication required.' if protected else ' with no auth guard.'),
        data={'methods': verbs, 'framework': shape})


# -- Queries ---------------------------------------------------------------

_AGGREGATES = (
    (('how many', 'count', 'number of', 'total number'), 'count'),
    (('total', 'sum of', 'sum'), 'sum'),
    (('average', 'mean', 'avg'), 'avg'),
    (('highest', 'maximum', 'max', 'largest'), 'max'),
    (('lowest', 'minimum', 'min', 'smallest'), 'min'),
)


@tool(name='dev.generate_database_query', title='Generate a database query',
      description=('Turn an intent like "count invoices per customer this month" into '
                   'a Django ORM expression and the equivalent SQL, saved as an '
                   'artefact.'),
      group='Code Generation', agent_types=('developer',), icon='fa-table',
      capability='Generate a database query',
      parameters=_obj(
          ('intent',),
          intent=_s('What the query has to answer, in plain words.'),
          tables=_s('Tables or models involved, comma separated.'),
          dialect=_enum('SQL dialect for the second form.',
                        ('sql', 'postgres', 'mysql', 'sqlite'))))
def generate_database_query(ctx, intent, tables='', dialect='sql'):
    """Both forms where possible: the ORM expression and the SQL behind it.

    Giving both is deliberate. The ORM version is what goes in the code; the
    SQL version is what a reviewer can read to see whether the question was
    understood, and what can be run against a copy of the data to check.
    """
    if not str(intent or '').strip():
        return _needs('There is no query intent.',
                      'Say what question the query has to answer.')

    names = _as_list(tables) or _nouns(intent, limit=2)
    primary = _singular(_snake(names[0], 'record')) if names else 'record'
    model = _pascal(primary, 'Record')
    lowered = str(intent).lower()

    aggregate = ''
    for keywords, kind in _AGGREGATES:
        if any(word in lowered for word in keywords):
            aggregate = kind
            break

    grouping = ''
    for marker in (' per ', ' by ', ' grouped by ', ' for each '):
        if marker in lowered:
            after = lowered.split(marker, 1)[1]
            candidates = _nouns(after, limit=1)
            grouping = _snake(candidates[0], '') if candidates else ''
            break

    limit = next((int(word) for word in _words(intent) if word.isdigit()), 0)
    recent = any(word in lowered for word in ('recent', 'latest', 'newest', 'last'))
    period = next((word for word in ('today', 'this week', 'this month', 'this year')
                   if word in lowered), '')

    filters, sql_where = [], []
    if period:
        filters.append("created_at__gte=start_of_period  # TODO: compute the boundary")
        sql_where.append("created_at >= :period_start")
    if 'active' in lowered:
        filters.append("is_active=True")
        sql_where.append("is_active = TRUE")
    if 'not ' in lowered or 'without' in lowered:
        filters.append("# TODO: the negative condition in the intent -- exclude(...)")

    orm = [f'from django.db.models import Avg, Count, Max, Min, Sum',
           '',
           f'# Intent: {_first_sentence(intent, 100)}',
           f'queryset = {model}.objects.all()']
    if filters:
        orm.append(f'queryset = queryset.filter({", ".join(filters)})')
    if grouping and aggregate:
        function = {'count': 'Count("id")', 'sum': 'Sum("amount")',
                    'avg': 'Avg("amount")', 'max': 'Max("amount")',
                    'min': 'Min("amount")'}[aggregate]
        orm.append(f'result = (queryset.values("{grouping}")')
        orm.append(f'          .annotate(value={function})')
        orm.append('          .order_by("-value"))')
    elif aggregate == 'count':
        orm.append('result = queryset.count()')
    elif aggregate:
        function = {'sum': 'Sum', 'avg': 'Avg', 'max': 'Max', 'min': 'Min'}[aggregate]
        orm.append(f'result = queryset.aggregate(value={function}("amount"))["value"]')
    else:
        if recent:
            orm.append('queryset = queryset.order_by("-created_at")')
        if limit:
            orm.append(f'result = list(queryset[:{limit}])')
        else:
            orm.append('result = list(queryset)')

    select = {'count': 'COUNT(*)', 'sum': 'SUM(amount)', 'avg': 'AVG(amount)',
              'max': 'MAX(amount)', 'min': 'MIN(amount)'}.get(aggregate, '*')
    table = f'marketing_{primary}'
    sql = [f'-- Intent: {_first_sentence(intent, 100)}',
           f'-- Dialect: {dialect}',
           f'SELECT {(grouping + ", ") if grouping else ""}{select}',
           f'FROM {table}']
    if sql_where:
        sql.append('WHERE ' + ' AND '.join(sql_where))
    if grouping:
        sql.append(f'GROUP BY {grouping}')
        sql.append(f'ORDER BY {select} DESC')
    elif recent:
        sql.append('ORDER BY created_at DESC')
    if limit:
        sql.append(f'LIMIT {limit}')
    sql[-1] = sql[-1] + ';'

    content = '\n'.join(['# ORM form', *orm, '', '# SQL form', *sql])

    explanation = '\n'.join([
        'WHAT WAS INFERRED',
        f'- Model: {model} (from "{primary}"). Table guessed as {table}, which '
        f'assumes the default Django naming.',
        f'- Aggregate: {aggregate or "none -- a plain listing"}.',
        f'- Grouping: {grouping or "none"}.',
        f'- Ordering: {"newest first" if recent else "database default"}.',
        f'- Limit: {limit or "none"}.',
        '',
        'WHAT TO CHECK',
        '- The column names. "amount" and "created_at" are placeholders taken from '
        'convention, not from your schema.',
        '- Any date boundary. The period was recognised but not computed; a query '
        'that says "this month" must decide whether that is calendar or rolling.',
        '',
        'MOST LIKELY THING TO BE WRONG',
        'A join. If the question spans two tables, the ORM form above walks one, '
        'and the answer will be quietly incomplete rather than an error.',
    ])

    artifact = _save_artifact(
        ctx, kind='query', title=f'Query: {_first_sentence(intent, 70)}',
        content=content, language='python', explanation=explanation,
        metadata={'model': model, 'aggregate': aggregate, 'grouping': grouping,
                  'limit': limit, 'dialect': dialect})

    return _artifact_result(
        artifact,
        f'Generated both an ORM expression and {dialect.upper()} for '
        f'"{_first_sentence(intent, 60)}"'
        + (f' -- {aggregate} aggregate' if aggregate else '')
        + (f' grouped by {grouping}' if grouping else '') + '.',
        data={'aggregate': aggregate, 'grouping': grouping})


@tool(name='dev.plan_implementation', title='Plan an implementation',
      description=('Ordered steps for building a requirement, each with the file it '
                   'likely touches and the check that proves it works. Saved as an '
                   'artefact.'),
      group='Code Generation', agent_types=('developer',), icon='fa-list-ol',
      capability='Plan an implementation',
      parameters=_obj(
          ('requirement',),
          requirement=_s('What has to be built.'),
          work_item_id=_i('Work item this plan is for.'),
          max_steps=_i('Most steps to produce. Default 10.')))
def plan_implementation(ctx, requirement, work_item_id=None, max_steps=10):
    """An ordered plan, each step with its file and its proof.

    The proof is the part usually missing. A step without a check is a step
    that gets marked done because the code was written, which is not the same
    as it working.
    """
    if not str(requirement or '').strip():
        return _needs('There is nothing to plan.', 'Say what has to be built.')

    item = None
    if work_item_id:
        item = _find_item(work_item_id)
        if item is None:
            return _no_item(work_item_id)

    lowered = str(requirement).lower()
    subject = _singular(_nouns(requirement)[0]) if _nouns(requirement) else 'feature'
    steps = []

    def add(step, path, check):
        steps.append({'step': step, 'file': path, 'check': check})

    if any(word in lowered for word in ('model', 'field', 'table', 'store', 'record',
                                        'database', 'save', 'persist')):
        add(f'Define or extend the model that holds the {subject}, with the fields '
            f'and constraints the requirement names.',
            'marketing/models.py',
            'Open a shell and create one row with the minimum fields; the constraint '
            'you expect to fire should fire.')
        add('Generate and read the migration before applying it.',
            'marketing/migrations/',
            'The migration file names only the changes you intended, and it reverses.')
    if any(word in lowered for word in ('api', 'endpoint', 'view', 'page', 'form',
                                        'submit', 'request', 'upload')):
        add(f'Add the view that handles the {subject}, validating the input before '
            f'touching the database.',
            'marketing/views.py',
            'Call it with valid input, with missing input, and with hostile input; '
            'each returns the documented status code.')
        add('Wire the URL.', 'marketing/urls.py',
            'The route resolves by name, and reversing it produces the path you '
            'expected.')
    if any(word in lowered for word in ('permission', 'role', 'admin', 'access',
                                        'auth', 'login', 'owner')):
        add('Enforce the access rule server side, not by hiding the control.',
            'marketing/views.py or marketing/roles.py',
            'A user without the permission is refused with 403 when calling the '
            'endpoint directly.')
    if any(word in lowered for word in ('screen', 'ui', 'template', 'display', 'show',
                                        'render', 'button', 'list')):
        add(f'Render the {subject} in the template, including the empty state.',
            'templates/',
            'The page renders with zero rows, one row and many rows without '
            'changing shape.')
    if any(word in lowered for word in ('email', 'notify', 'slack', 'message',
                                        'send', 'alert')):
        add('Route the outbound message through the approval queue rather than '
            'sending it directly.',
            'marketing/tools/',
            'The action appears as pending and nothing leaves until it is approved.')
    if any(word in lowered for word in ('import', 'export', 'csv', 'sync', 'bulk')):
        add('Handle the malformed row: fail that row alone and report its number.',
            'marketing/',
            'A file with one bad row imports the rest and names the bad line.')

    add(f'Write the tests for the {subject}: the ordinary path, the boundaries and '
        f'the failures.',
        'marketing/tests.py',
        'The tests fail when you revert the implementation. A test that passes '
        'either way is testing nothing.')
    add('Update whatever documents this behaviour, including the operational note '
        'for when it fails.',
        'README.md or the project documents',
        'Somebody who was not in this conversation can follow it.')

    limit = max(1, min(_as_int(max_steps) or 10, 15))
    steps = steps[:limit]

    lines = [f'Implementation plan: {_first_sentence(requirement, 100)}']
    if item is not None:
        lines.append(f'For {item.reference} on {item.project.key}.')
    lines.append('')
    for position, entry in enumerate(steps, start=1):
        lines.extend([f'{position}. {entry["step"]}',
                      f'   File:  {entry["file"]}',
                      f'   Proof: {entry["check"]}',
                      ''])

    explanation = '\n'.join([
        'HOW THIS ORDER WAS CHOSEN',
        'Data before behaviour before interface before tests before documentation. '
        'The steps present are the ones the requirement actually implies -- the '
        'words in it decided which layers appear.',
        '',
        'WHAT IT ASSUMED',
        '- The file paths follow this project\'s existing layout.',
        '- Each step is small enough to finish and verify before the next begins.',
        '',
        'MOST LIKELY THING TO BE WRONG',
        'A missing step for something the requirement did not mention but the '
        'change needs anyway -- a permission, a migration of existing rows, or an '
        'index. Read the plan against the code, not only against the requirement.',
    ])

    artifact = _save_artifact(
        ctx, kind='plan', title=f'Plan: {_first_sentence(requirement, 80)}',
        content='\n'.join(lines), language='markdown', explanation=explanation,
        work_item=item, metadata={'steps': len(steps)})

    return _artifact_result(
        artifact,
        f'Planned the work in {len(steps)} step(s), each with the file it touches '
        f'and the check that proves it'
        + (f'. Linked to {item.reference}.' if item is not None else '.'),
        data={'steps': steps, 'step_count': len(steps)})


@tool(name='dev.list_artifacts', title='List artefacts',
      description=('List the artefacts this employee has produced, filtered by kind or '
                   'work item.'),
      group='Code Generation', agent_types=('developer',), icon='fa-boxes-stacked',
      reads_only=True, capability='List code artefacts',
      parameters=_obj(
          (),
          kind=_enum('Restrict to one kind.',
                     ('code', 'model', 'api', 'query', 'test', 'doc', 'readme',
                      'review', 'fix', 'explanation', 'plan', 'commit', 'pr', 'debug')),
          work_item_id=_i('Restrict to one work item.'),
          limit=_i('Most artefacts to return. Default 20.')))
def list_artifacts(ctx, kind='', work_item_id=None, limit=20):
    """What has been generated, so an earlier artefact can be found by id."""
    query = CodeArtifact.objects.all()
    described = []
    if kind:
        query = query.filter(kind=kind)
        described.append(f'kind {kind}')
    if work_item_id:
        item = _find_item(work_item_id)
        if item is None:
            return _no_item(work_item_id)
        query = query.filter(work_item=item)
        described.append(f'work item {item.reference}')

    rows = list(query[:max(1, min(_as_int(limit) or 20, 100))])
    where = ' with ' + ', '.join(described) if described else ''
    if not rows:
        return ToolResult(ok=True, text=f'No artefacts{where}.',
                          data={'artifacts': [], 'count': 0})

    lines = [f'#{artifact.pk} [{artifact.kind}/{artifact.status}] {artifact.title} '
             f'-- {artifact.line_count} lines, {artifact.created_at:%d %b %H:%M}'
             for artifact in rows]

    return ToolResult(
        ok=True, text=f'{len(rows)} artefact(s){where}:\n' + '\n'.join(lines),
        data={'artifacts': [{'id': a.pk, 'kind': a.kind, 'title': a.title,
                             'status': a.status, 'language': a.language,
                             'lines': a.line_count,
                             'work_item_id': a.work_item_id} for a in rows],
              'count': len(rows)})


@tool(name='dev.get_artifact', title='Get an artefact',
      description='One artefact in full: its content, its explanation and its status.',
      group='Code Generation', agent_types=('developer',), icon='fa-file-code',
      reads_only=True, capability='Read a code artefact',
      parameters=_obj(('artifact_id',), artifact_id=_i('Artefact id.')))
def get_artifact(ctx, artifact_id):
    """Read back something generated earlier, content and caveats together."""
    artifact = CodeArtifact.objects.filter(pk=_as_int(artifact_id)).first()
    if artifact is None:
        message = (f'There is no artefact with id {artifact_id}. '
                   f'Call dev.list_artifacts to see what exists.')
        return ToolResult(ok=False, error=message, text=message)

    lines = [f'Artefact #{artifact.pk}: {artifact.title}',
             f'{artifact.get_kind_display()}, {artifact.language}, '
             f'{artifact.line_count} lines, status {artifact.get_status_display()}.']
    if artifact.suggested_path:
        lines.append(f'Suggested location: {artifact.suggested_path} (advice only -- '
                     f'nothing was written there).')
    if artifact.work_item_id:
        lines.append(f'Linked to {artifact.work_item.reference}.')
    if artifact.explanation:
        lines.extend(['', artifact.explanation])
    lines.extend(['', artifact.content])

    return ToolResult(
        ok=True, text='\n'.join(lines),
        data={'artifact_id': artifact.pk, 'kind': artifact.kind,
              'title': artifact.title, 'content': artifact.content,
              'explanation': artifact.explanation, 'status': artifact.status},
        subject_label='marketing.codeartifact', subject_id=artifact.pk)


# ===========================================================================
# GROUP: Debugging
# ===========================================================================
# A traceback is evidence. It is read here with regular expressions rather
# than paraphrased, and the reading is kept separate from the theorising: what
# the trace PROVES is stated first, and only then what is INFERRED from it.
# Collapsing those two is how a confident wrong diagnosis gets acted on.

_FRAME_RE = re.compile(r'File "(?P<file>[^"]+)", line (?P<line>\d+), in (?P<func>.*)')
_EXCEPTION_RE = re.compile(
    r'^(?P<type>[A-Za-z_][\w.]*?(?:Error|Exception|Exit|Interrupt|Warning|'
    r'NotFound|DoesNotExist|MultipleObjectsReturned|StopIteration|Timeout))'
    r'\s*(?::\s*(?P<message>.*))?$')

_FOREIGN_MARKERS = ('site-packages', 'dist-packages', 'lib/python', 'lib\\python',
                    '<frozen', '/usr/lib/python', 'python3.', 'Python3', 'Lib\\',
                    'lib2to3', '<string>')

# (exception types, message fragment, probable cause, smallest experiment)
_DIAGNOSES = (
    (('AttributeError',), 'nonetype',
     'Something upstream returned None where an object was expected, and the '
     'attribute access is only where it became visible.',
     'Print the value on the line above the failure. If it is None, walk back to '
     'the call that produced it -- most often a .first() or .get() that matched '
     'nothing, or a function that falls off the end without returning.'),
    (('AttributeError',), '',
     'The object is not the type the code assumes: either the attribute name is '
     'misspelled or the wrong object arrived.',
     'Add "print(type(obj), sorted(dir(obj))[:20])" immediately before the failing '
     'line. The type answers it in one run.'),
    (('KeyError',), '',
     'A key the code assumed present is absent -- usually because the data came '
     'from outside (a request body, a JSON file, an API response) and the shape '
     'was assumed rather than checked.',
     'Print the mapping\'s keys just before the access. If the key is genuinely '
     'optional, use .get() with a default and decide what the default means.'),
    (('IndexError',), '',
     'A sequence was shorter than assumed, most often empty.',
     'Print len(sequence) before the index. If empty is legitimate, handle it '
     'explicitly rather than indexing defensively.'),
    (('TypeError',), 'unexpected keyword argument',
     'A call does not match the signature it is calling: the function changed, or '
     'a different function of the same name is imported.',
     'Print the function\'s module and signature at the call site: '
     '"import inspect; print(target.__module__, inspect.signature(target))".'),
    (('TypeError',), 'missing 1 required positional argument',
     'Either a method is being called without its instance, or the call is short '
     'an argument. A method defined without self produces exactly this.',
     'Look at the definition of the callee. If it is a method on a class, check '
     'that its first parameter is self.'),
    (('TypeError',), 'not subscriptable',
     'Something that is None or not a container is being indexed. The value is '
     'wrong, not the indexing.',
     'Print the value and its type on the line before.'),
    (('TypeError',), 'not iterable',
     'A single value is being iterated, or None is, where a sequence was expected.',
     'Print the value before the loop. A function returning either one item or a '
     'list is the usual source.'),
    (('TypeError',), '',
     'A value of the wrong type reached this call -- often a string where a number '
     'was expected, from untrusted input that was never converted.',
     'Print the value and its type at the boundary where it entered, not where it '
     'failed.'),
    (('ValueError',), 'invalid literal for int',
     'A string that is not a number is being converted -- an empty field, a '
     'thousands separator, or a header row treated as data.',
     'Print the exact string with repr() so whitespace and empties are visible.'),
    (('ValueError',), '',
     'The value has the right type but not an acceptable value.',
     'Print the value with repr() at the point it entered the system.'),
    (('ZeroDivisionError',), '',
     'A denominator that is normally non-zero was zero -- almost always a count or '
     'a length over an empty set.',
     'Print the denominator. Then decide what the answer should be for an empty '
     'set; usually zero or "not applicable", not a crash.'),
    (('ModuleNotFoundError', 'ImportError'), 'circular',
     'A circular import: two modules each need the other at import time.',
     'Move the import inside the function that uses it. If that fixes it, the '
     'cycle is real and the module boundary needs rethinking.'),
    (('ModuleNotFoundError',), '',
     'The module is not installed in the interpreter actually running, or its name '
     'is misspelled. A different virtual environment is the common cause.',
     'Print sys.executable and sys.path at the top of the failing module, and '
     'compare with where the package is installed.'),
    (('ImportError',), '',
     'The module exists but the name being imported from it does not -- it was '
     'renamed, moved, or never existed.',
     'Import the module alone and print dir() on it.'),
    (('RecursionError',), '',
     'A base case is missing or never reached. A property that reads itself, or a '
     '__str__ that formats the object, does this.',
     'Print the recursion argument on entry. If it never approaches the base case, '
     'the recursion step is wrong.'),
    (('IntegrityError',), 'not null',
     'A required column received no value. Either the field should have a default, '
     'or the code path that omits it is the bug.',
     'Print the object\'s __dict__ immediately before saving.'),
    (('IntegrityError',), 'unique',
     'A duplicate is being inserted. Either the operation ran twice, or the '
     'uniqueness rule is stricter than the data.',
     'Query for the existing row with the same key before the insert; if it '
     'exists, the question is which of the two writes is correct.'),
    (('IntegrityError',), 'foreign key',
     'A row is referencing a parent that does not exist or has been deleted.',
     'Print the foreign key value and query for it directly.'),
    (('OperationalError',), 'no such column',
     'The database does not match the models: a migration has not been applied.',
     'Run showmigrations and compare with the model. This is a deployment state '
     'problem, not a code problem.'),
    (('OperationalError',), 'no such table',
     'The table does not exist -- migrations have not run against this database, '
     'or the code is pointed at the wrong one.',
     'Print the database file or host in use, then check showmigrations.'),
    (('OperationalError',), 'locked',
     'SQLite is refusing a concurrent write. Two writers, or one long transaction '
     'holding the lock.',
     'Find the longest-running transaction. On SQLite this is a design constraint '
     'rather than a bug to patch.'),
    (('DoesNotExist',), '',
     'A .get() found nothing, and the code assumed it always would.',
     'Replace with .filter(...).first() and print the result. Then decide what the '
     'absence means -- that decision is the real fix.'),
    (('MultipleObjectsReturned',), '',
     'A .get() matched more than one row: the uniqueness the code assumed is not '
     'enforced by the database.',
     'Count the matches. If duplicates are legitimate, the query needs narrowing; '
     'if not, the table needs a constraint.'),
    (('ValidationError',), '',
     'A model or form rejected the data. The message names the field.',
     'Print the full error dict; it lists every field, not only the first.'),
    (('UnicodeDecodeError',), '',
     'A file is being read with the wrong encoding. On Windows the default is not '
     'UTF-8, so a file written elsewhere fails here and nowhere else.',
     'Open with encoding="utf-8" explicitly. If that fails too, print the first '
     'bytes to identify the real encoding.'),
    (('FileNotFoundError',), '',
     'A relative path is being resolved against the working directory rather than '
     'the project, and the working directory is not what the code assumes.',
     'Print os.getcwd() and the absolute path being opened.'),
    (('PermissionError',), '',
     'The file is open in another process, or the account cannot write there.',
     'Print the path and check what holds it. On Windows an open spreadsheet is '
     'the usual answer.'),
    (('JSONDecodeError',), '',
     'The body being parsed is not JSON -- commonly empty, or an HTML error page '
     'returned by a service that failed.',
     'Print the first 200 characters of the raw body before parsing.'),
    (('ConnectionError', 'URLError', 'TimeoutError'), '',
     'The service was unreachable or too slow. This is an environment fact, not '
     'a code defect, until it is shown to be reproducible.',
     'Call the same endpoint from the same host with curl. If that also fails, the '
     'code is not the problem.'),
    (('AssertionError',), '',
     'A test assertion failed, or an assert is being used for runtime validation '
     '-- which is dangerous, because asserts vanish under python -O.',
     'Print both sides of the comparison. If this is production validation, raise '
     'a real exception instead.'),
)


def _read_trace(text):
    """Parse a traceback: frames, the exception, and which frames are ours.

    Returns a dictionary. Everything in it is read from the text; nothing is
    inferred, so the caller can present it as fact.
    """
    body = str(text or '')
    frames = []
    for match in _FRAME_RE.finditer(body):
        path = match.group('file')
        frames.append({
            'file': path,
            'line': int(match.group('line')),
            'function': match.group('func').strip(),
            'project': not any(marker in path for marker in _FOREIGN_MARKERS),
        })

    exception_type, message = '', ''
    for raw in reversed([line.strip() for line in body.splitlines() if line.strip()]):
        if raw.startswith(('File "', 'Traceback', 'During handling',
                           'The above exception')):
            continue
        match = _EXCEPTION_RE.match(raw)
        if match:
            exception_type = match.group('type').split('.')[-1]
            message = (match.group('message') or '').strip()
            break

    project_frames = [frame for frame in frames if frame['project']]
    return {
        'frames': frames,
        'project_frames': project_frames,
        'deepest_project_frame': project_frames[-1] if project_frames else None,
        'deepest_frame': frames[-1] if frames else None,
        'exception_type': exception_type,
        'message': message,
        'chained': 'During handling of the above exception' in body
                   or 'The above exception was the direct cause' in body,
    }


def _diagnose(exception_type, message):
    """Candidate causes, the most specific match first.

    A diagnosis keyed on the message text beats one keyed only on the
    exception type, because 'AttributeError on NoneType' and 'AttributeError
    on a typo' need different next steps.
    """
    lowered = str(message or '').lower()
    kind = str(exception_type or '')
    specific, generic = [], []
    for types, fragment, cause, experiment in _DIAGNOSES:
        if kind not in types:
            continue
        if fragment and fragment in lowered:
            specific.append({'cause': cause, 'experiment': experiment,
                             'matched_on': f'the message contains "{fragment}"'})
        elif not fragment:
            generic.append({'cause': cause, 'experiment': experiment,
                            'matched_on': f'the exception type is {kind}'})
    return specific + generic


def _trace_lines(reading, limit=12):
    """The frame-by-frame reading, project frames marked."""
    lines = []
    for frame in reading['frames'][:limit]:
        marker = '>>' if frame['project'] else '  '
        origin = '' if frame['project'] else '   (library code, not ours)'
        lines.append(f'{marker} {frame["file"]}:{frame["line"]} in '
                     f'{frame["function"]}{origin}')
    if len(reading['frames']) > limit:
        lines.append(f'   ... {len(reading["frames"]) - limit} more frame(s)')
    return lines


@tool(name='dev.analyse_error', title='Analyse an error',
      description=('Read a traceback: the exception, the deepest frame in project '
                   'code, the call chain, then the most probable cause, the evidence '
                   'for it and the smallest experiment that confirms or eliminates it. '
                   'Saved as an artefact.'),
      group='Debugging', agent_types=('developer',), icon='fa-bug',
      capability='Analyse an error',
      parameters=_obj(
          ('error_text',),
          error_text=_s('The traceback or error message, pasted as it appeared.'),
          language=_s("Language. Default 'python'."),
          context=_s('What was being done when it happened.')))
def analyse_error(ctx, error_text, language='python', context=''):
    """Read the trace before theorising, and keep the two apart.

    The separation between proven and inferred is the whole value of this
    tool. A traceback proves where the failure surfaced; it rarely proves why,
    and a diagnosis presented as certainty gets acted on without the one
    experiment that would have refuted it.
    """
    if not str(error_text or '').strip():
        return _needs('There is no error text to read.',
                      'Paste the traceback exactly as it appeared, including the '
                      'File lines.')

    reading = _read_trace(error_text)
    diagnoses = _diagnose(reading['exception_type'], reading['message'])
    deepest = reading['deepest_project_frame'] or reading['deepest_frame']

    proven = []
    if reading['exception_type']:
        proven.append(f'The exception is {reading["exception_type"]}'
                      + (f': {reading["message"]}' if reading['message'] else '')
                      + '.')
    else:
        proven.append('No exception line could be identified in the text supplied, '
                      'so the type is unknown. Paste the last line of the traceback '
                      'as well as the frames.')
    if deepest:
        where = 'project code' if deepest.get('project') else 'library code'
        proven.append(f'It surfaced at {deepest["file"]}:{deepest["line"]} in '
                      f'{deepest["function"]} -- {where}.')
    if reading['project_frames']:
        chain = ' -> '.join(f'{frame["function"]}:{frame["line"]}'
                            for frame in reading['project_frames'])
        proven.append(f'The call chain through our code was: {chain}.')
    if reading['frames'] and not reading['project_frames']:
        proven.append('Every frame is library code. Either the failing call is made '
                      'from a template or a framework hook, or the traceback was '
                      'truncated before our frames.')
    if reading['chained']:
        proven.append('This traceback is chained: an earlier exception was raised '
                      'while handling another. The first one is usually the real '
                      'fault, and it is above.')

    lines = ['WHAT THE TRACE PROVES']
    lines.extend(f'- {line}' for line in proven)
    lines.append('')
    lines.append('FRAME BY FRAME (>> marks our code)')
    lines.extend(_trace_lines(reading) or ['  No File lines were found in the text.'])
    lines.append('')
    lines.append('WHAT IS INFERRED, most probable first')
    if diagnoses:
        for position, entry in enumerate(diagnoses[:3], start=1):
            lines.extend([
                f'{position}. {entry["cause"]}',
                f'   Evidence: {entry["matched_on"]}.'
                + (f' It surfaced in {deepest["function"]}.' if deepest else ''),
                f'   Smallest experiment: {entry["experiment"]}',
            ])
    else:
        lines.append('1. No pattern in the table matches this exception type, so '
                     'there is no inference worth stating. Read the deepest project '
                     'frame and print the values it uses on the line above.')
    if str(context or '').strip():
        lines.extend(['', 'CONTEXT SUPPLIED', f'  {str(context).strip()[:600]}'])
    lines.extend([
        '',
        'WHAT WOULD ELIMINATE THE FIRST GUESS',
        'Run the experiment above and look at the printed value. If it is what the '
        'code expects, the cause is elsewhere and the next candidate applies -- do '
        'not patch around the symptom in the meantime.',
    ])

    content = '\n'.join(lines)
    artifact = _save_artifact(
        ctx, kind='debug',
        title=f'Error analysis: {reading["exception_type"] or "unidentified"}',
        content=content, language=str(language or 'python'),
        explanation='', metadata={'exception': reading['exception_type'],
                                  'message': reading['message'][:400],
                                  'frames': len(reading['frames']),
                                  'project_frames': len(reading['project_frames'])})

    headline = (f'Read the traceback: {reading["exception_type"] or "unidentified error"}'
                + (f' at {deepest["file"]}:{deepest["line"]}' if deepest else '')
                + f'. {len(reading["project_frames"])} of {len(reading["frames"])} '
                + f'frame(s) are project code, and '
                + f'{len(diagnoses)} candidate cause(s) match.')

    return _artifact_result(artifact, headline,
                            data={'exception': reading['exception_type'],
                                  'message': reading['message'],
                                  'diagnoses': diagnoses[:3],
                                  'deepest_project_frame': deepest})


@tool(name='dev.analyse_stack_trace', title='Analyse a stack trace',
      description=('Read a stack trace frame by frame, marking which frames are '
                   'project code and which are library code, and say where to look '
                   'first. Saved as an artefact.'),
      group='Debugging', agent_types=('developer',), icon='fa-layer-group',
      capability='Analyse a stack trace',
      parameters=_obj(('trace_text',), trace_text=_s('The stack trace.')))
def analyse_stack_trace(ctx, trace_text):
    """The frame-by-frame reading on its own, without the diagnosis.

    Useful when the question is 'how did we get here' rather than 'why did it
    fail' -- a deep trace through framework code hides the two lines that are
    ours, and those two lines are where the bug is.
    """
    if not str(trace_text or '').strip():
        return _needs('There is no stack trace to read.',
                      'Paste the trace, including the File lines.')

    reading = _read_trace(trace_text)
    if not reading['frames']:
        return _needs('No frames could be read from that text.',
                      'A Python traceback contains lines of the form: File "path", '
                      'line 12, in function. Paste those.')

    ours = reading['project_frames']
    library = [frame for frame in reading['frames'] if not frame['project']]

    lines = [f'{len(reading["frames"])} frame(s): {len(ours)} in project code, '
             f'{len(library)} in libraries.',
             '',
             'OUTERMOST TO INNERMOST (>> marks our code)']
    lines.extend(_trace_lines(reading, limit=25))
    lines.append('')
    if ours:
        deepest = ours[-1]
        lines.extend([
            'WHERE TO LOOK FIRST',
            f'{deepest["file"]}:{deepest["line"]} in {deepest["function"]} -- the '
            f'deepest frame that is ours. Whatever value it passed onwards is the '
            f'thing the library then objected to.',
        ])
        if ours[0] is not deepest:
            lines.append(f'The entry point into our code was '
                         f'{ours[0]["file"]}:{ours[0]["line"]} in '
                         f'{ours[0]["function"]}.')
    else:
        lines.extend([
            'WHERE TO LOOK FIRST',
            'No frame in this trace is project code, so nothing here points at our '
            'source directly. Look at the call that entered the library: a template '
            'tag, a signal handler, a management command or a middleware.',
        ])
    if reading['exception_type']:
        lines.append('')
        lines.append(f'The exception at the end is {reading["exception_type"]}'
                     + (f': {reading["message"]}' if reading['message'] else '')
                     + '. dev.analyse_error takes that further.')

    content = '\n'.join(lines)
    artifact = _save_artifact(
        ctx, kind='debug', title='Stack trace reading',
        content=content, language='text',
        metadata={'frames': len(reading['frames']), 'project_frames': len(ours)})

    return _artifact_result(
        artifact,
        f'Read {len(reading["frames"])} frame(s); {len(ours)} are project code.'
        + (f' Start at {ours[-1]["file"]}:{ours[-1]["line"]}.' if ours else ''),
        data={'frames': reading['frames'][:25],
              'project_frames': len(ours)})


# -- Concrete replacements for the findings that have one -------------------
# A fix suggestion is only worth reading when it shows the replacement line.
# These are the findings where the replacement can be derived exactly; every
# other finding gets guidance instead, which is honest about the difference.

def _replacement_for(line):
    """A concrete replacement for one line, or None."""
    stripped = line.rstrip()
    indent = ' ' * _indent_of(line)

    if _BARE_EXCEPT.match(line):
        return [f'{indent}except Exception as exc:  # narrow this to what you handle',
                f'{indent}    logger.warning("...", exc_info=exc)']
    if _EQ_NONE.search(line):
        replaced = re.sub(r'\s*==\s*None', ' is None', stripped)
        replaced = re.sub(r'\s*!=\s*None', ' is not None', replaced)
        return [replaced]
    if _EQ_BOOL.search(line):
        replaced = re.sub(r'\s*==\s*True\b', '', stripped)
        replaced = re.sub(r'\s*!=\s*False\b', '', replaced)
        replaced = re.sub(r'if\s+(.+?)\s*==\s*False\b', r'if not \1', replaced)
        replaced = re.sub(r'if\s+(.+?)\s*!=\s*True\b', r'if not \1', replaced)
        return [replaced]
    if _HAS_KEY.search(line):
        return [re.sub(r'(\w+)\.has_key\(([^)]*)\)', r'\2 in \1', stripped)]
    if _LEN_ZERO.search(line):
        replaced = re.sub(r'len\s*\(([^)]*)\)\s*==\s*0', r'not \1', stripped)
        replaced = re.sub(r'len\s*\(([^)]*)\)\s*(!=|>)\s*0', r'\1', replaced)
        return [replaced]
    if _TYPE_EQ.search(line):
        replaced = re.sub(r'type\s*\(([^)]*)\)\s*==\s*(\w+)',
                          r'isinstance(\1, \2)', stripped)
        return [replaced]
    if _MUTABLE_DEFAULT.search(line):
        replaced = re.sub(r'=\s*(\[\s*\]|\{\s*\}|set\(\)|list\(\)|dict\(\))',
                          '=None', stripped)
        return [replaced,
                f'{indent}    # then, first line of the body:',
                f'{indent}    # items = items or []']
    if _IF_ASSIGN.match(line) or _WHILE_ASSIGN.match(line):
        return [re.sub(r'(?<![=!<>])=(?!=)', '==', stripped, count=1)]
    return None


@tool(name='dev.suggest_fix', title='Suggest a fix',
      description=('Suggest the smallest change that addresses an error or a defect, '
                   'shaped as a diff, together with the risk of applying it. Saved as '
                   'an artefact -- nothing is changed.'),
      group='Debugging', agent_types=('developer',), icon='fa-wrench',
      capability='Suggest a fix',
      parameters=_obj(
          (),
          error_text=_s('The error or traceback, if there is one.'),
          code=_s('The code the fix applies to.'),
          work_item_id=_i('Work item this belongs to.')))
def suggest_fix(ctx, error_text='', code='', work_item_id=None):
    """The smallest change, with its risk stated.

    Minimal on purpose. A large fix offered alongside a diagnosis that has not
    been confirmed changes several things at once, and then nobody knows which
    one mattered.
    """
    if not str(error_text or '').strip() and not str(code or '').strip():
        return _needs('There is nothing to fix.',
                      'Pass the error text, the code, or both.')

    item = None
    if work_item_id:
        item = _find_item(work_item_id)
        if item is None:
            return _no_item(work_item_id)

    lines, risks, changed = [], [], 0
    code_text = str(code or '')

    if code_text.strip():
        source_lines = code_text.replace('\r', '').split('\n')
        findings = _static_findings(code_text) + _simplification_findings(code_text)
        seen_lines = set()
        lines.append('SUGGESTED DIFF')
        lines.append('--- current')
        lines.append('+++ suggested')
        for finding in findings:
            number = finding['line']
            if number in seen_lines or number > len(source_lines):
                continue
            original = source_lines[number - 1]
            replacement = _replacement_for(original)
            if not replacement:
                continue
            seen_lines.add(number)
            changed += 1
            lines.append(f'@@ line {number} @@')
            lines.append(f'-{original}')
            lines.extend(f'+{new}' for new in replacement)
            risks.append(f'Line {number}: {finding["note"][:150]}')
            if changed >= 6:
                break
        if not changed:
            lines = ['SUGGESTED DIFF',
                     'None of the defects found in this code has a mechanical '
                     'replacement, so no diff is offered rather than a plausible '
                     'guess at one.']
            if findings:
                lines.append('')
                lines.append('What was found, for a person to fix:')
                lines.extend(f'  line {finding["line"]}: {finding["note"]}'
                             for finding in findings[:6])
            else:
                lines.append('')
                lines.append('The static checks found nothing to change in this code.')

    if str(error_text or '').strip():
        reading = _read_trace(error_text)
        diagnoses = _diagnose(reading['exception_type'], reading['message'])
        lines.extend(['', 'WHAT THE ERROR POINTS AT'])
        if reading['exception_type']:
            lines.append(f'{reading["exception_type"]}'
                         + (f': {reading["message"]}' if reading['message'] else ''))
        deepest = reading['deepest_project_frame']
        if deepest:
            lines.append(f'Deepest project frame: {deepest["file"]}:'
                         f'{deepest["line"]} in {deepest["function"]}.')
        if diagnoses:
            first = diagnoses[0]
            lines.extend([
                f'Most probable cause: {first["cause"]}',
                f'Smallest change that would confirm it: {first["experiment"]}',
            ])
            risks.append('The diagnosis is inferred from the exception type and '
                         'message, not proven. Confirm it before changing anything.')
        else:
            lines.append('No known pattern matches this exception, so no fix is '
                         'suggested from the error alone.')

    lines.extend(['', 'RISK OF APPLYING THIS'])
    if risks:
        lines.extend(f'- {risk}' for risk in risks[:8])
    lines.extend([
        '- Each change above is small, but none of them has been run. There is no '
        'test execution in this platform.',
        '- Apply one at a time and run the tests between. A batch of six fixes that '
        'turns green tells you nothing about which five were unnecessary.',
        '- If the code is covered by tests, they should fail before the fix and pass '
        'after. If they pass before, the fix is addressing something the tests do '
        'not describe.',
    ])

    content = '\n'.join(lines)
    artifact = _save_artifact(
        ctx, kind='fix', title='Suggested fix', content=content,
        explanation='A suggestion, shaped as a diff so the change is visible. Nothing '
                    'has been applied: this platform cannot modify a working tree.',
        work_item=item, metadata={'changes': changed})

    return _artifact_result(
        artifact,
        f'Suggested {changed} concrete line change(s)' if changed else
        'Analysed it, but offered no mechanical change -- see the artefact for what '
        'was found and why no diff is claimed',
        data={'change_count': changed, 'work_item_id': item.pk if item else None})


_DEBUG_PLANS = (
    (('intermittent', 'sometimes', 'occasionally', 'flaky', 'random'),
     'Establish how often it happens before changing anything. An intermittent '
     'fault that is "fixed" after one clean run is not fixed.'),
    (('slow', 'performance', 'timeout', 'hangs', 'freeze'),
     'Measure before theorising: time the operation and find which part of it '
     'dominates. Most performance guesses are wrong.'),
    (('data', 'wrong value', 'incorrect', 'mismatch'),
     'Find one concrete example -- a specific record with a specific wrong value. '
     'A class of wrongness cannot be debugged; an instance can.'),
    (('crash', 'exception', 'error', 'traceback', '500'),
     'Get the full traceback from the failing environment, not a summary of it.'),
    (('only in production', 'works locally', 'staging', 'deploy'),
     'List every difference between the environments: data volume, configuration, '
     'versions, timezone. The bug is in one of them.'),
)


@tool(name='dev.generate_debugging_steps', title='Generate debugging steps',
      description=('An ordered plan for finding a fault by bisection: what to '
                   'establish, what to eliminate and in what order. Saved as an '
                   'artefact.'),
      group='Debugging', agent_types=('developer',), icon='fa-magnifying-glass',
      capability='Generate debugging steps',
      parameters=_obj(
          ('problem_description',),
          problem_description=_s('What is going wrong.'),
          max_steps=_i('Most steps to produce. Default 8.')))
def generate_debugging_steps(ctx, problem_description, max_steps=8):
    """A bisection plan, in the order that halves the search space fastest.

    The ordering is the content. Anybody can list debugging activities; the
    value is starting with the step that eliminates the most possibilities,
    which is almost always reproducing the fault reliably.
    """
    if not str(problem_description or '').strip():
        return _needs('There is nothing to debug.', 'Describe what is going wrong.')

    lowered = str(problem_description).lower()
    steps = [
        'Reproduce it on demand. Write down the exact input, the exact steps and '
        'the exact output. Until it can be reproduced, nothing that follows can be '
        'tested, and a fix cannot be shown to have worked.',
        'Read the error or the wrong output literally. What does it actually say, '
        'as opposed to what it is assumed to mean? Most of the search space '
        'disappears at this step.',
        'Establish when it last worked. A commit, a deployment, a configuration '
        'change or a data change. If it never worked, that is a different '
        'investigation from a regression.',
        'Bisect the path, not the code. Add one print or one log line at the '
        'midpoint of the flow and ask whether the data is already wrong there. '
        'Repeat on whichever half was wrong. Two or three of these locate almost '
        'anything.',
        'Check the boundary the data crossed last: a request body, a file, an API '
        'response, a form. Bad data usually enters at a boundary and fails much '
        'later.',
        'Eliminate the environment. Run the same input in a different environment '
        'or against a copy of the data. If it behaves differently, the difference '
        'is the lead.',
        'Write the failing test now, before the fix. It states the fault precisely, '
        'and it is the only proof afterwards that the fix worked.',
        'Fix one thing, then rerun the reproduction. If two things were changed and '
        'it works, it is not known which mattered, and the other change is now '
        'untested code.',
    ]

    for keywords, extra in _DEBUG_PLANS:
        if any(word in lowered for word in keywords):
            steps.insert(1, extra)

    limit = max(1, min(_as_int(max_steps) or 8, 12))
    steps = steps[:limit]

    lines = [f'Debugging plan: {_first_sentence(problem_description, 110)}', '']
    lines.extend(f'{position}. {step}' for position, step in enumerate(steps, start=1))
    lines.extend([
        '',
        'STOPPING RULE',
        'If four of these steps have been worked through and the cause is still '
        'unknown, the assumption being made is wrong rather than the search being '
        'incomplete. Write down what is believed to be true and test the belief '
        'that seems least worth testing.',
    ])

    content = '\n'.join(lines)
    artifact = _save_artifact(
        ctx, kind='debug',
        title=f'Debugging plan: {_first_sentence(problem_description, 70)}',
        content=content, language='markdown', metadata={'steps': len(steps)})

    return _artifact_result(
        artifact,
        f'Produced a {len(steps)}-step bisection plan, ordered so the earliest '
        f'steps remove the most possibilities.',
        data={'step_count': len(steps)})


# ===========================================================================
# GROUP: Code Review
# ===========================================================================

_SIDE_EFFECT_MARKERS = (
    (('open(', '.write(', '.writelines(', 'os.remove', 'shutil.'), 'writes to the file system'),
    (('.save(', '.delete(', '.create(', '.update(', '.bulk_create', 'objects.'),
     'writes to the database'),
    (('requests.', 'urlopen', 'urllib', 'httpx', 'socket.'), 'makes a network call'),
    (('subprocess', 'os.system', 'popen'), 'runs another process'),
    (('print(',), 'prints to standard output'),
    (('logger.', 'logging.'), 'logs'),
    (('send_mail', 'send_email', '.send('), 'sends a message'),
    (('os.environ', 'settings.', 'getenv'), 'reads configuration'),
    (('global ',), 'mutates module-level state'),
    (('cache.', 'session['), 'writes to a cache or session'),
)


@tool(name='dev.identify_bugs', title='Identify bugs',
      description=('Run static checks over a block of code: bare excepts, mutable '
                   'default arguments, comparison against None, unclosed resources, '
                   'shadowed builtins, assignment where comparison was meant, '
                   'unreachable code, a loop variable read after its loop, string '
                   'concatenation in a loop and a method missing self. Reports the '
                   'line and the failure each one causes.'),
      group='Code Review', agent_types=('developer',), icon='fa-bug-slash',
      capability='Identify bugs in code',
      parameters=_obj(('code',), code=_s('The code to check.'),
                      language=_s("Language. Default 'python'.")))
def identify_bugs(ctx, code, language='python'):
    """Find real defects, and say so plainly when there are none.

    "Nothing blocking found" is a legitimate outcome and it is reported as
    one. A review that always produces findings teaches its reader that the
    findings are decoration.
    """
    if not str(code or '').strip():
        return _needs('There is no code to check.', 'Paste the code.')

    tongue = str(language or 'python').strip().lower()
    if tongue != 'python':
        message = (f'These checks are written for Python and would produce nonsense '
                   f'against {tongue}. Reading the {tongue} code by eye is more '
                   f'honest than a Python parser guessing at it -- '
                   f'dev.review_code still gives a structured review.')
        return ToolResult(ok=False, error='python only', text=message)

    findings = _static_findings(code)
    blocking = [f for f in findings if f['severity'] in ('critical', 'high')]

    if not findings:
        lines = str(code).strip().splitlines()
        return ToolResult(
            ok=True,
            text=(f'Nothing blocking found in {len(lines)} line(s). The checks that '
                  f'ran were: bare except, mutable default argument, "== None", '
                  f'resource opened without "with", shadowed builtin, assignment in a '
                  f'condition, unreachable code after return, loop variable used '
                  f'after its loop, string concatenation in a loop, and method '
                  f'missing self. None of them fired. That is not a claim that the '
                  f'code is correct -- these are pattern checks, not an understanding '
                  f'of what it is meant to do.'),
            data={'findings': [], 'count': 0, 'blocking': 0})

    lines = [f'{len(findings)} finding(s), {len(blocking)} of them blocking:']
    for finding in findings:
        lines.extend([
            f'  line {finding["line"]} [{finding["severity"]}] {finding["note"]}',
            f'    Fix: {finding["suggestion"]}',
        ])
    if blocking:
        lines.append('')
        lines.append(f'The {len(blocking)} blocking finding(s) each cause a concrete '
                     f'failure rather than reading badly. They are worth fixing before '
                     f'this merges.')

    return ToolResult(ok=True, text='\n'.join(lines),
                      data={'findings': findings, 'count': len(findings),
                            'blocking': len(blocking)})


@tool(name='dev.review_code', title='Review code',
      description=('Review code and record it: correctness findings first, then real '
                   'simplifications, with a verdict. Creates a CodeReview row. No '
                   'style padding.'),
      group='Code Review', agent_types=('developer',), icon='fa-magnifying-glass-chart',
      capability='Review code',
      parameters=_obj(
          (),
          code=_s('The code to review.'),
          title=_s('What is being reviewed.'),
          language=_s("Language. Default 'python'."),
          work_item_id=_i('Work item this review belongs to.')))
def review_code(ctx, code='', title='', language='python', work_item_id=None):
    """Correctness first, then simplification, then a verdict. Nothing else.

    The order is the discipline. A review that opens with naming preferences
    gets read as a matter of taste, and the null-dereference three findings
    down is read the same way.
    """
    if not str(code or '').strip():
        return _needs('There is no code to review.', 'Paste the code.')

    item = None
    if work_item_id:
        item = _find_item(work_item_id)
        if item is None:
            return _no_item(work_item_id)

    correctness = _static_findings(code)
    simplifications = _simplification_findings(code)
    findings = correctness + simplifications
    blocking = [f for f in findings if f['severity'] in ('critical', 'high')]

    verdict = ('request_changes' if blocking
               else ('comment' if findings else 'approve'))
    heading = str(title or '').strip() or 'Code review'
    body_lines = str(code).strip().splitlines()

    summary_lines = [
        f'Reviewed {len(body_lines)} line(s) of {language}.',
        f'{len(correctness)} correctness finding(s), '
        f'{len(simplifications)} simplification(s), '
        f'{len(blocking)} blocking.',
    ]
    if verdict == 'approve':
        summary_lines.append('Nothing blocking found. The pattern checks all passed, '
                             'which is not the same as the logic being right -- what '
                             'this code is meant to do was not stated, so that part '
                             'was not reviewed.')
    elif verdict == 'comment':
        summary_lines.append('Nothing blocking, but there are changes worth making. '
                             'None of them has to hold up a merge.')
    else:
        summary_lines.append(f'{len(blocking)} finding(s) cause a concrete failure. '
                             f'Changes needed before this merges.')

    review = CodeReview.objects.create(
        repository=(item.project.repository if item is not None else ''),
        title=heading[:250], summary='\n'.join(summary_lines), findings=findings,
        verdict=verdict, work_item=item, created_by_agent=_agent_of(ctx))

    lines = [f'Review #{review.pk}: {heading}', '', *summary_lines]
    if correctness:
        lines.extend(['', 'CORRECTNESS'])
        for finding in correctness:
            lines.extend([f'  line {finding["line"]} [{finding["severity"]}] '
                          f'{finding["note"]}',
                          f'    Fix: {finding["suggestion"]}'])
    if simplifications:
        lines.extend(['', 'SIMPLIFICATION'])
        for finding in simplifications:
            lines.extend([f'  line {finding["line"]} [{finding["severity"]}] '
                          f'{finding["note"]}',
                          f'    Fix: {finding["suggestion"]}'])
    lines.extend(['', f'VERDICT: {review.get_verdict_display()}'])
    if item is not None:
        lines.append(f'Recorded against {item.reference}.')

    return ToolResult(
        ok=True, text='\n'.join(lines),
        data={'review_id': review.pk, 'verdict': verdict, 'findings': findings,
              'blocking_count': review.blocking_count,
              'finding_count': review.finding_count},
        subject_label='marketing.codereview', subject_id=review.pk)


@tool(name='dev.suggest_improvements', title='Suggest improvements',
      description=('Suggest improvements to code with a stated focus -- readability, '
                   'performance, safety or testability -- keeping to changes that '
                   'have a reason. Saved as an artefact.'),
      group='Code Review', agent_types=('developer',), icon='fa-arrow-up-right-dots',
      capability='Suggest improvements',
      parameters=_obj(
          ('code',),
          code=_s('The code to improve.'),
          language=_s("Language. Default 'python'."),
          focus=_enum('What to optimise for.',
                      ('readability', 'performance', 'safety', 'testability'))))
def suggest_improvements(ctx, code, language='python', focus='readability'):
    """Improvements with reasons, ordered by the stated focus.

    Every suggestion below comes from something measured in the code -- a
    nesting depth, a loop, a length -- rather than from a preference, which is
    why the list is short and sometimes empty.
    """
    if not str(code or '').strip():
        return _needs('There is no code to improve.', 'Paste the code.')

    body = str(code)
    lines_of_code = [line for line in body.replace('\r', '').split('\n') if line.strip()]
    aim = str(focus or 'readability').strip().lower()

    findings = _simplification_findings(body)
    correctness = _static_findings(body)
    suggestions = []

    if aim == 'performance':
        for number, line in enumerate(body.split('\n'), start=1):
            if '.all()' in line and 'for ' in line:
                suggestions.append((number,
                                    'A queryset is iterated inside a loop, which is '
                                    'one query per iteration.',
                                    'Use select_related or prefetch_related, or fetch '
                                    'once outside the loop.'))
            if _CONCAT_IN_LOOP.match(line):
                suggestions.append((number,
                                    'Repeated string concatenation copies the whole '
                                    'string each time.',
                                    'Collect into a list and join once.'))
            if 'in ' in line and re.search(r'in\s+\[', line):
                suggestions.append((number,
                                    'Membership is tested against a list, which is a '
                                    'linear scan.',
                                    'Use a set literal for the membership test.'))
    elif aim == 'safety':
        for finding in correctness:
            if finding['severity'] in ('critical', 'high', 'medium'):
                suggestions.append((finding['line'], finding['note'],
                                    finding['suggestion']))
    elif aim == 'testability':
        for number, line in enumerate(body.split('\n'), start=1):
            if re.match(r'^\s*def\s+\w+\s*\(\s*self\s*\)\s*:', line):
                suggestions.append((number,
                                    'A method taking no arguments beyond self reads '
                                    'its inputs from state, so a test has to build '
                                    'the whole object to exercise it.',
                                    'Pass what it needs as parameters; the test then '
                                    'needs only those.'))
            for markers, effect in _SIDE_EFFECT_MARKERS[:4]:
                if any(marker in line for marker in markers):
                    suggestions.append((number,
                                        f'This line {effect}, mixed in with the logic '
                                        f'around it, so the logic cannot be tested '
                                        f'without it.',
                                        'Separate the decision from the effect: return '
                                        'what should happen, and let the caller do it.'))
                    break
        if len(lines_of_code) > 40:
            suggestions.append((len(lines_of_code),
                                f'{len(lines_of_code)} lines in one block means a test '
                                f'has to set up everything to reach anything.',
                                'Split at the seams; each piece then has its own test.'))
    else:
        for finding in findings:
            suggestions.append((finding['line'], finding['note'],
                                finding['suggestion']))
        deepest = max((_indent_of(line) for line in lines_of_code), default=0)
        if deepest >= 16:
            suggestions.append((0,
                                f'Nesting reaches {deepest} spaces, so the main path '
                                f'is the most indented thing in the function.',
                                'Return early on the failures and let the ordinary '
                                'case sit at the top level.'))

    seen, unique = set(), []
    for number, note, fix in suggestions:
        key = (number, note[:60])
        if key in seen:
            continue
        seen.add(key)
        unique.append((number, note, fix))

    if not unique:
        content = (f'No {aim} improvement is worth making to these '
                   f'{len(lines_of_code)} line(s).\n\n'
                   f'The checks for this focus all passed. Suggesting something '
                   f'anyway would be padding, and padding is what makes reviews '
                   f'get skimmed.')
    else:
        rendered = [f'{len(unique)} improvement(s) for {aim}:', '']
        for number, note, fix in unique[:10]:
            where = f'line {number}' if number else 'overall'
            rendered.extend([f'{where}: {note}', f'  Change: {fix}', ''])
        content = '\n'.join(rendered)

    artifact = _save_artifact(
        ctx, kind='review', title=f'{aim.capitalize()} improvements',
        content=content, language=str(language or 'python'),
        explanation=f'Focus was {aim}. Each suggestion comes from something measured '
                    f'in the code, not from a style preference, which is why the list '
                    f'is short.',
        metadata={'focus': aim, 'count': len(unique)})

    return _artifact_result(
        artifact,
        f'{len(unique)} {aim} improvement(s) found in {len(lines_of_code)} line(s).'
        if unique else
        f'No {aim} improvement worth making in {len(lines_of_code)} line(s).',
        data={'focus': aim, 'suggestion_count': len(unique)})


@tool(name='dev.explain_code', title='Explain code',
      description=('A structured walk-through of a block of code: what it does, its '
                   'control flow, its inputs and outputs, its side effects and its '
                   'risks. Saved as an artefact.'),
      group='Code Review', agent_types=('developer',), icon='fa-book-open',
      capability='Explain code',
      parameters=_obj(('code',), code=_s('The code to explain.'),
                      language=_s("Language. Default 'python'.")))
def explain_code(ctx, code, language='python'):
    """Explain code from what is in it, in the order a reader needs it.

    Structure, then flow, then the boundary (inputs and outputs), then the
    side effects, then the risks. The side effects matter most: they are what
    makes the code hard to call, and they are invisible from the signature.
    """
    if not str(code or '').strip():
        return _needs('There is no code to explain.', 'Paste the code.')

    body = str(code).replace('\r', '')
    all_lines = body.split('\n')
    imports, classes, functions, returns = [], [], [], []
    loops, branches, effects = 0, 0, {}

    for number, raw in enumerate(all_lines, start=1):
        stripped = raw.strip()
        if not stripped or stripped.startswith('#'):
            continue
        if stripped.startswith(('import ', 'from ')):
            imports.append((number, stripped))
        class_match = _CLASS_LINE.match(raw)
        if class_match:
            classes.append((number, class_match.group(2)))
        def_match = _DEF_LINE.match(raw)
        if def_match:
            arguments = [part.strip() for part in def_match.group(3).split(',')
                         if part.strip() and part.strip() not in ('self', 'cls')]
            functions.append((number, def_match.group(2), arguments))
        if stripped.startswith(('for ', 'while ')):
            loops += 1
        if stripped.startswith(('if ', 'elif ', 'try:', 'except', 'match ')):
            branches += 1
        if stripped.startswith('return'):
            returns.append((number, stripped[:80]))
        for markers, effect in _SIDE_EFFECT_MARKERS:
            if any(marker in stripped for marker in markers):
                effects.setdefault(effect, []).append(number)

    risks = _static_findings(body)
    real_lines = [line for line in all_lines if line.strip()]

    lines = ['WHAT IT DOES']
    if classes and functions:
        lines.append(f'Defines {len(classes)} class(es) and {len(functions)} '
                     f'function(s) or method(s) across {len(real_lines)} lines of code.')
    elif functions:
        lines.append(f'Defines {len(functions)} function(s) across '
                     f'{len(real_lines)} lines of code.')
    else:
        lines.append(f'{len(real_lines)} line(s) of code at module level, with no '
                     f'function or class definitions -- it runs on import.')
    for number, name in classes:
        own = [f for f in functions if f[0] > number]
        lines.append(f'  class {name} (line {number})'
                     + (f', first method {own[0][1]}' if own else ', no methods'))
    for number, name, arguments in functions[:12]:
        lines.append(f'  {name}({", ".join(arguments) or "no arguments"}) '
                     f'-- line {number}')

    lines.extend(['', 'CONTROL FLOW',
                  f'{branches} branch point(s) and {loops} loop(s). '
                  + ('Straight-line code with few decisions.' if branches <= 2
                     else 'Enough branching that the paths need enumerating before '
                          'it can be tested properly.')])
    deepest = max((_indent_of(line) for line in real_lines), default=0)
    lines.append(f'Deepest indentation is {deepest} spaces, about '
                 f'{max(deepest // 4, 1)} level(s) of nesting.')

    lines.extend(['', 'INPUTS AND OUTPUTS'])
    if imports:
        lines.append('Depends on: ' + ', '.join(text for _n, text in imports[:8]))
    else:
        lines.append('No imports, so it depends only on builtins and on what is '
                     'passed to it.')
    if functions:
        every_argument = sorted({argument.split(':')[0].split('=')[0].strip()
                                 for _n, _name, arguments in functions
                                 for argument in arguments})
        lines.append('Takes: ' + (', '.join(every_argument) or 'no parameters'))
    lines.append(f'Returns: {len(returns)} return statement(s)'
                 + (f' -- e.g. {returns[0][1]} at line {returns[0][0]}'
                    if returns else ', so it returns None on every path'))

    lines.extend(['', 'SIDE EFFECTS'])
    if effects:
        for effect, numbers in effects.items():
            shown = ', '.join(str(number) for number in numbers[:6])
            lines.append(f'  It {effect} (line(s) {shown}).')
        lines.append('These are the reason it cannot be called freely in a test: each '
                     'one has to be arranged or isolated.')
    else:
        lines.append('  None detected. It computes and returns, which makes it '
                     'straightforward to test.')

    lines.extend(['', 'RISKS'])
    if risks:
        for finding in risks[:8]:
            lines.append(f'  line {finding["line"]} [{finding["severity"]}] '
                         f'{finding["note"]}')
    else:
        lines.append('  The static checks found nothing. What the code is meant to '
                     'achieve was not stated, so correctness against intent was not '
                     'assessed -- only patterns that fail regardless of intent.')

    content = '\n'.join(lines)
    artifact = _save_artifact(
        ctx, kind='explanation', title='Code explanation',
        content=content, language=str(language or 'python'),
        metadata={'classes': len(classes), 'functions': len(functions),
                  'branches': branches, 'loops': loops,
                  'effects': list(effects), 'risks': len(risks)})

    return _artifact_result(
        artifact,
        f'Walked through {len(real_lines)} line(s): {len(classes)} class(es), '
        f'{len(functions)} function(s), {branches} branch point(s), '
        f'{len(effects)} kind(s) of side effect, {len(risks)} risk(s).',
        data={'functions': [name for _n, name, _a in functions],
              'side_effects': list(effects), 'risk_count': len(risks)})


# ===========================================================================
# GROUP: Testing
# ===========================================================================

@tool(name='dev.generate_unit_tests', title='Generate unit tests',
      description=('Scaffold unit tests covering the ordinary path, the boundaries and '
                   'the failures, each test named for what it proves. Saved as an '
                   'artefact.'),
      group='Testing', agent_types=('developer',), icon='fa-vial',
      capability='Generate unit tests',
      parameters=_obj(
          ('target_description',),
          target_description=_s('What is being tested.'),
          language=_s("Language. Default 'python'."),
          framework=_enum('Test framework.', ('django', 'pytest', 'unittest')),
          work_item_id=_i('Work item this belongs to.')))
def generate_unit_tests(ctx, target_description, language='python',
                        framework='django', work_item_id=None):
    """Tests named for what they prove, not for the method they call.

    A test called test_create is a label; a test called
    test_rejects_invoice_with_no_lines is an assertion about the system that a
    reader can check against the requirement.
    """
    if not str(target_description or '').strip():
        return _needs('There is nothing to test.',
                      'Say what behaviour the tests should cover.')

    item = None
    if work_item_id:
        item = _find_item(work_item_id)
        if item is None:
            return _no_item(work_item_id)

    subject = _singular(_snake(' '.join(_nouns(target_description)[:2]) or 'subject',
                               'subject'))
    class_name = _pascal(target_description, 'Behaviour')
    shape = str(framework or 'django').strip().lower()
    lowered = str(target_description).lower()

    cases = [
        (f'test_{subject}_ordinary_case_produces_the_expected_result',
         'The ordinary path. If this fails, nothing else matters.',
         'the documented result for valid input'),
        (f'test_{subject}_rejects_missing_required_input',
         'Missing input must be refused rather than stored half-formed.',
         'a raised error naming the missing field'),
        (f'test_{subject}_handles_empty_collection',
         'Empty is the boundary that gets missed: it should be an empty result, not '
         'a crash and not a wrong total.',
         'an empty result, no exception'),
    ]
    if any(word in lowered for word in ('permission', 'role', 'auth', 'login',
                                        'owner', 'admin')):
        cases.append((f'test_{subject}_refuses_user_without_permission',
                      'Authorisation must be enforced on the server, not by hiding '
                      'the control.',
                      'a refusal, and no change to the data'))
    if any(word in lowered for word in ('unique', 'duplicate', 'once', 'idempotent')):
        cases.append((f'test_{subject}_second_identical_call_does_not_duplicate',
                      'The operation runs twice in real life -- a retry, a double '
                      'click, a replayed webhook.',
                      'one record, not two'))
    if any(word in lowered for word in ('amount', 'price', 'total', 'money',
                                        'invoice', 'tax')):
        cases.append((f'test_{subject}_rounds_to_two_decimals',
                      'Money arithmetic that rounds late is wrong by a cent, which is '
                      'the kind of wrong that gets noticed.',
                      'the exact expected amount, compared as Decimal'))
    if any(word in lowered for word in ('date', 'time', 'schedule', 'expire',
                                        'deadline')):
        cases.append((f'test_{subject}_at_the_exact_boundary_moment',
                      'The boundary itself -- is the deadline inclusive? The code has '
                      'an answer whether or not anybody chose it.',
                      'the behaviour the requirement states for the boundary'))
    if any(word in lowered for word in ('api', 'request', 'external', 'integration',
                                        'service')):
        cases.append((f'test_{subject}_when_the_dependency_is_unavailable',
                      'The dependency will be down at some point. What happens then '
                      'should be a decision, not an accident.',
                      'a handled failure, surfaced to the caller'))
    cases.append((f'test_{subject}_leaves_nothing_behind_when_it_fails',
                  'A partial write is worse than a clean failure, because nothing '
                  'reports it.',
                  'no partial record after the failure'))

    if shape == 'pytest':
        lines = ['import pytest', '', '']
        for name, why, expectation in cases:
            lines.extend([
                f'def {name}():',
                f'    """{why}',
                '',
                f'    Expects: {expectation}.',
                '    """',
                '    # Arrange: build the minimum input this case needs.',
                '    # Act:     call the behaviour under test.',
                '    # Assert:  ' + expectation,
                '    pytest.fail("test body not written yet")',
                '',
            ])
    else:
        base = 'TestCase' if shape == 'django' else 'unittest.TestCase'
        lines = (['from django.test import TestCase', '', ''] if shape == 'django'
                 else ['import unittest', '', ''])
        lines.extend([
            f'class {class_name}Tests({base}):',
            f'    """Tests for: {_first_sentence(target_description)}"""',
            '',
            '    def setUp(self):',
            '        # Build only what every test here needs; anything specific to',
            '        # one case belongs in that case, where it can be read.',
            '        pass',
            '',
        ])
        for name, why, expectation in cases:
            lines.extend([
                f'    def {name}(self):',
                f'        """{why}',
                '',
                f'        Expects: {expectation}.',
                '        """',
                '        # Arrange / Act / Assert',
                f'        self.fail("{name} has no body yet")',
                '',
            ])
        if shape == 'unittest':
            lines.extend(['', "if __name__ == '__main__':", '    unittest.main()'])

    explanation = '\n'.join([
        'WHAT IS COVERED',
        f'{len(cases)} case(s): the ordinary path, missing input, the empty '
        f'collection, and whichever of permissions, duplication, money, time '
        f'boundaries and dependency failure the description implied.',
        '',
        'WHAT IS NOT',
        '- No test has a body. The names and the docstrings state what each one has '
        'to prove; the arrangement depends on the real code.',
        '- No test has been run. This platform cannot execute anything.',
        '',
        'MOST LIKELY THING TO BE WRONG',
        'A missing case for a rule the description did not mention. Read the '
        'acceptance criteria against this list -- every criterion should map to at '
        'least one test name here, and any that does not is untested.',
    ])

    artifact = _save_artifact(
        ctx, kind='test', title=f'Tests: {_first_sentence(target_description, 70)}',
        content='\n'.join(lines), language=str(language or 'python'),
        explanation=explanation, suggested_path='marketing/tests.py',
        work_item=item, metadata={'framework': shape, 'cases': len(cases)})

    return _artifact_result(
        artifact,
        f'Generated {len(cases)} {shape} test(s), each named for what it proves: '
        + ', '.join(name for name, _w, _e in cases[:4])
        + (f' and {len(cases) - 4} more.' if len(cases) > 4 else '.'),
        data={'cases': [name for name, _w, _e in cases], 'framework': shape})


# Test-case categories, in the order a test plan should cover them. Each is
# (category, scenario template, input template, expected template).
_CASE_TEMPLATES = (
    ('happy path', 'A valid {subject} is processed', 'a complete, valid {subject}',
     'the documented success result'),
    ('validation', 'A required field is missing', 'a {subject} with one field absent',
     'refused, naming the missing field'),
    ('boundary', 'The smallest permitted value', 'a {subject} at the lower limit',
     'accepted, and handled the same as any other'),
    ('boundary', 'One beyond the permitted value',
     'a {subject} one past the limit', 'refused, with the limit stated'),
    ('empty state', 'Nothing to process at all', 'no {subject} at all',
     'an empty result, not an error'),
    ('invalid input', 'A wrong type where a value was expected',
     'text where a number belongs', 'refused before anything is stored'),
    ('permission', 'A user who is not allowed', 'a valid {subject}, wrong user',
     'refused with 403, and no change to the data'),
    ('duplication', 'The same operation twice',
     'the identical {subject} submitted again', 'one record, not two'),
    ('dependency failure', 'A service it depends on is down',
     'a valid {subject}, dependency unavailable',
     'a handled failure reported to the caller'),
    ('volume', 'Many at once', 'several thousand {subject} records',
     'completes without timing out, and the query count does not grow per record'),
    ('encoding', 'Non-ASCII text', 'a {subject} with accents and emoji',
     'stored and returned unchanged'),
    ('concurrency', 'Two writers at the same moment',
     'two simultaneous updates to one {subject}',
     'one wins cleanly; no interleaved half-written row'),
)


@tool(name='dev.generate_test_cases', title='Generate test cases',
      description=('A test-case table -- id, scenario, input, expected, type -- rather '
                   'than code, for a feature. Saved as an artefact.'),
      group='Testing', agent_types=('developer',), icon='fa-table-list',
      capability='Generate test cases',
      parameters=_obj(('feature_description',),
                      feature_description=_s('The feature to plan tests for.'),
                      count=_i('How many cases. Default 8, maximum 12.')))
def generate_test_cases(ctx, feature_description, count=8):
    """A table, not code. Useful before there is anything to run tests against."""
    if not str(feature_description or '').strip():
        return _needs('There is no feature to plan tests for.',
                      'Describe the feature.')

    nouns = _nouns(feature_description)
    subject = _singular(nouns[0]) if nouns else 'record'
    wanted = max(1, min(_as_int(count) or 8, len(_CASE_TEMPLATES)))
    lowered = str(feature_description).lower()

    ordered = list(_CASE_TEMPLATES)
    # Cases whose category the description already talks about come first, so
    # the table leads with what this feature has actually raised.
    ordered.sort(key=lambda entry: 0 if entry[0].split()[0] in lowered else 1)

    rows = []
    for position, (kind, scenario, given, expected) in enumerate(ordered[:wanted],
                                                                 start=1):
        rows.append({
            'id': f'TC-{position:02d}',
            'scenario': scenario.format(subject=subject),
            'input': given.format(subject=subject),
            'expected': expected.format(subject=subject),
            'type': kind,
        })

    width = max(len(row['scenario']) for row in rows)
    lines = [f'Test cases for: {_first_sentence(feature_description, 100)}', '',
             f'{"ID":<7} {"TYPE":<20} {"SCENARIO":<{width}}  INPUT -> EXPECTED']
    for row in rows:
        lines.append(f'{row["id"]:<7} {row["type"]:<20} {row["scenario"]:<{width}}  '
                     f'{row["input"]} -> {row["expected"]}')
    lines.extend([
        '',
        'These are cases to write, not results. Each one needs a real input value '
        'before it can be run -- "a complete, valid record" is a placeholder for a '
        'specific record somebody has to choose.',
    ])

    content = '\n'.join(lines)
    artifact = _save_artifact(
        ctx, kind='test',
        title=f'Test cases: {_first_sentence(feature_description, 70)}',
        content=content, language='text',
        explanation='A plan rather than code. The categories cover the ordinary path, '
                    'the boundaries, the failures and the operational realities '
                    '(duplication, volume, encoding, concurrency) that get discovered '
                    'in production when they are not tested.',
        metadata={'cases': len(rows)})

    return _artifact_result(
        artifact,
        f'Produced {len(rows)} test case(s) for "{subject}" across '
        f'{len({row["type"] for row in rows})} categories.',
        data={'cases': rows, 'count': len(rows)})


_EDGE_CASES = (
    (('list', 'search', 'filter', 'report', 'table', 'query'),
     'An empty result set',
     'The empty case is the one nobody demonstrates, and it is the first thing a '
     'new user sees.'),
    (('list', 'page', 'report'),
     'Exactly one record, and exactly one page-full',
     'Off-by-one errors in pagination live at precisely these two counts.'),
    (('text', 'name', 'title', 'description', 'comment', 'input'),
     'Text at the maximum length, and one character beyond it',
     'A CharField truncating silently corrupts data; failing loudly does not.'),
    (('text', 'name', 'input', 'search'),
     'Non-ASCII characters, right-to-left text and emoji',
     'Encoding assumptions fail at the storage boundary, and the failure surfaces '
     'far from the input.'),
    (('date', 'time', 'schedule', 'deadline', 'expire'),
     'The boundary instant itself, and a value in a different timezone',
     'Inclusive or exclusive is a decision; if nobody makes it, the code makes it '
     'arbitrarily.'),
    (('amount', 'price', 'total', 'money', 'payment', 'invoice'),
     'Zero, a negative amount, and a value needing rounding',
     'Negative and zero are legitimate in refunds and credits, and rounding late '
     'produces amounts that do not reconcile.'),
    (('upload', 'file', 'image', 'import', 'csv'),
     'An empty file, a very large file and a file of the wrong type',
     'All three arrive in practice, and only the wrong-type case is usually checked.'),
    (('delete', 'remove', 'cancel', 'archive'),
     'Deleting something already deleted, and deleting something referenced elsewhere',
     'The first should be harmless; the second either cascades or refuses, and it '
     'must not do so by accident.'),
    (('permission', 'role', 'auth', 'login', 'user'),
     'A user whose permission was revoked mid-session',
     'Access checked only at login is not access control.'),
    (('api', 'request', 'external', 'sync', 'webhook'),
     'A slow response, a duplicate delivery and a malformed body',
     'Networks retry. Anything not idempotent will be called twice.'),
    (('concurrent', 'simultaneous', 'queue', 'job', 'worker'),
     'Two identical jobs starting at the same moment',
     'The window between check and write is where double-processing happens.'),
)

_UNIVERSAL_EDGE_CASES = (
    ('Missing or null where a value was assumed',
     'The most common runtime failure in any codebase is None reaching code that '
     'assumed an object.'),
    ('The operation performed twice',
     'Retries, double clicks and replayed messages all produce this, and it is '
     'rarely tested.'),
    ('Failure halfway through a multi-step operation',
     'Decide now whether it rolls back or reports partial success, because it will '
     'happen either way.'),
)


@tool(name='dev.suggest_edge_cases', title='Suggest edge cases',
      description=('Edge cases for a feature, each with why it matters. Saved as an '
                   'artefact.'),
      group='Testing', agent_types=('developer',), icon='fa-border-all',
      capability='Suggest edge cases',
      parameters=_obj(('feature_description',),
                      feature_description=_s('The feature to consider.'),
                      count=_i('How many to return. Default 8.')))
def suggest_edge_cases(ctx, feature_description, count=8):
    """Edge cases with reasons, chosen from the words in the description.

    The reason is not decoration. An edge case without one gets dropped in
    planning as paranoia; with one it gets argued about, which is the point.
    """
    if not str(feature_description or '').strip():
        return _needs('There is no feature to consider.', 'Describe the feature.')

    lowered = str(feature_description).lower()
    chosen = []
    for keywords, case, why in _EDGE_CASES:
        if any(word in lowered for word in keywords):
            if all(case != existing[0] for existing in chosen):
                chosen.append((case, why))
    for case, why in _UNIVERSAL_EDGE_CASES:
        if all(case != existing[0] for existing in chosen):
            chosen.append((case, why))

    limit = max(1, min(_as_int(count) or 8, 15))
    chosen = chosen[:limit]
    matched = len([1 for keywords, case, _w in _EDGE_CASES
                   if any(word in lowered for word in keywords)])

    lines = [f'Edge cases for: {_first_sentence(feature_description, 100)}', '']
    for position, (case, why) in enumerate(chosen, start=1):
        lines.extend([f'{position}. {case}', f'   Why it matters: {why}', ''])
    lines.append(f'{matched} of these came from the vocabulary of the description; '
                 f'the rest apply to any change and are included because they are '
                 f'the ones most often left out.')

    content = '\n'.join(lines)
    artifact = _save_artifact(
        ctx, kind='test',
        title=f'Edge cases: {_first_sentence(feature_description, 70)}',
        content=content, language='text', metadata={'count': len(chosen)})

    return _artifact_result(
        artifact,
        f'Identified {len(chosen)} edge case(s), each with the reason it matters.',
        data={'edge_cases': [{'case': case, 'why': why} for case, why in chosen],
              'count': len(chosen)})


_TEST_NAME_PATTERNS = (
    re.compile(r'^(?:FAIL|ERROR):\s*(?P<name>\w+)\s*\((?P<where>[\w.]+)\)'),
    re.compile(r'^_{3,}\s*(?P<name>[\w.\[\]-]+)\s*_{3,}$'),
    re.compile(r'^(?P<where>[\w/\\.]+)::(?P<name>\w+)\s'),
    re.compile(r'^(?P<name>test_\w+)\s+\((?P<where>[\w.]+)\)\s*\.\.\.\s*(FAIL|ERROR)'),
)

_ASSERT_PATTERNS = (
    re.compile(r'^E?\s*AssertionError:\s*(?P<detail>.+)$'),
    re.compile(r'^E?\s*assert\s+(?P<detail>.+)$'),
    re.compile(r'^\s*(?P<detail>.+\s(?:!=|==)\s.+)$'),
)


@tool(name='dev.analyse_test_failure', title='Analyse a test failure',
      description=('Read test output: which test failed, which assertion, expected '
                   'against actual, and the most likely cause. Saved as an artefact.'),
      group='Testing', agent_types=('developer',), icon='fa-flask-vial',
      capability='Analyse a test failure',
      parameters=_obj(('failure_output',),
                      failure_output=_s('The test runner output, pasted as it appeared.')))
def analyse_test_failure(ctx, failure_output):
    """Read the failure, then say what it means -- and keep the two apart.

    A failing test is better evidence than a bug report, because it names the
    expectation that was violated. The job here is to extract that
    expectation, not to guess at the code.
    """
    if not str(failure_output or '').strip():
        return _needs('There is no test output to read.',
                      'Paste the runner output, including the assertion line.')

    body = str(failure_output).replace('\r', '')
    raw_lines = [line.rstrip() for line in body.split('\n')]

    tests, assertions = [], []
    for line in raw_lines:
        stripped = line.strip()
        if not stripped:
            continue
        for pattern in _TEST_NAME_PATTERNS:
            match = pattern.match(stripped)
            if match:
                name = match.groupdict().get('name', '')
                where = match.groupdict().get('where', '')
                if name and (name, where) not in tests:
                    tests.append((name, where))
                break
        for pattern in _ASSERT_PATTERNS:
            match = pattern.match(stripped)
            if match:
                detail = match.group('detail').strip()
                if detail and detail not in assertions and len(detail) < 300:
                    assertions.append(detail)
                break

    reading = _read_trace(body)
    counts = {
        'failed': len(re.findall(r'\bFAILED?\b', body)),
        'errors': len(re.findall(r'\bERRORS?\b|\bERROR:', body)),
        'passed': next((int(number) for number in re.findall(r'(\d+)\s+passed', body)),
                       0),
    }

    expected, actual = '', ''
    for detail in assertions:
        for separator in (' != ', ' == ', ' not equal to '):
            if separator in detail:
                halves = detail.split(separator, 1)
                expected, actual = halves[0].strip(), halves[1].strip()
                break
        if expected:
            break

    lines = ['WHAT THE OUTPUT SAYS']
    if tests:
        for name, where in tests[:6]:
            lines.append(f'- Failing test: {name}' + (f' in {where}' if where else ''))
    else:
        lines.append('- No test name could be identified. Paste the block that starts '
                     'with FAIL:, ERROR: or the underscored pytest heading.')
    if assertions:
        lines.append(f'- The assertion that failed: {assertions[0]}')
        for extra in assertions[1:3]:
            lines.append(f'  also reported: {extra}')
    if expected:
        lines.extend([f'- Expected: {expected}', f'- Actual:   {actual}'])
    if reading['exception_type'] and reading['exception_type'] != 'AssertionError':
        lines.append(f'- This is an error rather than a failed assertion: '
                     f'{reading["exception_type"]}'
                     + (f': {reading["message"]}' if reading['message'] else '')
                     + '. The test did not get as far as asserting anything.')
    if reading['deepest_project_frame']:
        frame = reading['deepest_project_frame']
        lines.append(f'- Deepest project frame: {frame["file"]}:{frame["line"]} in '
                     f'{frame["function"]}.')
    if counts['passed'] or counts['failed']:
        lines.append(f'- Counts as reported: {counts["failed"]} failure marker(s), '
                     f'{counts["passed"]} passed.')

    lines.extend(['', 'MOST LIKELY CAUSE'])
    if reading['exception_type'] and reading['exception_type'] != 'AssertionError':
        diagnoses = _diagnose(reading['exception_type'], reading['message'])
        if diagnoses:
            lines.extend([f'{diagnoses[0]["cause"]}',
                          f'Evidence: {diagnoses[0]["matched_on"]}.',
                          f'Next step: {diagnoses[0]["experiment"]}'])
        else:
            lines.append('The test raised rather than asserted, and the exception is '
                         'not one with a known pattern. Read the deepest project frame.')
    elif expected and actual:
        lines.append('The code produced a different value from the one the test '
                     'requires. Two possibilities, and they need different fixes:')
        lines.append('  1. The code is wrong -- fix the code.')
        lines.append('  2. The expectation is stale -- the behaviour changed '
                     'deliberately and the test was not updated.')
        lines.append('Decide which by reading the requirement, not by making the test '
                     'pass. Changing an assertion until it agrees with the code is how '
                     'a test stops testing anything.')
    elif tests:
        lines.append('A test failed but no assertion detail was captured. Run that '
                     'one test alone with higher verbosity; the runner will show both '
                     'sides of the comparison.')
    else:
        lines.append('Not enough of the output was supplied to say anything useful.')

    if tests:
        lines.extend(['', 'HOW TO NARROW IT',
                      f'Run only the failing test -- for Django: '
                      f'python manage.py test {tests[0][1] or "marketing"}.'
                      f'{tests[0][0]} -- so nothing else in the suite is in the way.'])

    content = '\n'.join(lines)
    artifact = _save_artifact(
        ctx, kind='debug',
        title=f'Test failure: {tests[0][0] if tests else "unidentified"}',
        content=content, language='text',
        metadata={'tests': [name for name, _w in tests], 'assertions': assertions[:3],
                  'exception': reading['exception_type']})

    return _artifact_result(
        artifact,
        (f'Read the failure: {tests[0][0]}' if tests else 'Read the test output')
        + (f' -- expected {expected[:60]}, got {actual[:60]}.' if expected
           else (f' -- raised {reading["exception_type"]}.'
                 if reading['exception_type'] else '.')),
        data={'tests': [{'name': name, 'where': where} for name, where in tests],
              'expected': expected, 'actual': actual,
              'exception': reading['exception_type']})


# ===========================================================================
# DOCUMENTATION
#
# The same boundary applies. A README section is an artefact, not a change to
# README.md, and a commit message is a suggestion for a commit a person will
# make. Nothing here reaches a working tree.
# ===========================================================================

_DOC_SHAPES = {
    'reference': (
        ('Purpose', 'What this is for, in one paragraph.'),
        ('Interface', 'The names a caller uses, with their arguments.'),
        ('Behaviour', 'What happens on the ordinary path.'),
        ('Failure modes', 'What goes wrong, and what the caller sees.'),
        ('Example', 'The smallest useful call.'),
    ),
    'guide': (
        ('Before you start', 'What must already be true.'),
        ('Steps', 'The ordered sequence, one action per step.'),
        ('Checking it worked', 'The observable result at the end.'),
        ('If it fails', 'The two or three most likely causes.'),
    ),
    'overview': (
        ('What it does', 'The one-paragraph answer.'),
        ('How it fits together', 'The parts, and what each is responsible for.'),
        ('Design decisions', 'What was chosen, and what it was chosen over.'),
        ('Limitations', 'What it deliberately does not do.'),
    ),
    'runbook': (
        ('Symptom', 'What somebody notices first.'),
        ('Immediate checks', 'The fastest way to confirm or eliminate it.'),
        ('Resolution', 'The steps that fix it.'),
        ('Escalation', 'When to stop and who to involve.'),
    ),
}

_AUDIENCE_NOTE = {
    'developer': 'Written for a developer: names things exactly and assumes the stack.',
    'reviewer': 'Written for a reviewer: leads with the risk and what to check.',
    'operator': 'Written for whoever runs it: commands and observable outcomes.',
    'newcomer': 'Written for somebody new: defines the vocabulary before using it.',
}


@tool(name='dev.generate_documentation',
      title='Write documentation',
      description=('Draft reference documentation, a how-to guide, an overview or a '
                   'runbook for a named subject. Saves it as a documentation artefact '
                   'and returns the text. Choose the kind that matches what the reader '
                   'needs: reference to look something up, guide to follow steps, '
                   'overview to understand the shape, runbook to fix an incident.'),
      group='Documentation', agent_types=('developer',), icon='fa-book',
      parameters=_obj(
          required=('subject',),
          subject=_s('What the documentation is about, e.g. "the approval queue".'),
          kind=_enum('The shape of the document.',
                     ('reference', 'guide', 'overview', 'runbook')),
          audience=_enum('Who is reading it.',
                         ('developer', 'reviewer', 'operator', 'newcomer')),
          notes=_s('Anything known about the subject that should appear in it.'),
      ))
def generate_documentation(ctx, subject, kind='reference', audience='developer',
                           notes=''):
    """Draft a documentation artefact with a real structure for its kind."""
    kind = kind if kind in _DOC_SHAPES else 'reference'
    audience = audience if audience in _AUDIENCE_NOTE else 'developer'
    shape = _DOC_SHAPES[kind]

    title = f'{subject.strip()[:120]}'
    lines = [f'# {title}', '']
    if notes.strip():
        lines.extend([_first_sentence(notes, 240), ''])

    known = _sentences(notes)
    for index, (heading, prompt) in enumerate(shape):
        lines.append(f'## {heading}')
        lines.append('')
        supplied = known[index] if index < len(known) else ''
        if supplied:
            lines.append(supplied)
        else:
            lines.append(f'TO WRITE -- {prompt}')
        lines.append('')

    explanation = (
        f'{_AUDIENCE_NOTE[audience]} The headings are the ones this kind of document '
        f'needs, and every section a person still has to write is marked TO WRITE '
        f'rather than filled with plausible-sounding text. That is deliberate: an '
        f'invented sentence in documentation is worse than a visible gap, because '
        f'the gap gets filled and the invention gets believed.')

    artifact = _save_artifact(
        ctx, kind='doc', title=f'{kind.capitalize()}: {title}',
        content='\n'.join(lines), language='markdown', explanation=explanation,
        suggested_path=f'docs/{_snake(subject, "subject")}.md',
        metadata={'kind': kind, 'audience': audience,
                  'sections': [heading for heading, _p in shape]})

    unwritten = sum(1 for line in lines if line.startswith('TO WRITE'))
    return _artifact_result(
        artifact,
        f'Drafted {kind} documentation for "{title}" with '
        f'{len(shape)} sections.',
        extra=[f'{unwritten} section{"s" if unwritten != 1 else ""} still need a '
               f'person to supply the facts.' if unwritten else
               'Every section was filled from the notes you gave.'],
        data={'sections': [heading for heading, _p in shape],
              'unwritten_sections': unwritten})


_README_SECTIONS = {
    'overview': ('What this is',
                 ('One paragraph on what the project does.',
                  'One paragraph on who it is for.',
                  'One sentence on what it deliberately does not do.')),
    'installation': ('Installing it',
                     ('The prerequisites, with versions.',
                      'The commands, in order, in a fenced block.',
                      'How to tell the installation worked.')),
    'usage': ('Using it',
              ('The smallest complete example.',
               'The two or three things most people want next.',
               'Where the fuller reference lives.')),
    'configuration': ('Configuring it',
                      ('Every setting, its default, and what it changes.',
                       'Where the settings are stored.',
                       'What happens when one is missing.')),
    'testing': ('Running the tests',
                ('The command.',
                 'What the suite covers and what it does not.',
                 'How long it takes.')),
    'architecture': ('How it works',
                     ('The layers, and what each is responsible for.',
                      'The one decision a reader most needs to understand.',
                      'Where to look first when extending it.')),
    'contributing': ('Contributing',
                     ('The branch and review conventions.',
                      'The checks that must pass.',
                      'What a good change looks like here.')),
    'troubleshooting': ('Troubleshooting',
                        ('The three failures people actually hit.',
                         'The symptom, the cause and the fix for each.',
                         'The first diagnostic command to run.')),
}


@tool(name='dev.generate_readme_section',
      title='Draft a README section',
      description=('Draft one section of a README -- overview, installation, usage, '
                   'configuration, testing, architecture, contributing or '
                   'troubleshooting. Reads the project record for its name, '
                   'repository and stack when a project id is given.'),
      group='Documentation', agent_types=('developer',), icon='fa-file-lines',
      parameters=_obj(
          section=_enum('Which section to draft.', tuple(_README_SECTIONS)),
          project_id=_i('The project this README belongs to, if it is recorded.'),
          project_name=_s('The project name, when no project id is available.'),
          notes=_s('Facts to use rather than leaving a section to be written.'),
      ))
def generate_readme_section(ctx, section='installation', project_id=None,
                            project_name='', notes=''):
    """Draft one README section against the real project record where there is one."""
    if section not in _README_SECTIONS:
        return _needs(
            f'"{section}" is not a section this tool knows.',
            f'Choose one of: {", ".join(_README_SECTIONS)}.')

    project = _find_project(project_id) if project_id else None
    name = (project.name if project is not None else project_name).strip() or 'the project'
    heading, prompts = _README_SECTIONS[section]

    lines = [f'## {heading}', '']
    facts = _sentences(notes)

    if project is not None:
        detail = []
        if project.repository:
            detail.append(f'Repository: `{project.repository}`')
        if project.tech_stack:
            detail.append(f'Stack: {", ".join(str(t) for t in project.tech_stack)}')
        if project.jira_project_key:
            detail.append(f'Issue tracker: Jira project `{project.jira_project_key}`')
        if detail:
            lines.extend(detail + [''])

    for index, prompt in enumerate(prompts):
        supplied = facts[index] if index < len(facts) else ''
        lines.append(supplied if supplied else f'TO WRITE -- {prompt}')
        lines.append('')

    artifact = _save_artifact(
        ctx, kind='readme', title=f'README: {heading} ({name})',
        content='\n'.join(lines), language='markdown',
        explanation=(
            f'Drafted for {name}. Anything not supplied is marked TO WRITE, because a '
            f'README is the one document a reader trusts without checking, so a '
            f'confident invention in it does the most damage.'),
        suggested_path='README.md',
        work_item=None,
        metadata={'section': section, 'project': name})

    return _artifact_result(
        artifact, f'Drafted the "{heading}" section of the README for {name}.',
        data={'section': section, 'project_id': getattr(project, 'pk', None)})


_COMMIT_TYPES = (
    ('fix', ('fix', 'bug', 'error', 'crash', 'regression', 'broken', 'repair')),
    ('feat', ('add', 'new', 'feature', 'support', 'introduce', 'implement')),
    ('refactor', ('refactor', 'rename', 'restructure', 'simplify', 'extract', 'tidy')),
    ('perf', ('performance', 'faster', 'slow', 'optimise', 'optimize', 'cache')),
    ('test', ('test', 'coverage', 'spec', 'fixture')),
    ('docs', ('document', 'documentation', 'readme', 'comment', 'docstring')),
    ('build', ('dependency', 'upgrade', 'version', 'package', 'requirements')),
    ('style', ('format', 'whitespace', 'lint', 'indentation')),
)


@tool(name='dev.generate_commit_message',
      title='Suggest a commit message',
      description=('Turn a description of a change into a commit message. Returns a '
                   'subject line under 72 characters and a body saying what changed '
                   'and why. This is a suggestion for a commit a person will make; '
                   'nothing is committed.'),
      group='Documentation', agent_types=('developer',), icon='fa-code-commit',
      parameters=_obj(
          required=('change_description',),
          change_description=_s('What the change does.'),
          work_item_id=_i('The work item this change belongs to, if there is one.'),
          style=_enum('Which convention to follow.', ('conventional', 'plain')),
          scope=_s('The area of the code, for a conventional-commit scope.'),
      ))
def generate_commit_message(ctx, change_description, work_item_id=None,
                            style='conventional', scope=''):
    """Suggest a commit message. Suggests only: this employee cannot commit."""
    item = _find_item(work_item_id) if work_item_id else None
    if work_item_id and item is None:
        return _no_item(work_item_id)

    text = change_description.strip()
    lowered = text.lower()
    commit_type = 'chore'
    for candidate, markers in _COMMIT_TYPES:
        if any(marker in lowered for marker in markers):
            commit_type = candidate
            break

    scope = (scope or (item.project.key.lower() if item is not None
                       and item.project.key else '')).strip()

    summary = _first_sentence(text, 100)
    summary = summary.rstrip('.')
    # Conventional commits want the imperative mood and no capital.
    summary = summary[0].lower() + summary[1:] if summary else 'update'

    if style == 'conventional':
        prefix = f'{commit_type}({scope})' if scope else commit_type
        subject = f'{prefix}: {summary}'
    else:
        subject = summary[0].upper() + summary[1:] if summary else 'Update'

    if len(subject) > 72:
        subject = subject[:69].rstrip() + '...'

    body = [subject, '']
    rest = text[len(_first_sentence(text, 100)):].strip()
    body.append('What changed')
    body.append(_first_sentence(text, 200) if not rest else rest[:400])
    body.append('')
    body.append('Why')
    body.append('TO WRITE -- the reason this change was worth making. A reader in six '
                'months has the diff already; what they lack is the reason.')
    if item is not None:
        body.extend(['', f'Refs: {item.reference}'])
        if item.external_reference:
            body.append(f'Tracker: {item.external_reference}')

    artifact = _save_artifact(
        ctx, kind='commit', title=f'Commit message: {subject[:80]}',
        content='\n'.join(body), language='text',
        explanation=(
            f'Classified as a {commit_type} change from the words in the description. '
            f'The subject line is {len(subject)} characters, which fits the 72 that '
            f'git log renders without wrapping. The "Why" is left for a person '
            f'because it is the one part that cannot be derived from the diff.'),
        work_item=item,
        metadata={'commit_type': commit_type, 'style': style, 'scope': scope,
                  'subject_length': len(subject)})

    return _artifact_result(
        artifact,
        f'Suggested a {commit_type} commit message'
        + (f' for {item.reference}.' if item is not None else '.'),
        data={'commit_type': commit_type, 'subject': subject,
              'subject_length': len(subject)})


@tool(name='dev.generate_pull_request_description',
      title='Draft a pull request description',
      description=('Draft a pull request description: what it does, why, how to test '
                   'it, and what the risks are. Reads the work item for its title, '
                   'acceptance criteria and tracker reference when one is given.'),
      group='Documentation', agent_types=('developer',), icon='fa-code-pull-request',
      parameters=_obj(
          work_item_id=_i('The work item this pull request implements.'),
          branch=_s('The branch name.'),
          change_description=_s('What the change does, when no work item is given.'),
      ))
def generate_pull_request_description(ctx, work_item_id=None, branch='',
                                      change_description=''):
    """Draft a pull request description grounded in the work item where there is one."""
    item = _find_item(work_item_id) if work_item_id else None
    if work_item_id and item is None:
        return _no_item(work_item_id)

    if item is None and not change_description.strip():
        return _needs(
            'A pull request description needs something to describe.',
            'Give a work_item_id, or a change_description saying what the '
            'change does.')

    title = item.title if item is not None else _first_sentence(change_description, 90)
    detail = (item.description or change_description or '').strip()

    lines = [f'## {title}', '']
    if item is not None:
        lines.append(f'Implements {item.reference}'
                     + (f' ({item.external_reference})' if item.external_reference
                        else '') + '.')
        lines.append('')

    lines.extend(['### What this does', ''])
    lines.append(_first_sentence(detail, 400) if detail
                 else 'TO WRITE -- one paragraph on the change.')
    lines.extend(['', '### Why', ''])
    lines.append('TO WRITE -- the problem this solves. Not the same as what it does.')

    lines.extend(['', '### How to test it', ''])
    criteria = list(item.acceptance_criteria or []) if item is not None else []
    if criteria:
        for entry in criteria:
            label = entry.get('text') if isinstance(entry, dict) else entry
            lines.append(f'- [ ] {label}')
        lines.append('')
        lines.append('Each box is an acceptance criterion from '
                     f'{item.reference}, so a reviewer can check the change against '
                     'what was actually asked for.')
    else:
        lines.extend([
            '- [ ] TO WRITE -- the ordinary path, with the exact steps.',
            '- [ ] TO WRITE -- one boundary case.',
            '- [ ] TO WRITE -- one failure case, and what the user sees.',
        ])

    lines.extend(['', '### Risks', ''])
    risks = _pr_risks(detail, item)
    lines.extend(f'- {risk}' for risk in risks)

    if branch.strip():
        lines.extend(['', f'Branch: `{branch.strip()}`'])

    artifact = _save_artifact(
        ctx, kind='pr', title=f'Pull request: {title[:110]}',
        content='\n'.join(lines), language='markdown',
        explanation=(
            'The risks section is filled from what the description mentions -- a '
            'migration, an API change, a credential, a deletion -- because those are '
            'the four things a reviewer most often discovers too late. Where the '
            'work item carried acceptance criteria they become the test checklist, '
            'so the review is against what was asked rather than against the diff.'),
        work_item=item,
        metadata={'branch': branch, 'criteria': len(criteria), 'risks': risks})

    return _artifact_result(
        artifact, f'Drafted a pull request description for "{title[:80]}".',
        extra=[f'{len(criteria)} acceptance criteria became the test checklist.'
               if criteria else
               'No acceptance criteria were recorded, so the test checklist is '
               'left for a person.'],
        data={'risks': risks, 'criteria_count': len(criteria)})


_RISK_MARKERS = (
    ('migration', 'Contains a schema change. A migration is hard to reverse once '
                  'data has been written under it, so check the reverse path.'),
    ('delete', 'Deletes data or rows. Confirm what is unrecoverable afterwards.'),
    ('drop', 'Drops a column or table. Confirm nothing still reads it.'),
    ('api', 'Changes an interface other code calls. Check every caller.'),
    ('endpoint', 'Changes an endpoint. Check the clients that call it.'),
    ('permission', 'Touches permissions. Confirm the least-privileged role still '
                   'sees only what it should.'),
    ('auth', 'Touches authentication. A mistake here is a security defect, not a bug.'),
    ('token', 'Handles a credential. Confirm it is never logged or rendered.'),
    ('secret', 'Handles a credential. Confirm it is never logged or rendered.'),
    ('cache', 'Changes caching. Check the behaviour on a cold cache and a stale one.'),
    ('async', 'Introduces concurrency. Check the interleaving, not just the happy path.'),
    ('email', 'Sends mail. Confirm it cannot send to a real address from a test.'),
)


def _pr_risks(detail, item):
    """The risks a reviewer should look at, derived from what the change mentions."""
    lowered = (detail or '').lower()
    found = [note for marker, note in _RISK_MARKERS if marker in lowered]

    if item is not None:
        blockers = list(item.depends_on.all())
        if blockers:
            found.append(
                'Depends on ' + ', '.join(b.reference for b in blockers[:4])
                + '. Confirm those landed first.')
        if item.item_type == 'bug':
            found.append('Fixes a bug, so it needs a test that fails without the '
                         'change. Without one the bug can return unnoticed.')

    if not found:
        found.append('TO WRITE -- name the one thing most likely to be wrong here. '
                     'A review that starts from the author\'s own doubt is far more '
                     'useful than one that starts from nothing.')
    return found


def _sentences(text, limit=8):
    """Split prose into sentences, for filling a document's sections."""
    if not text or not text.strip():
        return []
    parts = re.split(r'(?<=[.!?])\s+', text.strip())
    return [part.strip() for part in parts if part.strip()][:limit]


# ===========================================================================
# ISSUE TRACKING AND READING
#
# Reads act at once. Nothing changes outside the platform when this employee
# looks at a repository, a file or its own work queue.
# ===========================================================================

@tool(name='dev.list_my_work_items',
      title='List development work',
      description=('List the work items a developer has, optionally narrowed by '
                   'project, status or assignee name. Use this before quoting a '
                   'reference: work item ids and references must be looked up, '
                   'never guessed.'),
      group='Issue Tracking', agent_types=('developer',), icon='fa-list-check',
      reads_only=True,
      parameters=_obj(
          assignee_name=_s('The developer whose work to list.'),
          project_id=_i('Narrow to one project.'),
          status=_enum('Narrow to one status.',
                       ('backlog', 'todo', 'in_progress', 'in_review', 'blocked',
                        'done', 'cancelled')),
          limit=_i('How many to return. Default 20.'),
      ))
def list_my_work_items(ctx, assignee_name='', project_id=None, status='', limit=20):
    """List work items, so a reference is read rather than invented."""
    query = WorkItem.objects.select_related('project', 'sprint', 'assignee')
    if project_id:
        project = _find_project(project_id)
        if project is None:
            return _needs(f'There is no project with id {project_id}.',
                          'Call eng.list_projects, or omit project_id.')
        query = query.filter(project=project)
    if status:
        query = query.filter(status=status)
    if assignee_name.strip():
        name = assignee_name.strip()
        query = query.filter(
            Q(assignee__full_name__icontains=name)
            | Q(assignee_name__icontains=name))

    rows = list(query[:max(1, _as_int(limit) or 20)])
    if not rows:
        return ToolResult(
            ok=True,
            text=('No work items match that. The Engineering Delivery employee '
                  'creates them from a requirement; ask it to break the work down '
                  'first.'),
            data={'items': []})

    lines = [f'{len(rows)} work item{"s" if len(rows) != 1 else ""}:']
    for item in rows:
        flags = []
        if item.is_overdue:
            flags.append(f'OVERDUE by {item.days_overdue} days')
        if item.status == 'blocked':
            flags.append(f'blocked: {item.blocked_reason or "reason not recorded"}')
        if item.external_reference:
            flags.append(item.external_reference)
        lines.append(
            f'  {item.reference} [{item.get_status_display()}] {item.title} '
            f'-- {item.get_priority_display()} priority, {item.assignee_label}'
            + (f' ({"; ".join(flags)})' if flags else ''))

    return ToolResult(
        ok=True, text='\n'.join(lines),
        data={'items': [{'id': i.pk, 'reference': i.reference, 'title': i.title,
                         'status': i.status, 'priority': i.priority,
                         'assignee': i.assignee_label,
                         'overdue': i.is_overdue,
                         'external_reference': i.external_reference}
                        for i in rows]})


def default_repository():
    """The repository configured on the GitHub integration, or ''.

    WHY A TOOL NEEDS THIS

    A connector already falls back to its configured default when a tool
    passes an empty repository, so execution worked. What did not work was
    everything a person sees before execution: a read tool refused with
    "Which repository?" while a perfectly good default sat in the settings,
    and an issue proposal showed a reviewer a blank repository field, so the
    one question they most need answered -- where is this going -- had no
    answer on the approval card.

    So the default is resolved here, in the tool, rather than being left to
    the connector.
    """
    try:
        from ..integrations import get_integration
    except Exception:  # noqa: BLE001 -- a missing registry is not a failed tool
        return ''
    row = get_integration('github')
    if row is None:
        return ''
    return str((row.config or {}).get('default_repository') or '').strip()


def resolve_repository(repo='', work_item=None):
    """Which repository a GitHub call should use, in order of specificity.

    An explicit argument wins, then the project the work item belongs to,
    then the integration's own default.
    """
    named = str(repo or '').strip()
    if named:
        return named
    project = getattr(work_item, 'project', None)
    if project is not None and getattr(project, 'repository', ''):
        return str(project.repository).strip()
    return default_repository()


@tool(name='dev.read_repository_file',
      title='Read a file from a repository',
      description=('Read one file out of a GitHub repository, so code can be '
                   'explained or reviewed against what is actually there rather '
                   'than from memory. Reading changes nothing, so this happens at '
                   'once.'),
      group='Issue Tracking', agent_types=('developer',), integration='github',
      icon='fa-file-code', reads_only=True,
      parameters=_obj(
          required=('path',),
          repo=_s('The repository as owner/name. Uses the configured default when empty.'),
          path=_s('The path within the repository, e.g. "marketing/models.py".'),
          ref=_s('A branch, tag or commit. Defaults to the default branch.'),
      ))
def read_repository_file(ctx, path, repo='', ref=''):
    """Read a repository file through the GitHub integration."""
    result = _call('github', 'read_file', repo=repo, path=path, ref=ref)
    if not result.ok:
        return ToolResult(ok=False, error=result.error,
                          text=f'Could not read {path}: {result.error}')

    content = _first(result.data, 'content', default='')
    where = _first(result.data, 'repo', 'repository', default=repo or 'the repository')
    header = (f'{path} from {where}'
              + (f' at {ref}' if ref else '')
              + (' (simulated -- GitHub is in demo mode)' if result.demo else ''))
    body = content if content else '(no readable text content)'
    return ToolResult(
        ok=True, demo=result.demo,
        text=f'{header}\n\n{body}',
        data={'path': path, 'repository': where, 'ref': ref,
              'characters': len(content), 'simulated': result.demo})


@tool(name='dev.search_repository',
      title='Search a repository',
      description=('Search a GitHub repository for code matching a query, to find '
                   'where something is defined or used before changing anything '
                   'about it.'),
      group='Issue Tracking', agent_types=('developer',), integration='github',
      icon='fa-magnifying-glass', reads_only=True,
      parameters=_obj(
          required=('query',),
          query=_s('What to search for.'),
          repo=_s('The repository as owner/name.'),
          limit=_i('How many results. Default 20.'),
      ))
def search_repository(ctx, query, repo='', limit=20):
    """Search repository code through the GitHub integration."""
    result = _call('github', 'search_code', repo=repo, query=query,
                   limit=max(1, _as_int(limit) or 20))
    if not result.ok:
        return ToolResult(ok=False, error=result.error,
                          text=f'The search failed: {result.error}')

    matches = _first(result.data, 'results', 'matches', 'items', default=[]) or []
    if not matches:
        return ToolResult(
            ok=True, demo=result.demo,
            text=(f'Nothing in {repo or "the repository"} matched "{query}". '
                  f'That is a result, not a failure: the thing may be named '
                  f'differently, or may not exist.'),
            data={'matches': [], 'simulated': result.demo})

    lines = [f'{len(matches)} match{"es" if len(matches) != 1 else ""} for "{query}"'
             + (' (simulated)' if result.demo else '') + ':']
    for match in matches[:20]:
        if isinstance(match, dict):
            lines.append(f'  {match.get("path", match.get("name", "?"))}'
                         + (f' -- {match.get("snippet", "")[:110]}'
                            if match.get('snippet') else ''))
        else:
            lines.append(f'  {match}')
    return ToolResult(ok=True, demo=result.demo, text='\n'.join(lines),
                      data={'matches': matches[:20], 'simulated': result.demo})


@tool(name='dev.read_technical_document',
      title='Read a technical document',
      description=('Read a specification or technical document out of Google Drive '
                   'by its file id, so an implementation follows what was specified '
                   'rather than what was remembered.'),
      group='Issue Tracking', agent_types=('developer',), integration='google_drive',
      icon='fa-file-lines', reads_only=True,
      parameters=_obj(
          required=('file_id',),
          file_id=_s('The Drive file id.'),
      ))
def read_technical_document(ctx, file_id):
    """Read a Drive document through the integration layer."""
    result = _call('google_drive', 'read_file', file_id=file_id)
    if not result.ok:
        return ToolResult(ok=False, error=result.error,
                          text=f'Could not read that document: {result.error}')

    name = _first(result.data, 'name', 'title', default=file_id)
    content = _first(result.data, 'content', default='')
    if not content:
        note = _first(result.data, 'note', 'message',
                      default='The file has no readable text content.')
        return ToolResult(
            ok=True, demo=result.demo,
            text=f'{name}: {note}',
            data={'file_id': file_id, 'name': name, 'readable': False,
                  'simulated': result.demo})

    return ToolResult(
        ok=True, demo=result.demo,
        text=(f'{name}'
              + (' (simulated -- Google Drive is in demo mode)' if result.demo else '')
              + f'\n\n{content[:6000]}'),
        data={'file_id': file_id, 'name': name, 'characters': len(content),
              'readable': True, 'simulated': result.demo})


@tool(name='dev.analyse_pull_request',
      title='Analyse a pull request',
      description=('Read a pull request from GitHub and report its size, the files it '
                   'touches, where the risk is, and what a reviewer should look at '
                   'first. Records a code review. Reading a pull request changes '
                   'nothing, so this happens at once; posting the review would be a '
                   'separate approved action.'),
      group='Code Review', agent_types=('developer',), integration='github',
      icon='fa-code-pull-request', reads_only=True,
      parameters=_obj(
          required=('number',),
          number=_s('The pull request number.'),
          repo=_s('The repository as owner/name.'),
      ))
def analyse_pull_request(ctx, number, repo=''):
    """Read a pull request and say where a reviewer's attention should go."""
    detail = _call('github', 'get_pull_request', repo=repo, number=_as_int(number))
    if not detail.ok:
        return ToolResult(
            ok=False, error=detail.error,
            text=f'Could not read pull request {number}: {detail.error}')

    raw = detail.data or {}
    if raw.get('found') is False:
        return ToolResult(ok=True, demo=detail.demo,
                          text=detail.summary or f'Pull request {number} was not found.',
                          data=raw)

    # get_pull_request nests the actual row under 'pull_request' -- the top
    # level only carries repository and demo bookkeeping.
    data = raw.get('pull_request') if isinstance(raw.get('pull_request'), dict) else raw
    title = _first(data, 'title', default=f'Pull request {number}')
    changed = _as_int(_first(data, 'changed_files', 'files_changed', default=0)) or 0
    additions = _as_int(_first(data, 'additions', default=0)) or 0
    deletions = _as_int(_first(data, 'deletions', default=0)) or 0
    mergeable = _first(data, 'mergeable_state', 'mergeable', default='unknown')
    head = _first(data, 'head_branch', 'head', 'head_ref', default='')
    base = _first(data, 'base_branch', 'base', 'base_ref', default='')

    files_result = _call('github', 'list_pull_request_files', repo=repo,
                         number=_as_int(number), limit=100)
    files = (_first(files_result.data, 'files', 'items', default=[]) or []
             if files_result.ok else [])

    findings = []
    if changed >= 20 or (additions + deletions) >= 600:
        findings.append(_finding(
            'high', 0,
            f'This pull request touches {changed} files and {additions + deletions} '
            f'lines. A review of that size does not find defects; it finds typos. '
            f'Reviewers approve large changes at roughly the rate they approve '
            f'small ones, which is why size is itself the risk.',
            'Split it along the seams: one change per outcome.', ''))
    elif changed >= 10:
        findings.append(_finding(
            'medium', 0,
            f'{changed} files and {additions + deletions} lines is at the upper edge '
            f'of what one sitting reviews well.',
            'Review it in two passes: structure first, then line by line.', ''))

    if deletions > additions * 3 and deletions > 100:
        findings.append(_finding(
            'medium', 0,
            f'{deletions} lines removed against {additions} added. A large deletion '
            f'is usually either a real simplification or a lost behaviour, and the '
            f'diff alone does not distinguish them.',
            'Confirm a test covers each behaviour the removed code provided.', ''))

    risky = []
    for entry in files:
        path = (entry.get('filename') or entry.get('path') or ''
                if isinstance(entry, dict) else str(entry))
        lowered = path.lower()
        for marker, note in _RISK_MARKERS:
            if marker in lowered:
                risky.append((path, note))
                break
        if '/migrations/' in lowered or lowered.endswith('settings.py'):
            risky.append((path, 'Changes how the whole application is configured or '
                                'shaped. Check it before the rest of the diff.'))

    for path, note in risky[:8]:
        findings.append(_finding('high', 0, f'{path}: {note}',
                                 'Read this file first.', path))

    if str(mergeable).lower() in ('dirty', 'blocked', 'unstable', 'false'):
        findings.append(_finding(
            'medium', 0,
            f'GitHub reports the merge state as "{mergeable}", so something is '
            f'failing or conflicting.',
            'Resolve that before spending a reviewer\'s time on the content.', ''))

    verdict = ('request_changes'
               if any(f['severity'] == 'high' for f in findings)
               else ('comment' if findings else 'approve'))

    review = CodeReview.objects.create(
        repository=_first(raw, 'repo', 'repository', default=repo),
        pull_request_number=str(number),
        title=f'Review of #{number}: {title}'[:300],
        summary=(f'{changed} files changed, +{additions}/-{deletions}. '
                 f'{head} into {base}. '
                 f'{len(findings)} thing{"s" if len(findings) != 1 else ""} for a '
                 f'reviewer to look at.'),
        findings=findings, verdict=verdict, created_by_agent=_agent_of(ctx))

    lines = [f'Pull request #{number}: {title}'
             + (' (simulated -- GitHub is in demo mode)' if detail.demo else ''),
             f'  {changed} files changed, +{additions} / -{deletions}. '
             f'{head} into {base}. Merge state: {mergeable}.',
             f'  Verdict: {review.get_verdict_display()}.']
    if findings:
        lines.append('  Look at these first:')
        for finding in findings:
            lines.append(f'    [{finding["severity"]}] {finding["note"]}')
    else:
        lines.append('  Nothing stands out from the shape of the change. That is a '
                     'real outcome, not an empty one -- it still needs reading.')
    lines.append(f'  Recorded as code review #{review.pk}.')

    return ToolResult(
        ok=True, demo=detail.demo, text='\n'.join(lines),
        data={'review_id': review.pk, 'verdict': verdict, 'changed_files': changed,
              'additions': additions, 'deletions': deletions,
              'findings': findings, 'simulated': detail.demo},
        subject_label='marketing.codereview', subject_id=review.pk)


# ===========================================================================
# THE APPROVAL-GATED TOOLS
#
# Each one prepares an action and stops. The executor below it is the only
# code that reaches the service, and marketing/approvals.py is the only caller
# of an executor.
# ===========================================================================

def _issue_body(body, artifact_id=None, item=None):
    """Assemble an issue body, with the provenance a reader will want."""
    parts = [body.strip()] if body and body.strip() else []
    if item is not None:
        parts.append(f'Work item: {item.reference} ({item.get_status_display()}, '
                     f'{item.get_priority_display()} priority).')
    if artifact_id:
        parts.append(f'Generated artefact #{artifact_id} in AI Workforce holds the '
                     f'suggested implementation. It has not been applied.')
    parts.append('Filed from AI Workforce by the Software Development employee, '
                 'after human approval.')
    return '\n\n'.join(parts)


@tool(name='dev.create_github_issue',
      title='File a GitHub issue',
      description=('Prepare a GitHub issue for approval. Use it to record a bug found '
                   'while debugging, or a piece of work that came out of a review. '
                   'The issue is not filed until somebody approves it.'),
      group='Issue Tracking', agent_types=('developer',), integration='github',
      requires_approval=True, risk='medium', icon='fa-github',
      parameters=_obj(
          required=('title',),
          title=_s('The issue title. One line, specific.'),
          body=_s('The issue body: what happens, what should happen, how to reproduce.'),
          repo=_s('The repository as owner/name. Uses the default when empty.'),
          labels=_a('Labels to apply.'),
          assignees=_a('GitHub usernames to assign.'),
          work_item_id=_i('The work item this issue tracks, if there is one.'),
          artifact_id=_i('A generated artefact to reference from the issue.'),
      ))
def create_github_issue(ctx, title, body='', repo='', labels=None, assignees=None,
                        work_item_id=None, artifact_id=None):
    """Prepare a GitHub issue. Filing it needs a person."""
    item = _find_item(work_item_id) if work_item_id else None
    if work_item_id and item is None:
        return _no_item(work_item_id)

    target = resolve_repository(repo, item)
    full_body = _issue_body(body, artifact_id, item)

    return Proposal(
        title=f'GitHub issue: {title[:120]}',
        summary=(f'Would file an issue in {target or "the default repository"}. '
                 f'{len(_as_list(labels))} label(s), '
                 f'{len(_as_list(assignees))} assignee(s).'),
        payload={'repo': target, 'title': title, 'body': full_body,
                 'labels': _as_list(labels), 'assignees': _as_list(assignees),
                 'work_item_id': getattr(item, 'pk', None)},
        editable_fields=[
            editable('title', 'Issue title'),
            editable('body', 'Issue body', 'longtext', rows=12),
            editable('repo', 'Repository'),
        ],
        risk='medium', integration_key='github',
        subject_label='marketing.workitem', subject_id=getattr(item, 'pk', 0) or 0,
        subject_display=item.reference if item is not None else '')


@executor('dev.create_github_issue')
def _execute_create_github_issue(action):
    payload = action.payload or {}
    result = _call('github', 'create_issue',
                   repo=payload.get('repo', ''), title=payload.get('title', ''),
                   body=payload.get('body', ''),
                   labels=payload.get('labels') or None,
                   assignees=payload.get('assignees') or None)
    if not result.ok:
        return ToolResult(ok=False, error=result.error,
                          text=f'The issue was not filed: {result.error}')

    number = _first(result.data, 'number', default='')
    url = _first(result.data, 'url', 'html_url', default='')
    issue = ExternalIssue.objects.create(
        system='github', container=payload.get('repo', ''),
        reference=f'#{number}' if number else '',
        title=payload.get('title', '')[:300], body=payload.get('body', ''),
        issue_status='simulated' if result.demo else 'open',
        labels=payload.get('labels') or [],
        assignee=', '.join(payload.get('assignees') or [])[:120],
        url=url, external_id=str(number), agent=action.agent, action=action,
        subject_label=action.subject_label, subject_id=action.subject_id)

    item = _find_item(payload.get('work_item_id'))
    if item is not None and number:
        item.external_reference = f'#{number}'
        item.external_url = url
        item.save(update_fields=['external_reference', 'external_url'])

    return ToolResult(
        ok=True, demo=result.demo,
        text=(result.summary or f'Filed issue #{number}.')
             + f' Recorded as external issue #{issue.pk}.',
        data={'issue_id': issue.pk, 'number': number, 'url': url,
              'simulated': result.demo})


@tool(name='dev.update_github_issue',
      title='Update a GitHub issue',
      description=('Prepare a change to an existing GitHub issue -- its title, body '
                   'or open/closed state -- for approval.'),
      group='Issue Tracking', agent_types=('developer',), integration='github',
      requires_approval=True, risk='medium', icon='fa-pen-to-square',
      parameters=_obj(
          required=('number',),
          number=_s('The issue number.'),
          repo=_s('The repository as owner/name.'),
          title=_s('A replacement title. Leave empty to keep it.'),
          body=_s('A replacement body. Leave empty to keep it.'),
          state=_enum('Open or close the issue.', ('open', 'closed')),
      ))
def update_github_issue(ctx, number, repo='', title='', body='', state=''):
    """Prepare an issue update. Applying it needs a person."""
    changes = {k: v for k, v in (('title', title), ('body', body), ('state', state))
               if v and str(v).strip()}
    if not changes:
        return _needs('Nothing was given to change.',
                      'Supply a title, a body or a state.')

    return Proposal(
        title=f'Update GitHub issue #{number}',
        summary=f'Would change {", ".join(sorted(changes))} on issue #{number} '
                f'in {repo or "the default repository"}.',
        payload={'repo': repo, 'number': str(number), **changes},
        editable_fields=[
            editable('title', 'Title'),
            editable('body', 'Body', 'longtext', rows=10),
            editable('state', 'State'),
        ],
        risk='medium', integration_key='github')


@executor('dev.update_github_issue')
def _execute_update_github_issue(action):
    payload = dict(action.payload or {})
    number = payload.pop('number', '')
    repo = payload.pop('repo', '')
    result = _call('github', 'update_issue', repo=repo, number=_as_int(number),
                   **{k: v for k, v in payload.items()
                      if k in ('title', 'body', 'state')})
    if not result.ok:
        return ToolResult(ok=False, error=result.error,
                          text=f'The issue was not updated: {result.error}')
    return ToolResult(ok=True, demo=result.demo,
                      text=result.summary or f'Updated issue #{number}.',
                      data=dict(result.data or {}, simulated=result.demo))


@tool(name='dev.comment_on_github_issue',
      title='Comment on a GitHub issue',
      description=('Prepare a comment on a GitHub issue or pull request for approval. '
                   'Use it to post a review finding or a debugging conclusion where '
                   'the work is tracked.'),
      group='Issue Tracking', agent_types=('developer',), integration='github',
      requires_approval=True, risk='medium', icon='fa-comment',
      parameters=_obj(
          required=('number', 'body'),
          number=_s('The issue or pull request number.'),
          body=_s('The comment text.'),
          repo=_s('The repository as owner/name.'),
      ))
def comment_on_github_issue(ctx, number, body, repo=''):
    """Prepare a comment. Posting it needs a person."""
    return Proposal(
        title=f'Comment on GitHub #{number}',
        summary=f'Would post {len(body)} characters to #{number} in '
                f'{repo or "the default repository"}.',
        payload={'repo': repo, 'number': str(number), 'body': body},
        editable_fields=[editable('body', 'Comment', 'longtext', rows=10)],
        risk='medium', integration_key='github')


@executor('dev.comment_on_github_issue')
def _execute_comment_on_github_issue(action):
    payload = action.payload or {}
    result = _call('github', 'comment_issue', repo=payload.get('repo', ''),
                   number=_as_int(payload.get('number')),
                   body=payload.get('body', ''))
    if not result.ok:
        return ToolResult(ok=False, error=result.error,
                          text=f'The comment was not posted: {result.error}')
    return ToolResult(ok=True, demo=result.demo,
                      text=result.summary or 'Comment posted.',
                      data=dict(result.data or {}, simulated=result.demo))


@tool(name='dev.create_jira_issue',
      title='Create a Jira issue',
      description=('Prepare a Jira issue for approval, for a bug or a piece of work '
                   'that should be tracked where the team tracks everything else.'),
      group='Issue Tracking', agent_types=('developer',), integration='jira',
      requires_approval=True, risk='medium', icon='fa-jira',
      parameters=_obj(
          required=('summary',),
          summary=_s('The issue summary. One line.'),
          description=_s('The description: what happens, what should happen.'),
          project_key=_s('The Jira project key, e.g. ENG. Uses the default when empty.'),
          issue_type=_enum('The issue type.', ('Task', 'Bug', 'Story', 'Sub-task')),
          priority=_s('The priority name, if the project uses them.'),
          work_item_id=_i('The work item this tracks.'),
      ))
def create_jira_issue(ctx, summary, description='', project_key='', issue_type='Task',
                      priority='', work_item_id=None):
    """Prepare a Jira issue. Creating it needs a person."""
    item = _find_item(work_item_id) if work_item_id else None
    if work_item_id and item is None:
        return _no_item(work_item_id)

    target = project_key.strip() or (item.project.jira_project_key
                                     if item is not None else '')
    return Proposal(
        title=f'Jira issue: {summary[:120]}',
        summary=f'Would create a {issue_type} in '
                f'{target or "the default Jira project"}.',
        payload={'project': target, 'summary': summary,
                 'description': _issue_body(description, None, item),
                 'issue_type': issue_type, 'priority': priority,
                 'work_item_id': getattr(item, 'pk', None)},
        editable_fields=[
            editable('summary', 'Summary'),
            editable('description', 'Description', 'longtext', rows=12),
            editable('project', 'Project key'),
            editable('issue_type', 'Issue type'),
            editable('priority', 'Priority'),
        ],
        risk='medium', integration_key='jira',
        subject_label='marketing.workitem', subject_id=getattr(item, 'pk', 0) or 0,
        subject_display=item.reference if item is not None else '')


@executor('dev.create_jira_issue')
def _execute_create_jira_issue(action):
    payload = action.payload or {}
    result = _call('jira', 'create_issue',
                   project=payload.get('project', ''),
                   summary=payload.get('summary', ''),
                   description=payload.get('description', ''),
                   issue_type=payload.get('issue_type', 'Task'),
                   priority=payload.get('priority', ''))
    if not result.ok:
        return ToolResult(ok=False, error=result.error,
                          text=f'The Jira issue was not created: {result.error}')

    key = _first(result.data, 'key', default='')
    url = _first(result.data, 'url', default='')
    issue = ExternalIssue.objects.create(
        system='jira', container=payload.get('project', ''), reference=key,
        title=payload.get('summary', '')[:300],
        body=payload.get('description', ''),
        issue_status='simulated' if result.demo else 'open',
        priority=payload.get('priority', '')[:30], url=url, external_id=key,
        agent=action.agent, action=action,
        subject_label=action.subject_label, subject_id=action.subject_id)

    item = _find_item(payload.get('work_item_id'))
    if item is not None and key:
        item.external_reference = key
        item.external_url = url
        item.save(update_fields=['external_reference', 'external_url'])

    return ToolResult(
        ok=True, demo=result.demo,
        text=(result.summary or f'Created {key}.')
             + f' Recorded as external issue #{issue.pk}.',
        data={'issue_id': issue.pk, 'key': key, 'url': url,
              'simulated': result.demo})


@tool(name='dev.update_jira_status',
      title='Move a Jira issue',
      description=('Prepare a Jira status transition for approval, for example moving '
                   'an issue to In Review once the work is ready. Optionally adds a '
                   'comment in the same action.'),
      group='Issue Tracking', agent_types=('developer',), integration='jira',
      requires_approval=True, risk='medium', icon='fa-arrow-right-arrow-left',
      parameters=_obj(
          required=('issue_key', 'status'),
          issue_key=_s('The issue key, e.g. ENG-118.'),
          status=_s('The status to move it to, e.g. "In Review".'),
          comment=_s('A comment to add with the transition.'),
      ))
def update_jira_status(ctx, issue_key, status, comment=''):
    """Prepare a Jira transition. Applying it needs a person."""
    return Proposal(
        title=f'Move {issue_key} to {status}',
        summary=f'Would transition {issue_key} to "{status}"'
                + (' and add a comment.' if comment.strip() else '.'),
        payload={'key': issue_key, 'status': status, 'comment': comment},
        editable_fields=[
            editable('status', 'Target status'),
            editable('comment', 'Comment', 'longtext', rows=6),
        ],
        risk='medium', integration_key='jira')


@executor('dev.update_jira_status')
def _execute_update_jira_status(action):
    payload = action.payload or {}
    key = payload.get('key', '')
    moved = _call('jira', 'transition_issue', key=key, status=payload.get('status', ''))
    if not moved.ok:
        return ToolResult(ok=False, error=moved.error,
                          text=f'{key} was not moved: {moved.error}')

    notes = []
    if (payload.get('comment') or '').strip():
        commented = _call('jira', 'comment_issue', key=key,
                          body=payload['comment'])
        notes.append('Comment added.' if commented.ok
                     else f'The transition worked but the comment failed: '
                          f'{commented.error}')

    return ToolResult(
        ok=True, demo=moved.demo,
        text=' '.join([moved.summary or f'Moved {key}.'] + notes),
        data=dict(moved.data or {}, simulated=moved.demo))


@tool(name='dev.notify_developers',
      title='Notify the developers',
      description=('Prepare a Slack message to the engineering channel for approval. '
                   'Keep it short and specific: what happened, what it affects, what '
                   'somebody should do.'),
      group='Issue Tracking', agent_types=('developer',), integration='slack',
      requires_approval=True, risk='high', icon='fa-slack',
      parameters=_obj(
          required=('message',),
          message=_s('The message text.'),
          channel=_s('The channel. Uses the configured engineering channel when empty.'),
          work_item_id=_i('A work item to reference.'),
      ))
def notify_developers(ctx, message, channel='', work_item_id=None):
    """Prepare a Slack message to the developers. Sending it needs a person."""
    item = _find_item(work_item_id) if work_item_id else None
    if work_item_id and item is None:
        return _no_item(work_item_id)

    text = message.strip()
    if item is not None:
        text = f'{text}\n\n{item.reference}: {item.title}'
        if item.external_url:
            text = f'{text}\n{item.external_url}'

    return Proposal(
        title=f'Slack the developers: {_first_sentence(message, 80)}',
        summary=f'Would post to {channel or "the engineering channel"}.',
        payload={'channel': channel, 'text': text,
                 'work_item_id': getattr(item, 'pk', None)},
        editable_fields=[
            editable('text', 'Message', 'longtext', rows=8),
            editable('channel', 'Channel'),
        ],
        risk='high', integration_key='slack')


@tool(name='dev.send_bug_alert',
      title='Raise a bug alert',
      description=('Prepare a Slack alert about a bug for approval. State the impact '
                   'first, because an alert that opens with a stack trace makes the '
                   'reader work out whether to care.'),
      group='Issue Tracking', agent_types=('developer',), integration='slack',
      requires_approval=True, risk='high', icon='fa-triangle-exclamation',
      parameters=_obj(
          required=('summary',),
          summary=_s('What is broken, and what it affects.'),
          severity=_enum('How bad it is.', ('low', 'medium', 'high', 'critical')),
          channel=_s('The channel. Uses the configured alerts channel when empty.'),
          work_item_id=_i('The work item tracking it.'),
          detail=_s('The technical detail, after the impact.'),
      ))
def send_bug_alert(ctx, summary, severity='high', channel='', work_item_id=None,
                   detail=''):
    """Prepare a bug alert. Sending it needs a person."""
    item = _find_item(work_item_id) if work_item_id else None
    if work_item_id and item is None:
        return _no_item(work_item_id)

    lines = [f'[{severity.upper()}] {summary.strip()}']
    if detail.strip():
        lines.extend(['', detail.strip()])
    if item is not None:
        lines.extend(['', f'Tracked as {item.reference}'
                      + (f' / {item.external_reference}'
                         if item.external_reference else '')])

    return Proposal(
        title=f'Bug alert ({severity}): {_first_sentence(summary, 80)}',
        summary=f'Would post a {severity} bug alert to '
                f'{channel or "the alerts channel"}.',
        payload={'channel': channel, 'text': '\n'.join(lines),
                 'severity': severity,
                 'work_item_id': getattr(item, 'pk', None)},
        editable_fields=[
            editable('text', 'Alert', 'longtext', rows=8),
            editable('channel', 'Channel'),
            editable('severity', 'Severity'),
        ],
        risk='high', integration_key='slack')


def _execute_slack_post(action, kind):
    """Shared execution for the two Slack tools in this module."""
    payload = action.payload or {}
    result = _call('slack', 'post_message', channel=payload.get('channel', ''),
                   text=payload.get('text', ''))
    if not result.ok:
        return ToolResult(ok=False, error=result.error,
                          text=f'The {kind} was not posted: {result.error}')

    where = _first(result.data, 'channel', default=payload.get('channel')
                   or 'the default channel')
    message = OutboundMessage.objects.create(
        channel='slack', recipient=str(where)[:400],
        subject=action.title[:300], body=payload.get('text', ''),
        status='simulated' if result.demo else 'sent',
        external_id=str(_first(result.data, 'ts', 'message_id', default='')),
        agent=action.agent, action=action, integration=action.integration,
        metadata={'severity': payload.get('severity', ''), 'kind': kind})

    return ToolResult(
        ok=True, demo=result.demo,
        text=(result.summary or f'Posted the {kind} to {where}.')
             + f' Recorded as outbound message #{message.pk}.',
        data={'message_id': message.pk, 'channel': str(where),
              'simulated': result.demo})


@executor('dev.notify_developers')
def _execute_notify_developers(action):
    return _execute_slack_post(action, 'notification')


@executor('dev.send_bug_alert')
def _execute_send_bug_alert(action):
    return _execute_slack_post(action, 'bug alert')


@tool(name='dev.upload_documentation',
      title='File documentation in Drive',
      description=('Prepare an upload of documentation to Google Drive for approval. '
                   'Pass an artefact id to file something already generated, or a '
                   'name and content directly.'),
      group='Documentation', agent_types=('developer',), integration='google_drive',
      requires_approval=True, risk='medium', icon='fa-cloud-arrow-up',
      parameters=_obj(
          name=_s('The file name, including its extension.'),
          content=_s('The file content, when not filing an artefact.'),
          artifact_id=_i('A generated artefact to file.'),
          folder_id=_s('The Drive folder id. Uses the configured default when empty.'),
      ))
def upload_documentation(ctx, name='', content='', artifact_id=None, folder_id=''):
    """Prepare a Drive upload. Uploading needs a person."""
    if artifact_id:
        artifact = CodeArtifact.objects.filter(pk=_as_int(artifact_id)).first()
        if artifact is None:
            return _needs(f'There is no artefact with id {artifact_id}.',
                          'Call dev.list_artifacts to see the ids that exist.')
        name = name.strip() or (artifact.suggested_path.rsplit('/', 1)[-1]
                                or f'{_snake(artifact.title, "document")}.md')
        content = content.strip() or artifact.content
    else:
        artifact = None

    if not name.strip() or not content.strip():
        return _needs('An upload needs a file name and some content.',
                      'Give an artifact_id, or both name and content.')

    return Proposal(
        title=f'Upload {name} to Drive',
        summary=f'Would upload {len(content)} characters as {name} to '
                f'{folder_id or "the default folder"}.',
        payload={'name': name, 'content': content, 'folder_id': folder_id,
                 'artifact_id': getattr(artifact, 'pk', None)},
        editable_fields=[
            editable('name', 'File name'),
            editable('content', 'Content', 'longtext', rows=16),
            editable('folder_id', 'Folder id'),
        ],
        risk='medium', integration_key='google_drive',
        subject_label='marketing.codeartifact',
        subject_id=getattr(artifact, 'pk', 0) or 0,
        subject_display=artifact.title if artifact is not None else '')


@executor('dev.upload_documentation')
def _execute_upload_documentation(action):
    payload = action.payload or {}
    result = _call('google_drive', 'upload_file', name=payload.get('name', ''),
                   content=payload.get('content', ''),
                   folder_id=payload.get('folder_id', ''),
                   mime_type='text/markdown'
                   if payload.get('name', '').endswith('.md') else 'text/plain')
    if not result.ok:
        return ToolResult(ok=False, error=result.error,
                          text=f'The upload failed: {result.error}')

    # No OutboundMessage row: neither that model nor CalendarEvent honestly
    # describes a file upload. The ProposedAction and its audit entry are the
    # record, which is the same choice the marketing module made.
    artifact = CodeArtifact.objects.filter(
        pk=_as_int(payload.get('artifact_id'))).first()
    if artifact is not None:
        artifact.status = 'reviewed'
        artifact.metadata = dict(artifact.metadata or {},
                                 uploaded_to_drive=True,
                                 drive_file_id=str(_first(result.data, 'file_id',
                                                          default='')))
        artifact.save(update_fields=['status', 'metadata'])

    return ToolResult(
        ok=True, demo=result.demo,
        text=result.summary or f'Uploaded {payload.get("name")}.',
        data=dict(result.data or {}, simulated=result.demo))


# ===========================================================================
# READING WHAT IS ALREADY TRACKED
#
# These were missing, and the gap produced a bad answer rather than an error.
# The employee could create, update and comment on a GitHub issue but had no
# way to LIST one. Asked "what are the open issues", it had nothing to call,
# so it reached for the nearest thing -- repository search -- ran it seven
# times with different phrasings, exhausted the round limit, and concluded
# that the repository had no issue tracker. Every step of that was reasonable
# given the tools it held; the fault was the tools it held.
#
# A capability that can write to something must be able to read it back.
# ===========================================================================

@tool(name='dev.list_github_issues',
      title='List GitHub issues',
      description=('List the issues on a GitHub repository, open or closed. Use '
                   'this whenever somebody asks what issues exist, what is open, '
                   'or whether something is already filed -- do NOT search the '
                   'repository code for that, because issues are not in the code. '
                   'Reading changes nothing, so this happens at once.'),
      group='Issue Tracking', agent_types=('developer',), integration='github',
      icon='fa-list-ul', reads_only=True,
      parameters=_obj(
          repo=_s('The repository as owner/name. Uses the configured default '
                  'when empty.'),
          state=_enum('Which issues to return.', ('open', 'closed', 'all')),
          labels=_s('Only issues carrying these labels, comma separated.'),
          limit=_i('How many to return. Default 20.'),
      ))
def list_github_issues(ctx, repo='', state='open', labels='', limit=20):
    """List the issues on a repository, so nothing has to be inferred."""
    target = resolve_repository(repo)
    if not target:
        message = ("Which repository? Pass repo as 'owner/name', or set a default "
                   "repository on the GitHub integration.")
        return ToolResult(ok=False, error=message, text=message)

    result = _call('github', 'list_issues', repo=target,
                   state=state or 'open', limit=max(1, _as_int(limit) or 20),
                   labels=labels or '')
    if not result.ok:
        return ToolResult(
            ok=False, error=result.error,
            text=f'Could not read the issues on {target}: {result.error}')

    issues = _first(result.data, 'issues', 'items', default=[]) or []
    marker = ' (simulated -- GitHub is in demo mode)' if result.demo else ''

    if not issues:
        return ToolResult(
            ok=True, demo=result.demo,
            text=(f'{target} has no {state} issues{marker}. That is the real '
                  f'answer from the issue tracker, not an inference.'),
            data={'repository': target, 'issues': [], 'count': 0,
                  'simulated': result.demo})

    lines = [f'{len(issues)} {state} issue(s) in {target}{marker}:']
    for row in issues[:40]:
        if not isinstance(row, dict):
            lines.append(f'  {row}')
            continue
        tags = ', '.join(str(t) for t in (row.get('labels') or []))
        lines.append(
            f"  #{row.get('number', '?')} {row.get('title', '(no title)')}"
            + (f" [{tags}]" if tags else '')
            + (f" -- {row.get('assignee')}" if row.get('assignee') else ''))

    return ToolResult(ok=True, demo=result.demo, text='\n'.join(lines),
                      data={'repository': target, 'issues': issues[:40],
                            'count': len(issues), 'simulated': result.demo})


@tool(name='dev.get_github_issue',
      title='Read one GitHub issue',
      description=('Read a single GitHub issue in full, including its body and '
                   'state. Use it before commenting on or updating an issue, so '
                   'the comment answers what the issue actually says.'),
      group='Issue Tracking', agent_types=('developer',), integration='github',
      icon='fa-circle-info', reads_only=True,
      parameters=_obj(
          required=('number',),
          number=_s('The issue number.'),
          repo=_s('The repository as owner/name.'),
      ))
def get_github_issue(ctx, number, repo=''):
    """Read one issue, so a reply is grounded in what it says."""
    target = resolve_repository(repo)
    if not target:
        message = "Which repository? Pass repo as 'owner/name'."
        return ToolResult(ok=False, error=message, text=message)

    result = _call('github', 'get_issue', repo=target, number=_as_int(number))
    if not result.ok:
        return ToolResult(ok=False, error=result.error,
                          text=f'Could not read issue #{number}: {result.error}')

    raw = result.data or {}
    if raw.get('found') is False:
        return ToolResult(ok=True, demo=result.demo,
                          text=result.summary or f'Issue #{number} was not found.',
                          data=dict(raw, repository=target, simulated=result.demo))

    # get_issue nests the actual row under 'issue' -- the top level only
    # carries repository and demo bookkeeping.
    data = raw.get('issue') if isinstance(raw.get('issue'), dict) else raw
    assignees = data.get('assignees') or []
    marker = ' (simulated)' if result.demo else ''
    lines = [
        f"#{data.get('number', number)} {data.get('title', '')}{marker}",
        f"  state: {data.get('state', 'unknown')}"
        + (f" | assignee: {', '.join(assignees)}" if assignees else ''),
    ]
    if data.get('labels'):
        lines.append('  labels: ' + ', '.join(str(t) for t in data['labels']))
    if data.get('body'):
        lines.extend(['', str(data['body'])[:3000]])

    return ToolResult(ok=True, demo=result.demo, text='\n'.join(lines),
                      data=dict(data, repository=target, simulated=result.demo))


# ===========================================================================
# REPOSITORY ADMINISTRATION
#
# Creating and updating a repository sit alongside every other write in this
# module: the tool prepares the action, a person approves it, the executor
# performs it.
#
# DELETING ONE DOES NOT SIT ALONGSIDE THEM, and it is worth saying why in the
# code rather than only in a docstring somewhere.
#
# Every other action this platform can take is recoverable. A wrong email can
# be followed by a correction, a wrong issue can be closed, a wrong Jira
# transition can be transitioned back. Deleting a repository destroys work
# that may exist nowhere else, and GitHub's restore window is short and not
# guaranteed. Approval alone is a weak guard against it, because approving is
# one click and the thing most likely to go wrong is a reviewer approving
# quickly without reading which repository is named.
#
# So deletion carries three guards that nothing else here has:
#
#   1. It never falls back to the configured default repository. Every other
#      tool treats an empty repo as "use the default". Here that would be a
#      way to destroy the project's own repository because an argument went
#      missing between the model and the tool.
#   2. The caller must pass `confirm` matching the full owner/name exactly,
#      which is the same shape of guard GitHub's own interface uses.
#   3. It is risk high, so the approval card warns and the chat button asks
#      for confirmation before it will even submit.
#
# None of that makes deletion safe. It makes it deliberate, which is the most
# a tool layer can honestly offer.
# ===========================================================================

@tool(name='dev.create_repository',
      title='Create a GitHub repository',
      description=('Create a new GitHub repository. Prepared for approval; '
                   'nothing is created until somebody approves it. Requires a '
                   'token with Administration write, which a token scoped to a '
                   'single repository does not have.'),
      group='Repository Administration', agent_types=('developer',),
      integration='github', requires_approval=True, risk='medium',
      icon='fa-folder-plus',
      parameters=_obj(
          required=('name',),
          name=_s('The repository name, without the owner. Lower case with '
                  'hyphens is the convention.'),
          description=_s('One line describing what it is for.'),
          private=_s('true for a private repository, false for public. '
                     'Defaults to true.'),
          organisation=_s('Create it under this organisation instead of the '
                          'authenticated user.'),
          auto_init=_s('true to add an initial README so the repository can be '
                       'cloned at once. Defaults to true.'),
          gitignore_template=_s('A .gitignore template name, e.g. Python.'),
          license_template=_s('A licence template name, e.g. mit.'),
      ))
def create_repository(ctx, name, description='', private='true',
                      organisation='', auto_init='true',
                      gitignore_template='', license_template=''):
    """Prepare a new repository. Creating it needs a person."""
    clean = str(name or '').strip().strip('/')
    if not clean:
        return ToolResult(ok=False, error='no name',
                          text='A repository name is required.')

    owner = str(organisation or '').strip()
    full = clean if '/' in clean else (f'{owner}/{clean}' if owner else clean)
    is_private = str(private).strip().lower() not in ('false', 'no', '0', 'off')

    return Proposal(
        title=f'Create GitHub repository {full}',
        summary=(f'Would create {full} as a '
                 f'{"private" if is_private else "public"} repository'
                 + (f' under the {owner} organisation' if owner else
                    ' under your own account') + '.'),
        payload={'name': clean, 'description': str(description),
                 'private': is_private, 'organisation': owner,
                 'auto_init': str(auto_init).strip().lower() not in
                              ('false', 'no', '0', 'off'),
                 'gitignore_template': str(gitignore_template),
                 'license_template': str(license_template)},
        editable_fields=[
            editable('name', 'Repository name'),
            editable('description', 'Description', 'longtext', rows=3),
            editable('private', 'Private'),
            editable('organisation', 'Organisation'),
            editable('gitignore_template', 'Gitignore template'),
            editable('license_template', 'Licence'),
        ],
        risk='medium', integration_key='github',
        confirmation=(f'Prepared the creation of {full} and placed it in the '
                      f'approval queue. Nothing exists on GitHub yet.'))


@executor('dev.create_repository')
def _execute_create_repository(action):
    payload = action.payload or {}
    result = _call('github', 'create_repo',
                   name=payload.get('name', ''),
                   description=payload.get('description', ''),
                   private=payload.get('private', True),
                   organisation=payload.get('organisation', ''),
                   auto_init=payload.get('auto_init', True),
                   gitignore_template=payload.get('gitignore_template', ''),
                   license_template=payload.get('license_template', ''))
    if not result.ok:
        return ToolResult(ok=False, error=result.error,
                          text=f'The repository was not created: {result.error}')

    data = result.data or {}
    issue = ExternalIssue.objects.create(
        system='github', container=str(data.get('repository', ''))[:200],
        reference='repository', title=f'Created {data.get("repository", "")}',
        body=payload.get('description', ''),
        issue_status='simulated' if result.demo else 'open',
        url=str(data.get('url', ''))[:400],
        agent=action.agent, action=action)
    return ToolResult(ok=True, demo=result.demo,
                      text=f'{result.summary} Recorded as #{issue.pk}.',
                      data=dict(data, record_id=issue.pk))


@tool(name='dev.update_repository',
      title='Update a GitHub repository',
      description=('Change a repository\'s description, homepage, topics, '
                   'default branch, visibility or archived state. Prepared for '
                   'approval. Requires a token with Administration write.'),
      group='Repository Administration', agent_types=('developer',),
      integration='github', requires_approval=True, risk='medium',
      icon='fa-folder-tree',
      parameters=_obj(
          repo=_s('The repository as owner/name. Uses the configured default '
                  'when empty.'),
          description=_s('A new one-line description.'),
          homepage=_s('A URL for the repository homepage.'),
          topics=_s('Comma separated topics, replacing the existing set.'),
          default_branch=_s('The branch to make default. It must already exist.'),
          private=_s('true to make it private, false to make it public.'),
          archived=_s('true to archive it, making it read only.'),
      ))
def update_repository(ctx, repo='', description='', homepage='', topics='',
                      default_branch='', private='', archived=''):
    """Prepare a change to a repository's settings. Applying it needs a person."""
    target = resolve_repository(repo)
    if not target:
        message = ("Which repository? Pass repo as 'owner/name', or set a default "
                   "repository on the GitHub integration.")
        return ToolResult(ok=False, error=message, text=message)

    payload = {'repo': target}
    wanted = []
    for key, value in (('description', description), ('homepage', homepage),
                       ('default_branch', default_branch)):
        if str(value).strip():
            payload[key] = str(value).strip()
            wanted.append(key)
    if str(topics).strip():
        payload['topics'] = str(topics)
        wanted.append('topics')
    for key, value in (('private', private), ('archived', archived)):
        if str(value).strip():
            payload[key] = str(value).strip().lower() in ('true', 'yes', '1', 'on')
            wanted.append(key)

    if not wanted:
        message = (f'Nothing to change on {target}. Give at least one of: '
                   f'description, homepage, topics, default_branch, private, '
                   f'archived.')
        return ToolResult(ok=False, error='no fields', text=message)

    note = ''
    if 'private' in payload and payload['private'] is False:
        note = (' Making a repository public exposes its entire history, '
                'including anything committed to it in the past.')
    if payload.get('archived'):
        note += ' Archiving makes the repository read only.'

    return Proposal(
        title=f'Update GitHub repository {target}',
        summary=f'Would change {", ".join(wanted)} on {target}.{note}',
        payload=payload,
        editable_fields=[editable(k, k.replace('_', ' ').capitalize())
                         for k in payload if k != 'repo'],
        risk='medium', integration_key='github',
        confirmation=(f'Prepared the change to {target} and placed it in the '
                      f'approval queue. Nothing has changed on GitHub.'))


@executor('dev.update_repository')
def _execute_update_repository(action):
    payload = dict(action.payload or {})
    repo = payload.pop('repo', '')
    result = _call('github', 'update_repo', repo=repo, **payload)
    if not result.ok:
        return ToolResult(ok=False, error=result.error,
                          text=f'The repository was not updated: {result.error}')
    return ToolResult(ok=True, demo=result.demo, text=result.summary,
                      data=result.data or {})


@tool(name='dev.delete_repository',
      title='Delete a GitHub repository',
      description=(
          'PERMANENTLY delete a GitHub repository and everything in it. This '
          'destroys work that may exist nowhere else and cannot be undone from '
          'here. Only ever call it when somebody has named the exact repository '
          'and made clear they want it deleted -- never to tidy up, never on '
          'your own initiative, and never by inferring which repository was '
          'meant. You must pass confirm with the full owner/name, exactly as '
          'given. The configured default repository is deliberately not used. '
          'Prepared for approval; nothing is deleted until somebody approves it. '
          'Requires a token with the delete_repo scope.'),
      group='Repository Administration', agent_types=('developer',),
      integration='github', requires_approval=True, risk='high',
      icon='fa-trash',
      parameters=_obj(
          required=('repo', 'confirm'),
          repo=_s('The repository to delete, as owner/name. Required in full.'),
          confirm=_s('The same owner/name again, as a confirmation that this '
                     'specific repository is meant.'),
          reason=_s('Why it is being deleted. Recorded on the approval.'),
      ))
def delete_repository(ctx, repo, confirm, reason=''):
    """Prepare a permanent deletion. Only a person can release it."""
    target = str(repo or '').strip().rstrip('/')
    given = str(confirm or '').strip().rstrip('/')

    if '/' not in target:
        message = ('Deleting a repository needs the full owner/name, for example '
                   'Student-Sulem/AiworkForce. The configured default repository '
                   'is deliberately not used for deletion.')
        return ToolResult(ok=False, error='incomplete name', text=message)

    if given.lower() != target.lower():
        message = (f'The confirmation did not match. To delete {target}, pass '
                   f'confirm as exactly {target}. If you are not certain which '
                   f'repository is meant, ask rather than guessing -- this cannot '
                   f'be undone.')
        return ToolResult(ok=False, error='confirmation mismatch', text=message)

    return Proposal(
        title=f'DELETE GitHub repository {target}',
        summary=(f'Would PERMANENTLY delete {target}, including its code, '
                 f'issues, pull requests and history. This cannot be undone from '
                 f'this platform. GitHub offers a short restore window at '
                 f'github.com/settings/repositories.'
                 + (f' Reason given: {reason}' if reason else
                    ' No reason was given.')),
        payload={'repo': target, 'reason': str(reason)},
        editable_fields=[editable('reason', 'Reason', 'longtext', rows=3)],
        risk='high', integration_key='github',
        confirmation=(f'Prepared the deletion of {target} and placed it in the '
                      f'approval queue. It has NOT been deleted. Read the '
                      f'repository name on the approval card before releasing it.'))


@executor('dev.delete_repository')
def _execute_delete_repository(action):
    payload = action.payload or {}
    target = str(payload.get('repo', '')).strip()

    # Checked again at execution, not only at proposal. A reviewer can edit a
    # payload, and the one field that must never become something else between
    # being read and being acted on is which repository is destroyed.
    if '/' not in target:
        return ToolResult(
            ok=False, error='incomplete name',
            text='The repository name was incomplete, so nothing was deleted.')

    result = _call('github', 'delete_repo', repo=target)
    if not result.ok:
        return ToolResult(ok=False, error=result.error,
                          text=f'The repository was not deleted: {result.error}')

    issue = ExternalIssue.objects.create(
        system='github', container=target[:200], reference='repository',
        title=f'Deleted {target}', body=str(payload.get('reason', '')),
        issue_status='simulated' if result.demo else 'closed',
        agent=action.agent, action=action)
    return ToolResult(ok=True, demo=result.demo,
                      text=f'{result.summary} Recorded as #{issue.pk}.',
                      data=dict(result.data or {}, record_id=issue.pk))
