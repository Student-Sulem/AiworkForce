"""The People Operations toolkit: what the HR employee can actually do.

Every tool here belongs to ``agent_types=('hr',)``, which is the whole of how
the People Operations employee acquires its capabilities: the roster page, the
function definitions sent to the language model and the AgentCapability rows
are all derived from this module by marketing/tools/__init__.py.

THE DIVIDING LINE
-----------------
Two kinds of tool live here, and the difference is not stylistic.

  * Tools that write to our own database or read from it act immediately.
    Creating an opening, parsing a resume, scoring a candidate, planning an
    interview, building an onboarding checklist -- all internal, all reversible
    by editing a row, all done the moment they are called.

  * Tools whose effect leaves the company return a ``Proposal`` and stop. The
    invitation, the rejection, the calendar booking, the Slack announcement:
    each becomes a pending ProposedAction with its full text attached, and the
    matching ``@executor`` runs only when a person approves it. A proposing
    function has no integration call in it at all, so there is no path by which
    one could send something by accident.

WHY THE SCORING IS PLAIN PYTHON
-------------------------------
No tool in this module calls a language model. That is deliberate, and it is
most important for ``hr.score_candidate``. A screening decision has to be
reproducible and explainable: the same resume against the same requirements
must give the same score tomorrow, and every point has to be attributable to a
named requirement and a quoted line of the resume. A model asked to "score this
candidate" gives a plausible number that cannot be re-derived, defended or
audited. Deterministic keyword and evidence matching gives a worse number and a
far better decision, because the reader can see exactly what it was based on
and overrule it.

The employee's own reply is where language is generated. Tools record, read,
compute and propose.

FAIRNESS IS ENFORCED, NOT REQUESTED
-----------------------------------
``score_candidate`` refuses to score a requirement line that refers to age,
gender, nationality, marital or family status, health or religion. Such a line
is recorded in the breakdown with zero available points and a note asking a
person to review the wording, and it is excluded from the total rather than
quietly ignored -- a silently dropped requirement would make the totals
incomparable between candidates. Nothing in this module reads or writes those
attributes, and models_hr has no column to put them in.
"""

import re
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

from django.utils import timezone

from .. import integrations
from .base import Proposal, ToolResult, editable, executor, tool

# ===========================================================================
# Schema shorthands
#
# A tool's ``parameters`` must be a real JSON Schema object, because it is sent
# to the language model verbatim as a function definition. These two helpers
# keep that fact from burying the tools themselves in punctuation.
# ===========================================================================


def _p(kind, description, **extra):
    row = {'type': kind, 'description': description}
    row.update(extra)
    return row


def _schema(properties, required=()):
    return {'type': 'object', 'properties': properties, 'required': list(required)}


_STRING_LIST = {'type': 'array', 'items': {'type': 'string'}}


# ===========================================================================
# Record lookups
#
# Every tool guards its lookups. A language model working from a transcript
# will occasionally invent an identifier, and the useful response to that is a
# result telling it which listing tool to call -- not an exception, which the
# runner would turn into a stack trace in a chat window.
# ===========================================================================

def _agent(ctx):
    return getattr(ctx, 'agent', None)


def _int(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _decimal(value):
    try:
        return Decimal(str(value))
    except (TypeError, ValueError, InvalidOperation):
        return None


def _opening(pk):
    from ..models_hr import JobOpening
    key = _int(pk)
    return JobOpening.objects.filter(pk=key).first() if key else None


def _candidate(pk):
    from ..models_hr import Candidate
    key = _int(pk)
    return Candidate.objects.filter(pk=key).first() if key else None


def _interview(pk):
    from ..models_hr import Interview
    key = _int(pk)
    return Interview.objects.filter(pk=key).first() if key else None


def _employee(pk):
    from ..models_hr import Employee
    key = _int(pk)
    return Employee.objects.filter(pk=key).first() if key else None


def _onboarding_task(pk):
    from ..models_hr import OnboardingTask
    key = _int(pk)
    return OnboardingTask.objects.filter(pk=key).first() if key else None


def _not_found(kind, pk, lister):
    return ToolResult(
        ok=False,
        error=f'{kind} {pk} does not exist.',
        text=f'No {kind.lower()} with id {pk}. Use {lister} to see the ids.')


# ===========================================================================
# Dates and times
# ===========================================================================

_DATETIME_FORMATS = (
    '%Y-%m-%d %H:%M', '%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M', '%Y-%m-%dT%H:%M:%S',
    '%d/%m/%Y %H:%M', '%d-%m-%Y %H:%M', '%d %b %Y %H:%M', '%d %B %Y %H:%M',
    '%Y-%m-%d', '%d/%m/%Y', '%d %b %Y', '%d %B %Y',
)


def _parse_datetime(raw):
    """Read a start time forgivingly, and say what was understood.

    Returns ``(datetime or None, description)``. The description is returned
    rather than logged because the tool must tell the reader what it parsed:
    'Thursday 12 March 2026 at 10:00' read back from '2026-03-12' is how a
    reviewer catches that no time of day was ever specified, and a booking on
    the wrong day is expensive to undo once an invitation has gone out.
    """
    text = str(raw or '').strip()
    if not text:
        return None, 'no start time was given'

    candidate = text.replace('Z', '+00:00')
    value = None
    try:
        value = datetime.fromisoformat(candidate)
    except ValueError:
        for shape in _DATETIME_FORMATS:
            try:
                value = datetime.strptime(text, shape)
                break
            except ValueError:
                continue

    if value is None:
        return None, (f'could not read "{text}" as a date and time. Give it as '
                      f'"2026-03-12 10:00" or in ISO form.')

    note = ''
    if value.hour == 0 and value.minute == 0 and not re.search(r'\d\s*:\s*\d', text):
        value = value.replace(hour=10)
        note = ' (no time of day was given, so 10:00 local time was assumed)'

    if timezone.is_naive(value):
        try:
            value = timezone.make_aware(value, timezone.get_current_timezone())
        except Exception:  # noqa: BLE001 -- ambiguous or non-existent local time
            value = timezone.make_aware(value + timedelta(hours=1),
                                        timezone.get_current_timezone())
            note += ' (the local clock change made the original time ambiguous, '
            note += 'so it was moved an hour later)'

    return value, value.strftime('%A %d %B %Y at %H:%M %Z').strip() + note


def _parse_date(raw):
    """A plain calendar date, or None."""
    if isinstance(raw, date) and not isinstance(raw, datetime):
        return raw
    parsed, _description = _parse_datetime(raw)
    return parsed.date() if parsed else None


def _readable_when(value):
    return value.strftime('%A %d %B %Y at %H:%M %Z').strip() if value else 'not yet scheduled'


# ===========================================================================
# The screening vocabulary
#
# One curated list, used for three jobs: pulling skills out of a resume,
# picking the checkable terms out of a requirement, and deciding which of a
# requirement's words carry weight. Keeping it in one place is what makes
# 'Spring Boot' a single skill in all three rather than a phrase in one and
# two stopwords in another.
#
# Display casing is kept here and matching is done in lower case, so a tool
# result reads 'PostgreSQL' rather than 'postgresql'.
# ===========================================================================

_SKILL_VOCABULARY = (
    # Languages
    'Python', 'Java', 'JavaScript', 'TypeScript', 'C#', 'C++', 'Go', 'Golang',
    'Rust', 'Ruby', 'PHP', 'Kotlin', 'Swift', 'Scala', 'SQL', 'Bash',
    'PowerShell', 'HTML', 'CSS',
    # Frameworks and web
    'Django', 'Flask', 'FastAPI', 'REST', 'GraphQL', 'React', 'Angular', 'Vue',
    'Next.js', 'Node.js', 'Express', 'Spring Boot', 'Rails', 'Laravel',
    'ASP.NET', 'HTMX', 'Tailwind', 'Bootstrap', 'jQuery', 'Celery',
    # Data
    'PostgreSQL', 'Postgres', 'MySQL', 'SQLite', 'Oracle', 'SQL Server',
    'MongoDB', 'Redis', 'Elasticsearch', 'Kafka', 'RabbitMQ', 'Snowflake',
    'BigQuery', 'Spark', 'Hadoop', 'Airflow', 'dbt', 'Pandas', 'NumPy',
    'scikit-learn', 'PyTorch', 'TensorFlow', 'Power BI', 'Tableau', 'Looker',
    'Excel', 'data modelling', 'ETL',
    # Cloud and operations
    'AWS', 'Azure', 'GCP', 'Google Cloud', 'Docker', 'Kubernetes', 'Terraform',
    'Ansible', 'Jenkins', 'GitHub Actions', 'GitLab CI', 'CI/CD', 'Linux',
    'Nginx', 'Prometheus', 'Grafana', 'Datadog', 'observability',
    # Practice
    'Agile', 'Scrum', 'Kanban', 'Jira', 'Confluence', 'Git', 'GitHub',
    'GitLab', 'TDD', 'unit testing', 'pytest', 'Selenium', 'Cypress',
    'Playwright', 'microservices', 'API design', 'code review',
    'pair programming', 'OAuth', 'JWT', 'accessibility', 'WCAG', 'SEO',
    'system design', 'refactoring', 'technical writing',
    # Business systems
    'Salesforce', 'HubSpot', 'Zendesk', 'Intercom', 'Freshdesk', 'SAP',
    'Xero', 'MYOB', 'NetSuite', 'Workday', 'BambooHR',
    # Functional
    'payroll', 'recruitment', 'onboarding', 'employee relations',
    'stakeholder management', 'project management', 'budgeting',
    'forecasting', 'data analysis', 'reporting', 'copywriting',
    'content marketing', 'Google Analytics', 'CRM', 'customer service',
    'account management', 'bookkeeping', 'reconciliation', 'compliance',
    'risk management', 'change management', 'workforce planning',
    # Stated behaviours, scoreable only where the resume actually claims them
    'mentoring', 'leadership', 'presentation', 'negotiation', 'documentation',
    'training', 'facilitation', 'communication',
)

_SKILL_LOOKUP = {name.lower(): name for name in _SKILL_VOCABULARY}
_SKILL_KEYS = frozenset(_SKILL_LOOKUP)
_MULTIWORD_SKILLS = tuple(sorted(
    (key for key in _SKILL_LOOKUP if ' ' in key), key=len, reverse=True))

# Words that carry no screening signal. 'Experience' and 'years' are in here
# because a stated duration is handled separately and precisely; left in the
# keyword set they would match every resume ever written and inflate every
# score by the same meaningless amount.
_STOPWORDS = frozenset("""
a an and or but the of in on for with to at by from as is are was were be been
being have has had having do does did you your our their we they he she it its
this that those these there here who whom which what when where why how all any
both each few more most other some such only own same so than too very can will
would should could must need needs needed require required requires requirement
requirements essential desirable preferred plus bonus advantage advantageous
strong solid proven demonstrated excellent good great ability able capable
experience experienced working work works worked knowledge understanding
familiar familiarity least minimum maximum year years yrs month months role
position candidate applicant successful ideal ideally including include includes
etc using use used across within into over under about also well team teams
environment environments new
""".split())

# Requirement lines that refer to a protected or personal attribute. Matched
# narrowly on purpose: 'health care experience' and 'disability services' are
# legitimate job requirements in this market, so only phrasings that clearly
# describe the person rather than the work are caught.
_PROTECTED_PATTERNS = (
    ('age', (r'\bage\b', r'\baged\b', r'age range', r'years old',
             r'date of birth', r'\bdob\b')),
    ('gender', (r'\bgender\b', r'\bsex\b', r'\bmale\b', r'\bfemale\b')),
    ('nationality', (r'\bnationality\b', r'national origin', r'\bethnicit',
                     r'\brace\b', r'country of birth')),
    ('marital or family status', (r'\bmarital\b', r'\bmarried\b', r'\bpregnan',
                                 r'family status')),
    ('health or disability status', (r'health (status|condition|record|history)',
                                    r'medical (condition|record|history)',
                                    r'disability status', r'\bbmi\b')),
    ('religion', (r'\breligio',)),
)

_COMPILED_PROTECTED = tuple(
    (label, tuple(re.compile(shape, re.IGNORECASE) for shape in shapes))
    for label, shapes in _PROTECTED_PATTERNS)


def _protected_attribute(text):
    """The protected category a requirement line refers to, or ''.

    Used to refuse to score such a line. Returning the category name rather
    than a boolean means the breakdown can say what the problem is, which is
    the difference between a note somebody acts on and one they skim past.
    """
    for label, shapes in _COMPILED_PROTECTED:
        for shape in shapes:
            if shape.search(text or ''):
                return label
    return ''


def _contains(haystack, needle):
    """Whole-token containment that survives '+', '#', '.' and '/'.

    A word-boundary escape cannot be used here: it would fail on the trailing
    symbols of 'C++' and would split 'CI/CD' in two. Explicit lookarounds on
    alphanumerics behave correctly for every entry in the vocabulary.
    """
    if not needle:
        return False
    shape = r'(?<![a-z0-9])' + re.escape(needle) + r'(?![a-z0-9])'
    return re.search(shape, haystack) is not None


# A requirement asks for a capability; a resume names a product. 'SQL' is
# evidenced by 'PostgreSQL query tuning' and 'a container workflow' by
# 'Docker', and without these a candidate who plainly meets a requirement
# scores zero on it because they used the specific word rather than the
# general one. Aliases are one-directional: the requirement's word is the key.
_SKILL_ALIASES = {
    'sql': ('postgresql', 'postgres', 'mysql', 'sqlite', 'sql server', 'oracle',
            'bigquery', 'snowflake', 'tsql', 'plsql'),
    'database': ('postgresql', 'postgres', 'mysql', 'sqlite', 'mongodb', 'oracle',
                 'sql', 'redis', 'sql server'),
    'databases': ('postgresql', 'postgres', 'mysql', 'sqlite', 'mongodb', 'sql'),
    'relational': ('postgresql', 'postgres', 'mysql', 'sqlite', 'sql server',
                   'oracle', 'sql'),
    'testing': ('pytest', 'unit testing', 'tdd', 'selenium', 'cypress',
                'playwright', 'test', 'tests', 'test suite'),
    'api': ('rest', 'graphql', 'apis', 'endpoint', 'endpoints'),
    'apis': ('rest', 'graphql', 'api', 'endpoint', 'endpoints'),
    'cloud': ('aws', 'azure', 'gcp', 'google cloud'),
    'container': ('docker', 'kubernetes'),
    'containers': ('docker', 'kubernetes'),
    'kubernetes': ('k8s',),
    'ci/cd': ('github actions', 'gitlab ci', 'jenkins', 'pipeline', 'pipelines',
              'continuous integration', 'continuous delivery'),
    'pipelines': ('airflow', 'dbt', 'etl', 'pipeline'),
    'agile': ('scrum', 'kanban', 'sprint', 'sprints'),
    'leadership': ('led', 'managed', 'mentored', 'headed'),
    'mentoring': ('mentored', 'mentor', 'coached', 'coaching'),
    'ticketing': ('zendesk', 'intercom', 'freshdesk', 'jira', 'servicenow'),
    'dashboard': ('power bi', 'tableau', 'looker', 'dashboards'),
    'reporting': ('report', 'reports', 'dashboard', 'power bi', 'tableau'),
    'microservices': ('microservice', 'service-oriented'),
    'observability': ('prometheus', 'grafana', 'datadog', 'monitoring'),
    'api design': ('api', 'apis', 'rest', 'graphql', 'endpoint', 'endpoints'),
    'refactoring': ('refactor', 'refactored', 'debug', 'debugged', 'debugging',
                    'legacy'),
    'stakeholder management': ('stakeholder', 'stakeholders', 'cross-functional'),
    'communication': ('communicated', 'presented', 'wrote', 'documentation',
                      'stakeholder'),
}


def _contains_loose(haystack, needle):
    """As ``_contains``, tolerating plurals, gerunds and known aliases."""
    if _contains(haystack, needle):
        return True
    for variant in (needle + 's', needle.rstrip('s'), needle + 'ing'):
        if variant != needle and len(variant) > 2 and _contains(haystack, variant):
            return True
    for alias in _SKILL_ALIASES.get(needle, ()):
        if _contains(haystack, alias):
            return True
    return False


def _keywords(text, limit=5):
    """The checkable terms in one requirement line, best first.

    Where the line names anything from the screening vocabulary, ONLY those
    terms are returned. That restriction is the difference between a working
    score and a useless one: 'Automated testing as a normal habit, not an
    afterthought' contains four content words no resume will ever use, and
    counting them as unmet requirements pushes every candidate towards reject
    for reasons that have nothing to do with them. Filler cannot be evidenced,
    so it must not be scoreable.

    Only where a line names nothing recognisable does it fall back to content
    words, capped tightly for the same reason.
    """
    low = ' ' + (text or '').lower() + ' '
    known, plain = [], []

    for phrase in _MULTIWORD_SKILLS:
        if _contains(low, phrase):
            known.append(phrase)
            low = low.replace(phrase, ' ')

    for token in re.findall(r'[a-z][a-z0-9+#./\-]*', low):
        token = token.strip('.-/')
        if len(token) < 2 or token in _STOPWORDS:
            continue
        if token in _SKILL_KEYS:
            if token not in known:
                known.append(token)
        elif token in _SKILL_ALIASES:
            if token not in known:
                known.append(token)
        elif token not in plain:
            plain.append(token)

    if known:
        known.sort(key=lambda key: -len(key))
        return known[:limit]

    plain.sort(key=lambda key: -len(key))
    return plain[:min(limit, 4)]


def _has_named_skill(keywords):
    """Whether the requirement named something recognisable rather than prose."""
    return any(key in _SKILL_KEYS or key in _SKILL_ALIASES for key in keywords)


def _display(keyword):
    return _SKILL_LOOKUP.get(keyword, keyword)


def _stated_years(text):
    """The largest duration a piece of text claims, in whole years, or None."""
    best = None
    for match in re.finditer(r'(\d{1,2})\s*(?:\+|plus)?\s*(?:years?|yrs?)\b',
                             text or '', re.IGNORECASE):
        value = int(match.group(1))
        if 0 < value <= 50 and (best is None or value > best):
            best = value
    return best


# ===========================================================================
# Resume parsing
#
# Everything below is regular expressions and word lists. It is not clever and
# it is not meant to be: a parse that is wrong in a predictable way can be
# checked and corrected by the reader, whereas a parse that is usually right
# for reasons nobody can inspect cannot. Each returned fact carries how it was
# arrived at, so 'six years' can be read as 'the resume says so' or 'the dates
# add up to that', which are very different claims.
# ===========================================================================

_MONTH_NUMBERS = {
    'jan': 1, 'feb': 2, 'mar': 3, 'apr': 4, 'may': 5, 'jun': 6, 'jul': 7,
    'aug': 8, 'sep': 9, 'sept': 9, 'oct': 10, 'nov': 11, 'dec': 12,
}

_DATE_RANGE = re.compile(
    r'(?P<m1>[A-Za-z]{3,9})?\.?\s*(?P<y1>(?:19|20)\d{2})'
    r'\s*(?:-{1,2}|to|until|through|–|—)\s*'
    r'(?P<m2>[A-Za-z]{3,9})?\.?\s*(?P<y2>(?:19|20)\d{2}|present|current|now|ongoing|to date)',
    re.IGNORECASE)

_TITLE_WORDS = (
    'engineer', 'developer', 'programmer', 'manager', 'analyst', 'designer',
    'architect', 'consultant', 'specialist', 'administrator', 'director',
    'lead', 'scientist', 'officer', 'coordinator', 'executive', 'accountant',
    'technician', 'recruiter', 'representative', 'strategist', 'marketer',
    'intern', 'associate', 'partner', 'supervisor', 'head of', 'chief',
)

_DEGREE_WORDS = (
    'bachelor', 'master', 'mba', 'phd', 'doctorate', 'b.sc', 'bsc', 'm.sc',
    'msc', 'b.eng', 'beng', 'b.tech', 'btech', 'm.tech', 'b.com', 'bcom',
    'diploma', 'advanced diploma', 'certificate iii', 'certificate iv',
    'associate degree', 'honours', 'llb', 'mbbs', 'graduate certificate',
)

_CERTIFICATION_WORDS = (
    'aws certified', 'azure certified', 'google cloud certified', 'pmp',
    'prince2', 'itil', 'certified scrum master', 'scrum master', 'safe',
    'cissp', 'cisa', 'cpa', 'ca anz', 'ccna', 'cka', 'six sigma',
    'certified practising accountant',
)


def _months(month_word, default):
    if not month_word:
        return default
    return _MONTH_NUMBERS.get(month_word[:4].lower().strip('.'),
                              _MONTH_NUMBERS.get(month_word[:3].lower(), default))


def _date_ranges(text):
    """Employment date ranges as decimal years, earliest first.

    A range is kept as ``(start, end, label)`` in decimal years, which makes
    the gap arithmetic below one subtraction rather than a calendar problem.

    Ranges sitting on a line that names a degree are dropped. A degree taken
    from 2013 to 2016 is not employment, and counting it inflates the derived
    duration and invents a gap between graduating and the first job -- both of
    which would then be reported as facts about a career.
    """
    today = date.today()
    now_decimal = today.year + (today.month - 1) / 12
    rows = []
    for match in _DATE_RANGE.finditer(text or ''):
        line_start = (text or '').rfind('\n', 0, match.start()) + 1
        line_end = (text or '').find('\n', match.end())
        line = (text or '')[line_start:line_end if line_end != -1 else None].lower()
        if any(word in line for word in _DEGREE_WORDS):
            continue
        start_year = int(match.group('y1'))
        start = start_year + (_months(match.group('m1'), 1) - 1) / 12
        raw_end = match.group('y2').lower()
        if raw_end.isdigit():
            end = int(raw_end) + (_months(match.group('m2'), 12) - 1) / 12
        else:
            end = now_decimal
        if end < start:
            start, end = end, start
        if 1960 <= start <= now_decimal + 1:
            rows.append((round(start, 3), round(min(end, now_decimal + 0.1), 3),
                         ' '.join(match.group(0).split())))
    rows.sort()
    return rows


def _covered_years(ranges):
    """Total time covered by the ranges, overlaps counted once."""
    total = 0.0
    reached = None
    for start, end, _label in ranges:
        if reached is None or start > reached:
            total += max(0.0, end - start)
            reached = end
        elif end > reached:
            total += end - reached
            reached = end
    return round(total, 1)


def _employment_gaps(ranges, threshold_months=6):
    """Unexplained breaks between consecutive roles.

    Reported as a neutral fact with the months counted, never as a judgement.
    A gap is a thing to ask about in an interview, and the point of surfacing
    it is that the question gets asked rather than guessed at.
    """
    gaps = []
    reached = None
    for start, end, _label in ranges:
        if reached is not None and start - reached >= threshold_months / 12:
            months = int(round((start - reached) * 12))
            gaps.append({
                'from': _decimal_year_label(reached),
                'to': _decimal_year_label(start),
                'months': months,
            })
        reached = max(reached or 0.0, end)
    return gaps


def _decimal_year_label(value):
    year = int(value)
    month = int(round((value - year) * 12)) + 1
    month = min(max(month, 1), 12)
    names = ['January', 'February', 'March', 'April', 'May', 'June', 'July',
             'August', 'September', 'October', 'November', 'December']
    return f'{names[month - 1]} {year}'


def _extract_skills(text):
    low = (text or '').lower()
    return [_SKILL_LOOKUP[key] for key in _SKILL_LOOKUP if _contains(low, key)]


def _extract_titles(text):
    """Lines that read like a job title, in the order they appear.

    Certification lines are skipped even though they match the title words:
    'AWS Certified Solutions Architect' contains 'architect' but is a
    qualification, and letting it through means the first entry -- which is
    read back as the current title -- can be a certificate.
    """
    titles = []
    for line in re.split(r'[\r\n|;]+', text or ''):
        cleaned = ' '.join(line.split())
        if not cleaned or len(cleaned) > 90:
            continue
        low = cleaned.lower()
        if any(word in low for word in _CERTIFICATION_WORDS):
            continue
        if 'certified' in low or 'certificate' in low:
            continue
        if any(word in low for word in _TITLE_WORDS):
            trimmed = re.split(r'\s{2,}|\s[–-]\s|,\s', cleaned)[0].strip()
            trimmed = re.sub(r'\b(19|20)\d{2}\b.*$', '', trimmed).strip(' ,-')
            if 3 <= len(trimmed) <= 70 and trimmed not in titles:
                titles.append(trimmed)
    return titles[:6]


def _match_any(text, vocabulary):
    """Vocabulary entries present in the text, quoted as the text wrote them.

    The original casing is returned rather than the vocabulary entry, so a
    result reads 'AWS Certified' rather than 'Aws Certified'. Reading a fact
    back to somebody in a casing they did not use makes it look invented.
    """
    body = text or ''
    low = body.lower()
    found = []
    for entry in vocabulary:
        position = low.find(entry)
        if position >= 0:
            found.append(body[position:position + len(entry)])
    return found


def _phone_numbers(text):
    """Digit runs that plausibly are telephone numbers.

    Nine digits minimum, and a year range is rejected outright: '2013 - 2016'
    satisfies every loose numeric pattern and would otherwise be offered to a
    recruiter as a contact number.
    """
    found = []
    for raw in re.findall(r'\+?\d[\d\s().-]{7,}\d', text or ''):
        cleaned = ' '.join(raw.split())
        digits = re.sub(r'\D', '', cleaned)
        if len(digits) < 9 or len(digits) > 15:
            continue
        if re.fullmatch(r'(?:19|20)\d{2}\s*[-.]\s*(?:19|20)\d{2}', cleaned):
            continue
        if cleaned not in found:
            found.append(cleaned)
    return found[:3]


def _analyse_resume_text(text):
    """Everything the heuristics can honestly say about one resume."""
    body = text or ''
    ranges = _date_ranges(body)
    stated = _stated_years(body)
    derived = _covered_years(ranges) if ranges else None

    if stated is not None:
        years, source = float(stated), 'stated in the resume'
    elif derived:
        years, source = float(derived), 'derived from the employment dates'
    else:
        years, source = None, 'not determinable from the text'

    titles = _extract_titles(body)
    return {
        'skills': _extract_skills(body),
        'years_experience': years,
        'years_source': source,
        'years_stated': stated,
        'years_from_dates': derived,
        'titles': titles,
        'current_title': titles[0] if titles else '',
        'education': _match_any(body, _DEGREE_WORDS),
        'certifications': _match_any(body, _CERTIFICATION_WORDS),
        'emails': sorted(set(re.findall(r'[\w.+-]+@[\w-]+\.[\w.-]+', body))),
        'phones': _phone_numbers(body),
        'links': sorted(set(re.findall(r'https?://[^\s,)\]]+', body)))[:6],
        'date_ranges': [label for _s, _e, label in ranges],
        'employment_gaps': _employment_gaps(ranges),
        'word_count': len(body.split()),
    }


def _resume_summary(facts):
    """The parse said in sentences, because the model reads the text field."""
    lines = []
    if facts['years_experience'] is not None:
        lines.append(f"Around {facts['years_experience']:g} years of experience "
                     f"({facts['years_source']}).")
    else:
        lines.append('No usable duration of experience could be read from the text.')
    if facts['current_title']:
        lines.append(f"Most recent title read as \"{facts['current_title']}\".")
    if facts['skills']:
        lines.append(f"Skills found ({len(facts['skills'])}): "
                     f"{', '.join(facts['skills'][:18])}.")
    else:
        lines.append('No skills from the screening vocabulary appear in the text, '
                     'which usually means the resume is a scan or the extraction failed.')
    if facts['education']:
        lines.append(f"Education mentions: {', '.join(facts['education'])}.")
    if facts['certifications']:
        lines.append(f"Certifications mentioned: {', '.join(facts['certifications'])}.")
    if facts['date_ranges']:
        lines.append(f"Date ranges found: {', '.join(facts['date_ranges'][:8])}.")
    if facts['employment_gaps']:
        spans = '; '.join(f"{g['months']} months between {g['from']} and {g['to']}"
                          for g in facts['employment_gaps'])
        lines.append(f"Breaks of six months or more between roles: {spans}. "
                     f"Worth asking about; not a mark against the candidate.")
    elif facts['date_ranges']:
        lines.append('No break of six months or more appears between the roles listed.')
    if facts['word_count'] < 80:
        lines.append(f"Only {facts['word_count']} words were available, so treat every "
                     f"finding above as provisional.")
    return ' '.join(lines)


# ===========================================================================
# Requirement handling and scoring
# ===========================================================================

_DEFAULT_WEIGHT = 3
_MANDATORY_MARKERS = ('must have', 'must-have', 'must ', 'required', 'requires',
                      'essential', 'mandatory', 'minimum', 'at least')


def _looks_mandatory(text):
    low = (text or '').lower()
    return any(marker in low for marker in _MANDATORY_MARKERS)


def _normalise_requirements(raw):
    """Force any accepted shape into the scoreable one.

    The model is told to send dictionaries and will sometimes send strings,
    and a person pasting a job advertisement will always send strings. Both
    are accepted and normalised here, once, so that no scoring code ever has
    to ask which shape it received.
    """
    rows = []
    for entry in (raw or []):
        if isinstance(entry, dict):
            text = str(entry.get('text') or entry.get('requirement')
                       or entry.get('name') or '').strip()
            weight = _int(entry.get('weight'), _DEFAULT_WEIGHT)
            must_have = bool(entry.get('must_have', _looks_mandatory(text)))
        else:
            text = str(entry or '').strip()
            weight = _DEFAULT_WEIGHT
            must_have = _looks_mandatory(text)
        if not text:
            continue
        rows.append({
            'text': text[:400],
            'weight': min(5, max(1, weight or _DEFAULT_WEIGHT)),
            'must_have': must_have,
        })
    return rows


def _plain_list(raw, limit=40):
    """A JSON list field cleaned into plain strings."""
    rows = []
    for entry in (raw or []):
        if isinstance(entry, dict):
            value = str(entry.get('text') or entry.get('name') or '').strip()
        else:
            value = str(entry or '').strip()
        if value:
            rows.append(value[:400])
    return rows[:limit]


def _best_sentence(resume_text, keywords):
    """The sentence covering the most of these keywords, with the hits.

    Quoting the candidate's own words is the point. A score with no evidence
    behind it is an opinion, and an opinion is not something a hiring manager
    can act on or argue with.
    """
    best, best_hits = '', []
    for sentence in re.split(r'(?<=[.!?])\s+|\n+', resume_text or ''):
        cleaned = ' '.join(sentence.split())
        if not cleaned:
            continue
        low = cleaned.lower()
        hits = [key for key in keywords if _contains_loose(low, key)]
        if len(hits) > len(best_hits):
            best, best_hits = cleaned, hits
    return best[:220], best_hits


def _band(ratio):
    """Coverage turned into a proportion of the available points.

    Banded rather than linear so that a single incidental keyword match cannot
    read as half a requirement met. The steps are coarse on purpose: the
    reader is being told 'well covered' or 'barely mentioned', and false
    precision in a screening score invites more confidence than it deserves.
    """
    if ratio >= 0.85:
        return 1.0
    if ratio >= 0.6:
        return 0.8
    if ratio >= 0.34:
        return 0.55
    if ratio > 0:
        return 0.3
    return 0.0


def _score_one_requirement(requirement, resume_text, haystack, candidate_years):
    """One breakdown row: requirement, score, max, evidence."""
    text = requirement['text']

    protected = _protected_attribute(text)
    if protected:
        return {
            'requirement': text, 'score': 0, 'max': 0,
            'evidence': (f'Not scored. This line refers to {protected}, which is not '
                         f'a job-related criterion and is excluded from the total. '
                         f'A person should rewrite or remove it.'),
            'skipped': True,
        }

    weight = requirement['weight'] * (2 if requirement['must_have'] else 1)
    keywords = _keywords(text)
    wanted_years = _stated_years(text)

    # A line naming neither a recognisable skill nor a duration cannot be
    # evidenced from a resume at all. Scoring it anyway on whatever words it
    # happens to contain punishes the candidate for how the advertisement was
    # written -- 'you will thrive here' would cost real points -- so it is
    # excluded from the total and flagged for rewriting instead.
    if not _has_named_skill(keywords) and wanted_years is None:
        return {
            'requirement': text, 'score': 0, 'max': 0,
            'evidence': ('Not scored. This line names no skill, tool or duration, so '
                         'there is nothing in a resume that could evidence it either '
                         'way. Rewrite it as something checkable, or assess it at '
                         'interview instead.'),
            'skipped': True,
        }

    sentence, hits = _best_sentence(resume_text, keywords)
    all_hits = [key for key in keywords if _contains_loose(haystack, key)]
    keyword_ratio = (len(all_hits) / len(keywords)) if keywords else None

    years_ratio = None
    if wanted_years:
        if candidate_years is None:
            years_ratio = 0.0
        else:
            years_ratio = min(1.0, float(candidate_years) / float(wanted_years))

    if keyword_ratio is None:
        ratio = years_ratio or 0.0
    elif years_ratio is None:
        ratio = keyword_ratio
    elif _has_named_skill(keywords):
        # The line asks for both a duration and a named skill, so both count.
        ratio = 0.5 * keyword_ratio + 0.5 * years_ratio
    else:
        # A duration plus prose. The duration is the checkable half; the words
        # around it ('production software', 'a comparable role') are mostly
        # scenery, so they get a light touch rather than half the weight.
        ratio = 0.75 * years_ratio + 0.25 * keyword_ratio

    earned = round(weight * _band(ratio), 2)

    parts = []
    if all_hits:
        parts.append('Matched ' + ', '.join(_display(k) for k in all_hits[:6]))
        if sentence and hits:
            parts.append(f'in: "{sentence}"')
    elif keywords:
        parts.append('No mention of ' + ', '.join(_display(k) for k in keywords[:6])
                     + ' anywhere in the resume')
    if wanted_years:
        if candidate_years is None:
            parts.append(f'the line asks for {wanted_years} years and the resume gives '
                         f'no readable duration')
        else:
            parts.append(f'{float(candidate_years):g} years available against the '
                         f'{wanted_years} asked for')

    return {
        'requirement': text,
        'score': earned,
        'max': weight,
        'evidence': ('. '.join(parts) + '.') if parts else 'No evidence either way.',
        'must_have': requirement['must_have'],
        'matched': [_display(k) for k in all_hits],
        'missing': [_display(k) for k in keywords if k not in all_hits],
        'skipped': False,
    }


def _screen(opening, candidate):
    """Score one candidate against one opening. Wholly deterministic.

    Returns the breakdown, the totals, the strengths and gaps, and a
    recommendation. Called by ``hr.score_candidate`` and by
    ``hr.compare_candidates`` when a candidate has never been screened, so
    that a comparison never silently omits somebody.
    """
    requirements = _normalise_requirements(opening.requirements)
    resume_text = candidate.resume_text or ''
    haystack = ' '.join([
        resume_text, ' '.join(str(s) for s in (candidate.skills or [])),
        candidate.current_title or '', candidate.current_company or '',
    ]).lower()

    rows = [_score_one_requirement(row, resume_text, haystack,
                                   candidate.years_experience)
            for row in requirements]

    scored = [row for row in rows if row['max']]
    available = sum(row['max'] for row in scored)
    earned = sum(row['score'] for row in scored)
    total = round(earned * 100 / available, 2) if available else 0.0

    strengths, gaps = [], []
    for row in scored:
        share = row['score'] / row['max'] if row['max'] else 0
        if share >= 0.8:
            strengths.append(row['requirement'])
        elif share <= 0.4:
            gaps.append(row['requirement'])

    unmet_must_haves = [row['requirement'] for row in scored
                        if row.get('must_have') and row['score'] / row['max'] < 0.5]
    unscoreable = [row['requirement'] for row in rows if row['skipped']]

    if not available:
        recommendation = 'hold'
        rationale = ('Nothing could be scored: the opening has no checkable '
                     'requirements. Run hr.generate_job_requirements on the opening '
                     'first, then screen again.')
    elif unmet_must_haves:
        recommendation = 'hold' if total >= 55 else 'reject'
        rationale = (
            f'Scored {total:g} out of 100 across {len(scored)} scoreable requirements, '
            f'but {len(unmet_must_haves)} must-have requirement'
            f'{"s are" if len(unmet_must_haves) > 1 else " is"} not evidenced: '
            f'{"; ".join(unmet_must_haves[:3])}. '
            f'A must-have gap is the reason for the recommendation, not the total.')
    elif total >= 75:
        recommendation = 'advance'
        rationale = (f'Scored {total:g} out of 100 with every must-have requirement '
                     f'evidenced in the resume. Strongest areas: '
                     f'{"; ".join(strengths[:3]) or "spread evenly"}.')
    elif total >= 55:
        recommendation = 'hold'
        rationale = (f'Scored {total:g} out of 100. The must-haves are covered but the '
                     f'weighting is thin: {"; ".join(gaps[:3]) or "no single large gap"}. '
                     f'Worth a screening call rather than an immediate decision.')
    else:
        recommendation = 'reject'
        rationale = (f'Scored {total:g} out of 100 against the stated requirements. '
                     f'Main gaps: {"; ".join(gaps[:4]) or "coverage is thin throughout"}.')

    if unscoreable:
        rationale += (f' {len(unscoreable)} requirement line'
                      f'{"s were" if len(unscoreable) > 1 else " was"} excluded from the '
                      f'total as not job-related or not checkable; see the breakdown.')

    if not resume_text.strip():
        rationale += (' There is no resume text on this candidate, so the score reflects '
                      'an absence of evidence rather than an absence of ability.')

    return {
        'breakdown': rows,
        'total': total,
        'available': available,
        'earned': round(earned, 2),
        'strengths': strengths,
        'gaps': gaps,
        'unmet_must_haves': unmet_must_haves,
        'unscoreable': unscoreable,
        'recommendation': recommendation,
        'rationale': rationale,
    }


# ===========================================================================
# Generators
#
# These build requirement sets, question sets, forms and checklists from
# templates. They are templates rather than model output for the same reason
# the scoring is: the employee's reply can phrase things freshly, but the
# artefact a hiring manager scores against has to be the same shape every
# time, or two candidates interviewed a week apart were not measured with the
# same instrument.
# ===========================================================================

_REQUIREMENT_LIBRARY = {
    'engineering': (
        ('{years} years building and maintaining production software', 5, True),
        ('Strong {stack}, with design decisions you can defend', 5, True),
        ('Relational database design and SQL beyond simple queries', 4, True),
        ('Automated testing as a normal habit, not an afterthought', 4, True),
        ('Git and code review in a team of several developers', 3, True),
        ('Debugging and refactoring code somebody else wrote', 4, False),
        ('CI/CD pipelines and deploying without a manual runbook', 3, False),
        ('API design that other teams have had to consume', 3, False),
        ('Docker or another container workflow', 2, False),
        ('Cloud hosting on AWS, Azure or GCP', 2, False),
    ),
    'data': (
        ('{years} years working with data in a production setting', 5, True),
        ('SQL to the level of window functions and query tuning', 5, True),
        ('Python for data work, including Pandas', 4, True),
        ('Building a pipeline somebody else depends on daily', 4, True),
        ('Explaining a number to a non-technical audience', 4, False),
        ('Data modelling for reporting, not just for storage', 3, False),
        ('A dashboarding tool such as Power BI, Tableau or Looker', 2, False),
        ('Airflow, dbt or comparable orchestration', 2, False),
    ),
    'management': (
        ('{years} years leading a team, including hiring and performance', 5, True),
        ('Planning work across a quarter and reporting honestly against it', 5, True),
        ('Running one-to-ones and difficult conversations', 4, True),
        ('Stakeholder management outside your own function', 4, True),
        ('Enough technical depth to challenge an estimate', 3, False),
        ('Setting and defending priorities when everything is urgent', 4, False),
        ('Improving a process and measuring whether it worked', 3, False),
    ),
    'support': (
        ('{years} years in a customer-facing support role', 4, True),
        ('Written communication a customer would thank you for', 5, True),
        ('A ticketing system such as Zendesk, Intercom or Freshdesk', 3, True),
        ('Working to a service level agreement', 3, True),
        ('Diagnosing a problem from an incomplete description', 4, False),
        ('Escalating without dropping the customer', 3, False),
        ('Writing help documentation from what you learn', 2, False),
    ),
    'marketing': (
        ('{years} years producing marketing content that shipped', 4, True),
        ('Writing for a specific channel rather than one house voice', 5, True),
        ('Planning a campaign and reporting what it achieved', 4, True),
        ('Google Analytics or comparable measurement', 3, False),
        ('Working from a brand and tone guide', 3, False),
        ('Briefing and reviewing the work of other people', 2, False),
        ('SEO fundamentals', 2, False),
    ),
    'people': (
        ('{years} years in a people or recruitment role', 4, True),
        ('End-to-end recruitment for roles you were accountable for', 5, True),
        ('Handling employee matters with discretion', 5, True),
        ('Working knowledge of local employment obligations', 4, True),
        ('Onboarding design, not just paperwork', 3, False),
        ('An HR system such as Workday or BambooHR', 2, False),
        ('Reporting on people data', 2, False),
    ),
    'finance': (
        ('{years} years in an accounting or finance role', 4, True),
        ('Month-end close and reconciliation', 5, True),
        ('An accounting system such as Xero, MYOB or NetSuite', 4, True),
        ('Excel to a level that survives an audit', 4, True),
        ('Explaining a variance to someone who owns the budget', 3, False),
        ('Relevant professional qualification or progress towards one', 3, False),
    ),
    'general': (
        ('{years} years in a comparable role', 4, True),
        ('Clear written and spoken communication', 4, True),
        ('Owning a piece of work from start to finish', 4, True),
        ('Stakeholder management outside your own team', 3, True),
        ('Prioritising when the workload exceeds the time', 3, False),
        ('Learning an unfamiliar system quickly', 3, False),
        ('Documentation somebody else could follow to repeat the work', 2, False),
    ),
}

_FAMILY_MARKERS = (
    ('engineering', ('engineer', 'developer', 'programmer', 'software', 'backend',
                     'back-end', 'frontend', 'front-end', 'full stack', 'full-stack',
                     'devops', 'platform', 'qa', 'tester', 'architect', 'django',
                     'python', 'java', 'react')),
    ('data', ('data', 'analyst', 'analytics', 'scientist', 'bi', 'machine learning',
              'reporting')),
    ('management', ('manager', 'lead', 'head of', 'director', 'chief', 'principal',
                    'supervisor', 'delivery')),
    ('support', ('support', 'service desk', 'helpdesk', 'customer success',
                 'customer care', 'representative')),
    ('marketing', ('marketing', 'content', 'communications', 'social media', 'brand',
                   'copywriter', 'seo', 'growth')),
    ('people', ('hr', 'human resources', 'people', 'recruit', 'talent', 'payroll')),
    ('finance', ('finance', 'account', 'bookkeep', 'payable', 'receivable', 'audit',
                 'tax')),
)

_SENIORITY_YEARS = {
    'intern': 0, 'junior': 1, 'mid': 3, 'senior': 5, 'lead': 7,
    'principal': 9, 'manager': 5, 'director': 10,
}

_STACK_HINTS = (
    ('Django and Python', ('django', 'python')),
    ('React and TypeScript', ('react', 'typescript', 'frontend', 'front-end')),
    ('Java and Spring Boot', ('java', 'spring')),
    ('.NET and C#', ('.net', 'c#', 'asp')),
    ('Node.js and TypeScript', ('node', 'express')),
)


def _role_family(text):
    low = (text or '').lower()
    for family, markers in _FAMILY_MARKERS:
        if any(marker in low for marker in markers):
            return family
    return 'general'


def _stack_for(text):
    low = (text or '').lower()
    for label, markers in _STACK_HINTS:
        if any(marker in low for marker in markers):
            return label
    return 'command of the technologies this team uses'


def _generate_requirements(title, seniority, count=8):
    """A scoreable requirement set for a role, must-haves first.

    Ordered with the non-negotiables at the top because that is the order a
    reader needs them in, and because ``hr.score_candidate`` reports the
    breakdown in list order.
    """
    family = _role_family(title)
    years = _SENIORITY_YEARS.get(seniority, 3)
    library = _REQUIREMENT_LIBRARY.get(family, _REQUIREMENT_LIBRARY['general'])

    rows = []
    for text, weight, must_have in library:
        filled = text.format(years=max(1, years), stack=_stack_for(title))
        if years <= 1 and 'years' in filled and must_have:
            filled = filled.replace(f'{max(1, years)} years building',
                                    'Some practical exposure to building')
        rows.append({'text': filled, 'weight': weight, 'must_have': must_have})

    # Generic lines top the set up when the role library is short. They are
    # never must-haves: sorting puts must-haves first, so a generic line
    # marked non-negotiable would displace a role-specific one and then block
    # a candidate on 'working with people outside your own team'.
    if family != 'general':
        for text, weight, _must_have in _REQUIREMENT_LIBRARY['general'][2:]:
            filled = text.format(years=max(1, years), stack=_stack_for(title))
            if all(row['text'] != filled for row in rows):
                rows.append({'text': filled, 'weight': weight, 'must_have': False})

    rows.sort(key=lambda row: (not row['must_have'], -row['weight']))
    return rows[:max(3, min(int(count or 8), 15))], family


_RESPONSIBILITY_LIBRARY = {
    'engineering': (
        'Build, test and ship features in the team codebase',
        'Review colleagues code and act on review of your own',
        'Investigate and fix defects reported by users and monitoring',
        'Contribute to technical design discussions and record the decisions',
        'Keep the automated test suite meaningful as the code changes',
    ),
    'data': (
        'Build and maintain the pipelines the business reports from',
        'Answer analytical questions and show how the number was reached',
        'Own the accuracy of a defined set of reports',
        'Work with the teams that produce the source data',
    ),
    'management': (
        'Plan and sequence the team work and report progress honestly',
        'Run one-to-ones, set expectations and manage performance',
        'Hire into the team and onboard new starters',
        'Remove blockers and escalate the ones you cannot remove',
    ),
    'support': (
        'Respond to customer requests within the agreed service levels',
        'Diagnose problems and resolve or escalate them with full context',
        'Keep the help documentation current from what you learn',
        'Flag recurring problems as a product signal',
    ),
    'marketing': (
        'Produce content for the channels this role owns',
        'Plan campaigns and report what they achieved',
        'Keep the messaging consistent with the brand guide',
    ),
    'people': (
        'Run recruitment end to end for the roles you are given',
        'Support managers on employee matters with discretion',
        'Keep employee records and onboarding accurate and current',
    ),
    'finance': (
        'Own parts of the month-end close and reconciliation',
        'Maintain accurate records in the accounting system',
        'Report variances to the budget owners with an explanation',
    ),
    'general': (
        'Deliver the work this role owns, end to end',
        'Work with the teams this role depends on',
        'Keep records of what you did so it can be repeated',
        'Raise problems early rather than absorbing them',
    ),
}


def _generate_responsibilities(title):
    family = _role_family(title)
    rows = list(_RESPONSIBILITY_LIBRARY.get(family, ()))
    for entry in _RESPONSIBILITY_LIBRARY['general']:
        if entry not in rows:
            rows.append(entry)
    return rows[:7]


_QUESTION_SHAPES = (
    ('Tell me about a piece of work that turned on {topic_noun}. What was the '
     'situation, what did you do yourself, and what was the outcome?',
     'A specific example with the actions the candidate took themselves named, and a '
     'result they can quantify. Watch for answers that describe what the team did.'),
    ('Walk me through the most demanding piece of work you have done involving '
     '{topic_noun}.',
     'Real depth: the constraint they worked under, the option they rejected, and why. '
     'Vagueness here usually means exposure rather than ownership.'),
    ('How would you approach {topic_noun} on a project where the previous approach had '
     'already failed once?',
     'Diagnosis before solution. A good answer asks what failed and why before '
     'proposing anything.'),
    ('What is the hardest problem you have solved involving {topic_noun}, and what '
     'would you do differently now?',
     'Willingness to name their own mistake. Candidates who cannot are usually the '
     'ones who repeat it.'),
    ('Describe a decision you made about {topic_noun} that you later reversed.',
     'Evidence they notice being wrong and act on it, and that they treat a reversal '
     'as information rather than a defeat.'),
    ('If you joined and found {topic_noun} in a worse state than described, what would '
     'your first two weeks look like?',
     'A plan that starts with finding out, not with rebuilding. Listen for who they '
     'would talk to.'),
)

_GENERAL_QUESTIONS = (
    ('Why this role, and why now?',
     'A reason connected to the actual job rather than to leaving the last one.'),
    ('Tell me about a disagreement with a colleague over how to do something.',
     'How the disagreement ended and whether the working relationship survived it.'),
    ('What have you learned in the last six months, and how?',
     'Evidence of learning by doing rather than by intending to.'),
    ('You have more work than time. How do you decide, and who do you tell?',
     'A stated method and, crucially, that somebody is told before the deadline slips.'),
    ('What do you need from a manager to do your best work?',
     'Self-knowledge, and whether what they need is something this team can offer.'),
    ('What would you want to ask us that we have not covered?',
     'What they care about. No questions at all is worth noting.'),
)


def _topic_from(requirement_text):
    """A short phrase to put inside a question shape.

    Requirement lines are written to be scored, not spoken, so the checkable
    terms are pulled out and the boilerplate dropped. Asking a candidate to
    'tell me about a time you had to 5 years building and maintaining
    production software' is the failure mode this exists to prevent.
    """
    keywords = _keywords(requirement_text, limit=3)
    named = [_display(key) for key in keywords if key in _SKILL_KEYS]
    if named:
        return ' and '.join(named[:2])
    cleaned = re.sub(r'^\s*\d+\+?\s*years?\s*(of\s*)?', '', requirement_text or '',
                     flags=re.IGNORECASE)
    cleaned = re.sub(r'^(strong|solid|proven|demonstrated|excellent|good)\s+', '',
                     cleaned.strip(), flags=re.IGNORECASE)
    return ' '.join(cleaned.split())[:90] or 'this area'


def _generate_questions(requirements, round_name, count=8):
    """Competency questions derived from the requirement set."""
    rows = []
    lines = [row['text'] for row in _normalise_requirements(requirements)
             if not _protected_attribute(row['text'])]

    for index, line in enumerate(lines):
        shape, guidance = _QUESTION_SHAPES[index % len(_QUESTION_SHAPES)]
        topic = _topic_from(line)
        rows.append({
            'question': shape.format(topic_noun=topic),
            'competency': line[:160],
            'what_to_look_for': guidance,
        })
        if len(rows) >= count:
            break

    for question, guidance in _GENERAL_QUESTIONS:
        if len(rows) >= count:
            break
        rows.append({'question': question,
                     'competency': f'General fit ({round_name})',
                     'what_to_look_for': guidance})

    return rows[:max(3, int(count or 8))]


_STANDARD_CRITERIA = (
    ('Communication', 'Explains their own work so a listener follows it, and asks when '
                      'the question is unclear.'),
    ('Problem solving', 'Diagnoses before proposing. Distinguishes what they know from '
                        'what they are guessing.'),
    ('Ownership', 'Took responsibility for an outcome, including when it went wrong.'),
    ('Collaboration', 'Worked with people who disagreed with them and kept the '
                      'relationship intact.'),
    ('Role fit', 'The work described in this opening is work they would actually want '
                 'to do for the next two years.'),
)

_SCALE = '1 to 5, where 1 is no evidence, 3 is adequate evidence, 5 is strong evidence'


def _generate_evaluation_form(requirements):
    """Scoring criteria for an interviewer, requirement-led then general."""
    rows = []
    for row in _normalise_requirements(requirements):
        if _protected_attribute(row['text']):
            continue
        rows.append({
            'criterion': row['text'][:160],
            'scale': _SCALE,
            'guidance': ('Non-negotiable for this role. Score 1 unless the candidate '
                         'gave a concrete example.' if row['must_have'] else
                         'Weighted requirement. Score from the example given, not from '
                         'the claim made.'),
        })
        if len(rows) >= 8:
            break
    for criterion, guidance in _STANDARD_CRITERIA:
        rows.append({'criterion': criterion, 'scale': _SCALE, 'guidance': guidance})
    return rows


# ---------------------------------------------------------------------------
# Onboarding
#
# The offsets are the substance of this template. A checklist that says
# 'order the laptop' without saying when is a list of things that will all be
# discovered on the first morning, which is the failure this is designed
# around: contracts and equipment run before day one, access lands on day one,
# training fills the first week and compliance the second.
# ---------------------------------------------------------------------------

_ONBOARDING_TEMPLATE = (
    ('paperwork', -10, 'Issue the employment contract for signature',
     'People Operations',
     'Send the contract, the position description and the start details together.'),
    ('paperwork', -7, 'Collect the signed contract, tax declaration and '
                      'superannuation choice', 'People Operations',
     'Chase this before day one. It is the item that most often delays the first pay.'),
    ('paperwork', -5, 'Collect payroll bank details and an emergency contact',
     'Payroll', 'Confirm the pay cycle and the first pay date in writing.'),
    ('paperwork', -3, 'Confirm employment eligibility documentation is on file',
     'People Operations',
     'A record-keeping obligation. Record that it was checked, not the document itself.'),
    ('equipment', -5, 'Order the laptop, monitor, headset and any role-specific kit',
     'IT Support', 'Order early. Lead times, not budget, are what miss a start date.'),
    ('equipment', -2, 'Build the machine and install the standard toolset',
     'IT Support', 'Ready and tested two days out, so a failed build is recoverable.'),
    ('equipment', -1, 'Prepare the desk, access pass and parking if required',
     'Office Management', ''),
    ('access', -1, 'Create the email account and directory entry', 'IT Support',
     'Create it the day before so the calendar invitations for day one can be sent.'),
    ('access', 0, 'Grant access to the core systems this role needs',
     'IT Support', 'Least privilege. Add access as the work requires it.'),
    ('access', 0, 'Add to the department channels, calendars and mailing lists',
     'IT Support', ''),
    ('access', 1, 'Set up multi-factor authentication and the password manager',
     'IT Support', 'Do this before the new starter has anything worth protecting.'),
    ('introduction', 0, 'Welcome, workspace tour and the day-one agenda',
     'Hiring Manager',
     'Somebody named is expecting them at a stated time. An unmet new starter on '
     'day one is the detail people remember for years.'),
    ('introduction', 0, 'Introduce the immediate team', 'Hiring Manager', ''),
    ('introduction', 1, 'Assign an onboarding buddy outside the reporting line',
     'Hiring Manager',
     'Someone to ask the questions they would not ask their manager.'),
    ('introduction', 2, 'One-to-one with the manager: expectations for the first '
                        '30 days', 'Hiring Manager',
     'Written down and agreed, so the 30-day review has something to measure.'),
    ('introduction', 4, 'Meet the three teams this role works with most',
     'Hiring Manager', ''),
    ('training', 1, 'Walk through the handbook and where the policies live',
     'People Operations',
     'Show them how to find an answer rather than reciting the answers.'),
    ('training', 3, 'Role-specific systems and process training', 'Hiring Manager', ''),
    ('training', 5, 'First real piece of work, reviewed with the manager',
     'Hiring Manager',
     'Small, real and finishable inside week one. Nothing settles a new starter '
     'faster than having shipped something.'),
    ('training', 19, 'Thirty-day review against the agreed expectations',
     'Hiring Manager', ''),
    ('compliance', 4, 'Complete the work health and safety induction', 'Compliance',
     ''),
    ('compliance', 9, 'Complete the privacy and data handling module', 'Compliance',
     'Before they have access to anything sensitive, not after.'),
    ('compliance', 11, 'Acknowledge the code of conduct', 'Compliance', ''),
    ('compliance', 12, 'Confirm any role-specific compliance checks are complete',
     'Compliance', ''),
    ('introduction', 9, 'Buddy check-in at the end of week two',
     'Onboarding Buddy',
     'Ask what is still confusing. Week two is when the polite answers stop.'),
)

_ROLE_EXTRAS = (
    (('engineer', 'developer', 'data', 'software', 'analyst', 'devops'), (
        ('access', 0, 'Grant repository and CI access', 'IT Support',
         'Read access on day one, write access once the environment runs.'),
        ('training', 2, 'Get the development environment running locally',
         'Hiring Manager',
         'A day-two milestone. If it takes longer than that, the setup '
         'documentation is the problem, not the new starter.'),
        ('training', 6, 'Ship a first small change through the full review and '
                        'deploy path', 'Hiring Manager', ''),
    )),
    (('support', 'service', 'customer', 'helpdesk'), (
        ('access', 0, 'Grant helpdesk and knowledge base access', 'IT Support', ''),
        ('training', 2, 'Shadow a colleague on live tickets', 'Team Lead', ''),
        ('training', 6, 'Handle first tickets with a reviewer on every reply',
         'Team Lead', ''),
    )),
    (('sales', 'account', 'business development'), (
        ('access', 0, 'Grant CRM access and assign a territory or segment',
         'IT Support', ''),
        ('training', 3, 'Pipeline and product walkthrough', 'Sales Lead', ''),
    )),
    (('marketing', 'content', 'communications'), (
        ('access', 0, 'Grant access to the brand kit, CMS and analytics',
         'IT Support', ''),
        ('training', 3, 'Brand, tone and approval process walkthrough',
         'Marketing Lead', ''),
    )),
    (('manager', 'lead', 'head', 'director'), (
        ('access', 1, 'Grant leave approval and timesheet approval rights',
         'People Operations', ''),
        ('introduction', 3, 'Schedule recurring one-to-ones with each report',
         'Hiring Manager', ''),
        ('training', 7, 'Walk through the performance and remuneration cycle',
         'People Operations', ''),
    )),
)


def _onboarding_rows(role_hint, include_categories=None):
    """The checklist for one role, filtered and ordered."""
    wanted = {str(entry).strip().lower() for entry in (include_categories or [])
              if str(entry).strip()}
    rows = list(_ONBOARDING_TEMPLATE)

    low = (role_hint or '').lower()
    for markers, extras in _ROLE_EXTRAS:
        if any(marker in low for marker in markers):
            rows.extend(extras)

    if wanted:
        rows = [row for row in rows if row[0] in wanted]

    rows.sort(key=lambda row: (row[1], row[0]))
    return rows


# ---------------------------------------------------------------------------
# Performance reviews and feedback
# ---------------------------------------------------------------------------

_REVIEW_SECTIONS = (
    ('objectives', 'Delivery against objectives',
     'What was agreed for this period, and what actually happened. Name the work.'),
    ('quality', 'Quality of work',
     'Did the work hold up? Rework, defects, and how the person responded to them.'),
    ('collaboration', 'Collaboration and communication',
     'How they work with people who depend on them, and with people who disagree.'),
    ('ownership', 'Ownership and initiative',
     'What they picked up without being asked, and what they escalated in time.'),
    ('growth', 'Growth since the last review',
     'What they can do now that they could not before, with an example.'),
    ('goals', 'Goals for the next period',
     'Two or three, each with something observable that would count as done.'),
    ('support', 'Support needed from the manager',
     'What only the manager can change. Leaving this blank is itself a finding.'),
)


def _generate_review_sections(custom=None):
    if custom:
        rows = []
        for index, entry in enumerate(_plain_list(custom)):
            rows.append({'key': re.sub(r'\W+', '_', entry.lower())[:40] or f'section_{index}',
                         'title': entry, 'prompt': 'Write your assessment.',
                         'scale': _SCALE})
        return rows
    return [{'key': key, 'title': title, 'prompt': prompt, 'scale': _SCALE}
            for key, title, prompt in _REVIEW_SECTIONS]


_FEEDBACK_THEMES = (
    ('Communication', ('communicat', 'explain', 'clear', 'clarity', 'listen',
                       'written', 'writes', 'presentation', 'articulate', 'update')),
    ('Collaboration', ('team', 'collaborat', 'help', 'support', 'peer', 'colleague',
                       'cross-functional', 'stakeholder')),
    ('Delivery and reliability', ('deliver', 'deadline', 'on time', 'late', 'ship',
                                  'commit', 'reliab', 'consistent', 'follow through',
                                  'follow-through')),
    ('Quality of work', ('quality', 'bug', 'defect', 'careful', 'thorough', 'detail',
                         'accuracy', 'rework', 'review', 'test')),
    ('Ownership and initiative', ('initiative', 'ownership', 'proactive', 'volunteer',
                                  'stepped up', 'drive', 'self-starter', 'escalat')),
    ('Technical skill', ('technical', 'skill', 'depth', 'architecture', 'design',
                         'code', 'expertise', 'knowledge')),
    ('Leadership and mentoring', ('mentor', 'coach', 'guide', 'junior', 'onboard',
                                  'teach', 'leadership')),
    ('Customer focus', ('customer', 'client', 'user', 'account')),
    ('Workload and prioritisation', ('priorit', 'workload', 'capacity', 'overload',
                                     'stretched', 'time management', 'juggl',
                                     'spread thin')),
)

_POSITIVE_MARKERS = ('excellent', 'great', 'strong', 'outstanding', 'reliable',
                     'helpful', 'clear', 'improved', 'best', 'impressive',
                     'appreciate', 'good', 'consistently', 'always', 'exceeded',
                     'well', 'thorough', 'calm', 'trusted')
_NEGATIVE_MARKERS = ('needs', 'need to', 'should', 'lacks', 'lacking', 'struggle',
                     'missed', 'late', 'unclear', 'confus', 'inconsistent',
                     'concern', 'difficult', 'problem', 'fails', 'failed', 'slow',
                     'improve', 'sometimes', 'occasionally', 'not enough', 'could be',
                     'would like', 'too much', 'overload')

_THEME_ACTIONS = {
    'Communication': 'agree a written weekly update to the people who depend on this '
                     'work, and review it together after a month',
    'Collaboration': 'pair them with the team that raised the feedback on one shared '
                     'piece of work rather than discussing it in the abstract',
    'Delivery and reliability': 'agree what gets flagged and to whom the moment a date '
                                'is at risk, so a slip is news before the deadline',
    'Quality of work': 'add a review step on the specific kind of work the feedback '
                       'names, and remove it once two clean cycles have passed',
    'Ownership and initiative': 'hand over one outcome end to end, with the decision '
                                'rights that go with it',
    'Technical skill': 'name one depth area and give them a real piece of work in it '
                       'with a reviewer attached',
    'Leadership and mentoring': 'give them one person to onboard, with a fortnightly '
                                'check-in on how that is going',
    'Customer focus': 'put them in front of two customer conversations they would not '
                      'normally attend',
    'Workload and prioritisation': 'cut the list with them rather than asking them to '
                                   'prioritise it, and confirm what is now not being '
                                   'done',
}


def _summarise_feedback(text):
    """Themes, strengths, development areas and one action.

    Sentence-level rather than document-level because a single review contains
    both praise and criticism, and a document-level verdict would average them
    into something nobody wrote.
    """
    sentences = [' '.join(part.split()) for part in
                 re.split(r'(?<=[.!?])\s+|\n+', text or '') if part.strip()]

    themes, strengths, development = {}, [], []
    for sentence in sentences:
        low = sentence.lower()
        positive = sum(1 for marker in _POSITIVE_MARKERS if marker in low)
        negative = sum(1 for marker in _NEGATIVE_MARKERS if marker in low)
        tone = 'strength' if positive > negative else (
            'development' if negative > positive else 'neutral')

        hit_theme = ''
        for name, markers in _FEEDBACK_THEMES:
            if any(marker in low for marker in markers):
                hit_theme = name
                break
        if hit_theme:
            entry = themes.setdefault(hit_theme, {'theme': hit_theme, 'mentions': 0,
                                                  'positive': 0, 'negative': 0,
                                                  'examples': []})
            entry['mentions'] += 1
            entry['positive'] += 1 if tone == 'strength' else 0
            entry['negative'] += 1 if tone == 'development' else 0
            if len(entry['examples']) < 3:
                entry['examples'].append(sentence[:200])

        if tone == 'strength' and len(strengths) < 6:
            strengths.append(sentence[:200])
        elif tone == 'development' and len(development) < 6:
            development.append(sentence[:200])

    ranked = sorted(themes.values(), key=lambda row: -row['mentions'])
    weakest = max(themes.values(), key=lambda row: row['negative'], default=None)

    if weakest and weakest['negative']:
        action = (f"On {weakest['theme'].lower()}: "
                  f"{_THEME_ACTIONS.get(weakest['theme'], 'agree one observable change and a date to review it')}.")
    elif ranked:
        action = (f"Nothing in this feedback needs correcting. Tell them so explicitly, "
                  f"naming {ranked[0]['theme'].lower()}, and agree one stretch goal.")
    else:
        action = ('There is not enough in this text to act on. Ask the people who gave '
                  'it for one specific example each.')

    return {'themes': ranked, 'strengths': strengths,
            'development_areas': development, 'recommended_action': action,
            'sentences_read': len(sentences)}


# ===========================================================================
# The knowledge base
#
# Policy answers are searched, never remembered. An improvised policy quoted
# back to an employee six months later is a genuine problem, so these tools
# report what the documents say and name them, or say plainly that nothing was
# found and what ought to be added.
#
# The import is local and guarded because marketing/knowledge.py is a separate
# module owned elsewhere in the project. If it is absent this module must still
# import, or the entire HR employee disappears from the roster over a missing
# search helper.
# ===========================================================================

# The doc_type values in models_knowledge that an HR answer may rest on.
_POLICY_DOC_TYPES = ('policy', 'handbook', 'process', 'faq')


def _knowledge_search(query, limit=5, doc_types=None, strict=False):
    """Search the knowledge base. Returns ``(hits, problem)``.

    ``problem`` is a sentence to put in the tool result when the search could
    not run, so the reader learns that the knowledge base was unavailable
    rather than that no policy exists. Those are very different answers.
    """
    try:
        from ..knowledge import search
    except ImportError:
        return [], ('The knowledge base module is not installed in this deployment, so '
                    'no document search could be run.')
    except Exception as exc:  # noqa: BLE001
        return [], f'The knowledge base could not be loaded ({exc}).'

    try:
        hits = list(search(query, limit=max(1, int(limit or 5))) or [])
    except Exception as exc:  # noqa: BLE001
        return [], f'The knowledge base search failed ({exc}).'

    if doc_types:
        wanted = {str(kind).lower() for kind in doc_types}
        narrowed = [hit for hit in hits
                    if str(getattr(getattr(hit, 'document', None), 'doc_type', '')
                           ).lower() in wanted]
        # A policy answer may fall back to a document of another type rather
        # than answering nothing, because a product page that states the rule
        # is still a citation. A search that promised to look only at HR
        # documents may not: returning something else under that label would
        # misrepresent where the answer came from.
        if narrowed or strict:
            hits = narrowed

    return hits, ''


def _citation(hit):
    """One search hit flattened into something quotable."""
    document = getattr(hit, 'document', None)
    return {
        'title': str(getattr(document, 'title', '') or 'untitled document'),
        'url': str(getattr(document, 'url', '') or ''),
        'doc_type': str(getattr(document, 'doc_type', '') or ''),
        'snippet': ' '.join(str(getattr(hit, 'snippet', '') or '').split())[:600],
        'score': float(getattr(hit, 'score', 0) or 0),
    }


def _cited_answer(question, hits, problem, *, subject='policy'):
    """The standard shape of a knowledge-grounded answer.

    Deliberately uniform across the three policy tools. A reader should not
    have to work out whether an answer was sourced: either quotations with
    document names follow, or the first line says nothing was found.
    """
    if problem:
        return (f'{problem} No answer to "{question}" can be given from documents, and '
                f'one should not be improvised. Ask the Knowledge and Research '
                f'employee, or have the {subject} document loaded into the knowledge '
                f'base.'), []

    citations = [_citation(hit) for hit in hits]
    if not citations:
        return (f'The knowledge base has nothing relevant to "{question}". No answer '
                f'should be given from memory. What is missing is a {subject} document '
                f'covering this: recommend that one be written and added, and say so '
                f'to whoever asked rather than approximating an answer.'), []

    lines = [f'Found {len(citations)} relevant document'
             f'{"s" if len(citations) > 1 else ""} for "{question}".']
    for index, row in enumerate(citations, start=1):
        where = f' ({row["url"]})' if row['url'] else ''
        kind = f' [{row["doc_type"]}]' if row['doc_type'] else ''
        lines.append(f'{index}. "{row["title"]}"{kind}{where}: "{row["snippet"]}"')
    lines.append('Answer from these quotations only, and name the document each claim '
                 'came from. Where they do not cover part of the question, say so.')
    return '\n'.join(lines), citations


# ===========================================================================
# Recording what an approved action actually did
#
# Execution is the only place in this module that touches an integration, and
# every execution leaves a row behind. The status carried on that row is taken
# from the CallResult, never assumed: a simulated invitation is recorded as
# simulated, because an audit trail that cannot distinguish the two is worse
# than none.
# ===========================================================================

def _external_id(result):
    data = result.data or {}
    for key in ('id', 'message_id', 'event_id', 'ts', 'file_id', 'external_id'):
        if data.get(key):
            return str(data[key])[:200]
    return ''


def _record_message(action, result, *, channel, recipient, subject='', body='',
                    metadata=None):
    from ..models_platform import OutboundMessage

    if not result.ok:
        status = 'failed'
    else:
        status = 'simulated' if result.demo else 'sent'

    return OutboundMessage.objects.create(
        channel=channel, recipient=str(recipient or '')[:400],
        subject=str(subject or '')[:300], body=body or '',
        status=status, external_id=_external_id(result),
        external_url=str((result.data or {}).get('url') or '')[:400],
        error_message=result.error or '',
        agent=action.agent, action=action, integration=action.integration,
        metadata=metadata or {})


def _executed(result, done, prepared_but_not_sent=''):
    """One ToolResult shape for every executor.

    ``demo`` is threaded straight from the CallResult so the layer above can
    label the outcome honestly without inspecting anything.
    """
    if not result.ok:
        return ToolResult(ok=False, demo=result.demo, error=result.error,
                          text=f'{prepared_but_not_sent or done} did not go through: '
                               f'{result.error}',
                          data=result.data or {})
    marker = ' (simulated -- the integration is not connected, so nothing left the '\
             'building)' if result.demo else ''
    return ToolResult(ok=True, demo=result.demo,
                      text=f'{done}{marker}. {result.summary}'.strip(),
                      data=result.data or {})


# ===========================================================================
# GROUP: RECRUITMENT
# ===========================================================================

@tool(
    name='hr.create_job_opening',
    title='Create a job opening',
    description=(
        'Record a new role the company is hiring for. Use this whenever a job '
        'description, vacancy or opening is asked for, and write the real content '
        'rather than a placeholder. Requirements may be given as plain strings or as '
        '{"text","weight","must_have"} objects; either way they are stored in the '
        'scoreable form that hr.score_candidate reads. Acts immediately on our own '
        'database; nothing is published anywhere.'),
    group='Recruitment',
    agent_types=('hr',),
    icon='fa-briefcase',
    parameters=_schema({
        'title': _p('string', 'The role title, e.g. "Senior Django Developer".'),
        'department': _p('string', 'Owning department.'),
        'location': _p('string', 'City and country, or "Remote".'),
        'seniority': _p('string', 'One of intern, junior, mid, senior, lead, '
                                  'principal, manager, director.'),
        'employment_type': _p('string', 'One of full_time, part_time, contract, '
                                        'internship.'),
        'is_remote': _p('boolean', 'True where the role can be done remotely.'),
        'summary': _p('string', 'Two or three sentences anybody would understand.'),
        'description': _p('string', 'The full advertisement text.'),
        'responsibilities': dict(_STRING_LIST,
                                 description='What the person will actually do.'),
        'requirements': {'type': 'array',
                         'description': ('Scoreable requirements. Strings, or objects '
                                         'with text, weight (1-5) and must_have.'),
                         'items': {'type': ['string', 'object']}},
        'nice_to_have': dict(_STRING_LIST, description='Desirable, not required.'),
        'salary_min': _p('integer', 'Bottom of the band, whole currency units.'),
        'salary_max': _p('integer', 'Top of the band.'),
        'hiring_manager': _p('string', 'Who owns the hire.'),
        'openings': _p('integer', 'How many people are being hired. Defaults to 1.'),
    }, required=('title',)),
)
def create_job_opening(ctx, title, department='', location='', seniority='mid',
                       employment_type='full_time', is_remote=False, summary='',
                       description='', responsibilities=None, requirements=None,
                       nice_to_have=None, salary_min=None, salary_max=None,
                       hiring_manager='', openings=1):
    from ..models_hr import JobOpening

    clean_title = str(title or '').strip()
    if not clean_title:
        return ToolResult(ok=False, text='A job opening needs a title.',
                          error='title was empty')

    normalised = _normalise_requirements(requirements)
    generated = False
    if not normalised:
        normalised, _family = _generate_requirements(clean_title, seniority)
        generated = True

    duties = _plain_list(responsibilities) or _generate_responsibilities(clean_title)

    opening = JobOpening.objects.create(
        title=clean_title[:160],
        department=str(department or '')[:100],
        location=str(location or '')[:140],
        employment_type=str(employment_type or 'full_time'),
        seniority=str(seniority or 'mid'),
        is_remote=bool(is_remote),
        summary=str(summary or ''),
        description=str(description or ''),
        responsibilities=duties,
        requirements=normalised,
        nice_to_have=_plain_list(nice_to_have),
        salary_min=_int(salary_min),
        salary_max=_int(salary_max),
        openings=max(1, _int(openings, 1) or 1),
        hiring_manager=str(hiring_manager or '')[:140],
        status='draft',
        created_by_agent=_agent(ctx),
        created_by=getattr(ctx, 'user', None) if getattr(
            getattr(ctx, 'user', None), 'pk', None) else None,
    )

    must_haves = sum(1 for row in normalised if row['must_have'])
    origin = ('generated from the title and seniority because none were supplied'
              if generated else 'as supplied')
    return ToolResult(
        ok=True,
        text=(f'Created job opening #{opening.pk} "{opening.title}" in '
              f'{opening.department or "no department recorded"}, status draft, with '
              f'{len(normalised)} scoreable requirements ({must_haves} must-have, '
              f'{origin}) and {len(duties)} responsibilities. '
              f'{"Salary band " + opening.salary_range + ". " if opening.salary_range else ""}'
              f'Candidates can now be scored against it with hr.score_candidate. '
              f'Set status to open with hr.update_job_opening when it is ready to '
              f'advertise.'),
        data={'job_opening_id': opening.pk, 'title': opening.title,
              'status': opening.status, 'requirements': normalised,
              'responsibilities': duties,
              'requirements_generated': generated},
        subject_label='marketing.jobopening', subject_id=opening.pk)


@tool(
    name='hr.update_job_opening',
    title='Update a job opening',
    description=(
        'Change fields on an existing opening, including its status (draft, open, '
        'on_hold, filled, closed). Only the arguments given are changed; everything '
        'else is left as it is. Use this to open a draft for applications, put a '
        'search on hold, or sharpen the requirements after a first round of '
        'screening.'),
    group='Recruitment',
    agent_types=('hr',),
    icon='fa-pen-to-square',
    parameters=_schema({
        'job_opening_id': _p('integer', 'Which opening to change.'),
        'title': _p('string', 'New role title.'),
        'department': _p('string', 'New department.'),
        'location': _p('string', 'New location.'),
        'seniority': _p('string', 'intern, junior, mid, senior, lead, principal, '
                                  'manager or director.'),
        'employment_type': _p('string', 'full_time, part_time, contract or internship.'),
        'is_remote': _p('boolean', 'Whether the role is remote.'),
        'summary': _p('string', 'New short summary.'),
        'description': _p('string', 'New full advertisement text.'),
        'responsibilities': dict(_STRING_LIST, description='Replaces the current list.'),
        'requirements': {'type': 'array',
                         'description': 'Replaces the current scoreable requirement set.',
                         'items': {'type': ['string', 'object']}},
        'nice_to_have': dict(_STRING_LIST, description='Replaces the current list.'),
        'benefits': dict(_STRING_LIST, description='Replaces the current list.'),
        'salary_min': _p('integer', 'Bottom of the band.'),
        'salary_max': _p('integer', 'Top of the band.'),
        'hiring_manager': _p('string', 'Who owns the hire.'),
        'openings': _p('integer', 'How many people are being hired.'),
        'status': _p('string', 'draft, open, on_hold, filled or closed.'),
        'target_start_date': _p('string', 'When the person is needed, as YYYY-MM-DD.'),
    }, required=('job_opening_id',)),
)
def update_job_opening(ctx, job_opening_id, title=None, department=None, location=None,
                       seniority=None, employment_type=None, is_remote=None,
                       summary=None, description=None, responsibilities=None,
                       requirements=None, nice_to_have=None, benefits=None,
                       salary_min=None, salary_max=None, hiring_manager=None,
                       openings=None, status=None, target_start_date=None):
    opening = _opening(job_opening_id)
    if opening is None:
        return _not_found('Job opening', job_opening_id, 'hr.list_job_openings')

    changed = []

    def apply(field, value, transform=lambda v: v):
        if value is None:
            return
        setattr(opening, field, transform(value))
        changed.append(field)

    apply('title', title, lambda v: str(v)[:160])
    apply('department', department, lambda v: str(v)[:100])
    apply('location', location, lambda v: str(v)[:140])
    apply('seniority', seniority, str)
    apply('employment_type', employment_type, str)
    apply('is_remote', is_remote, bool)
    apply('summary', summary, str)
    apply('description', description, str)
    apply('responsibilities', responsibilities, _plain_list)
    apply('requirements', requirements, _normalise_requirements)
    apply('nice_to_have', nice_to_have, _plain_list)
    apply('benefits', benefits, _plain_list)
    apply('salary_min', salary_min, _int)
    apply('salary_max', salary_max, _int)
    apply('hiring_manager', hiring_manager, lambda v: str(v)[:140])
    apply('openings', openings, lambda v: max(1, _int(v, 1) or 1))
    apply('status', status, str)

    if target_start_date is not None:
        parsed = _parse_date(target_start_date)
        if parsed is None:
            return ToolResult(
                ok=False, error='unreadable date',
                text=f'Could not read "{target_start_date}" as a date. Give it as '
                     f'YYYY-MM-DD. Nothing was changed.')
        opening.target_start_date = parsed
        changed.append('target_start_date')

    if not changed:
        return ToolResult(
            ok=False,
            text=f'Nothing was given to change on opening #{opening.pk} '
                 f'"{opening.title}". Name at least one field.',
            error='no fields supplied')

    opening.save()
    return ToolResult(
        ok=True,
        text=(f'Updated opening #{opening.pk} "{opening.title}": changed '
              f'{", ".join(changed)}. Status is now {opening.get_status_display()} with '
              f'{len(opening.requirement_lines)} requirements and '
              f'{opening.candidate_count} candidates attached.'),
        data={'job_opening_id': opening.pk, 'changed': changed,
              'status': opening.status},
        subject_label='marketing.jobopening', subject_id=opening.pk)


@tool(
    name='hr.list_job_openings',
    title='List job openings',
    description=(
        'List the openings with their ids, status, candidate counts and requirement '
        'counts. Call this before any tool that needs a job_opening_id rather than '
        'guessing one.'),
    group='Recruitment',
    agent_types=('hr',),
    icon='fa-list',
    reads_only=True,
    parameters=_schema({
        'status': _p('string', 'Filter by draft, open, on_hold, filled or closed. '
                               'Leave empty for all.'),
        'limit': _p('integer', 'How many to return. Defaults to 20.'),
    }),
)
def list_job_openings(ctx, status='', limit=20):
    from ..models_hr import JobOpening

    rows = JobOpening.objects.all()
    if status:
        rows = rows.filter(status=str(status))
    rows = list(rows[:max(1, min(_int(limit, 20) or 20, 100))])

    if not rows:
        where = f' with status {status}' if status else ''
        return ToolResult(
            ok=True,
            text=f'There are no job openings{where}. Create one with '
                 f'hr.create_job_opening.',
            data={'openings': [], 'count': 0})

    lines, payload = [], []
    for row in rows:
        lines.append(
            f'#{row.pk} {row.title} -- {row.get_status_display()}, '
            f'{row.department or "no department"}, '
            f'{row.location or "location not recorded"}, '
            f'{len(row.requirement_lines)} requirements, '
            f'{row.candidate_count} candidates ({row.shortlisted_count} shortlisted '
            f'or beyond)')
        payload.append({'id': row.pk, 'title': row.title, 'status': row.status,
                        'department': row.department, 'location': row.location,
                        'seniority': row.seniority,
                        'requirements': len(row.requirement_lines),
                        'candidates': row.candidate_count,
                        'shortlisted': row.shortlisted_count,
                        'salary_range': row.salary_range})

    return ToolResult(
        ok=True,
        text=f'{len(rows)} job opening{"s" if len(rows) > 1 else ""}:\n' +
             '\n'.join(lines),
        data={'openings': payload, 'count': len(payload)})


@tool(
    name='hr.get_job_opening',
    title='Read a job opening in full',
    description=(
        'The complete detail of one opening: summary, description, responsibilities, '
        'the scoreable requirements with their weights and must-have flags, benefits, '
        'salary band and candidate counts. Read this before writing an advertisement, '
        'interview questions or an evaluation form for the role.'),
    group='Recruitment',
    agent_types=('hr',),
    icon='fa-file-lines',
    reads_only=True,
    parameters=_schema({
        'job_opening_id': _p('integer', 'Which opening to read.'),
    }, required=('job_opening_id',)),
)
def get_job_opening(ctx, job_opening_id):
    opening = _opening(job_opening_id)
    if opening is None:
        return _not_found('Job opening', job_opening_id, 'hr.list_job_openings')

    requirements = _normalise_requirements(opening.requirements)
    requirement_text = '\n'.join(
        f'  {index}. {row["text"]} (weight {row["weight"]}'
        f'{", must-have" if row["must_have"] else ""})'
        for index, row in enumerate(requirements, start=1)) or '  none recorded'

    duties = '\n'.join(f'  - {line}' for line in
                       _plain_list(opening.responsibilities)) or '  none recorded'
    nice = ', '.join(_plain_list(opening.nice_to_have)) or 'none recorded'
    benefits = ', '.join(_plain_list(opening.benefits)) or 'none recorded'

    return ToolResult(
        ok=True,
        text=(f'Opening #{opening.pk} "{opening.title}"\n'
              f'Status: {opening.get_status_display()}. '
              f'{opening.get_seniority_display()}, '
              f'{opening.get_employment_type_display()}, '
              f'{"remote" if opening.is_remote else opening.location or "location not recorded"}. '
              f'Department: {opening.department or "not recorded"}. '
              f'Hiring manager: {opening.hiring_manager or "not recorded"}. '
              f'Openings: {opening.openings}. '
              f'Salary: {opening.salary_range or "not recorded"}.\n'
              f'Summary: {opening.summary or "not written yet"}\n'
              f'Responsibilities:\n{duties}\n'
              f'Requirements (these are what candidates are scored against):\n'
              f'{requirement_text}\n'
              f'Nice to have: {nice}\n'
              f'Benefits: {benefits}\n'
              f'Candidates: {opening.candidate_count}, of which '
              f'{opening.shortlisted_count} are shortlisted or further along.'),
        data={'job_opening_id': opening.pk, 'title': opening.title,
              'status': opening.status, 'department': opening.department,
              'location': opening.location, 'is_remote': opening.is_remote,
              'seniority': opening.seniority,
              'employment_type': opening.employment_type,
              'summary': opening.summary, 'description': opening.description,
              'responsibilities': _plain_list(opening.responsibilities),
              'requirements': requirements,
              'nice_to_have': _plain_list(opening.nice_to_have),
              'benefits': _plain_list(opening.benefits),
              'salary_range': opening.salary_range,
              'hiring_manager': opening.hiring_manager,
              'openings': opening.openings,
              'candidate_count': opening.candidate_count,
              'shortlisted_count': opening.shortlisted_count},
        subject_label='marketing.jobopening', subject_id=opening.pk)


@tool(
    name='hr.generate_job_requirements',
    title='Write a scoreable requirement set',
    description=(
        'Produce a weighted, must-have-flagged requirement set for a role. Call this '
        'when an opening has vague requirements or none, because screening reads this '
        'field and cannot score prose. Where a job_opening_id is given the set is '
        'saved onto that opening, replacing what was there; otherwise it is returned '
        'for review.'),
    group='Recruitment',
    agent_types=('hr',),
    icon='fa-list-check',
    parameters=_schema({
        'job_opening_id': _p('integer', 'Opening to write the requirements onto. '
                                        'Optional.'),
        'title': _p('string', 'Role title to derive from, when no opening is given.'),
        'seniority': _p('string', 'intern, junior, mid, senior, lead, principal, '
                                  'manager or director.'),
        'count': _p('integer', 'How many requirements. Defaults to 8.'),
    }),
)
def generate_job_requirements(ctx, job_opening_id=None, title='', seniority='mid',
                              count=8):
    opening = _opening(job_opening_id) if job_opening_id else None
    if job_opening_id and opening is None:
        return _not_found('Job opening', job_opening_id, 'hr.list_job_openings')

    role_title = str(title or '').strip() or (opening.title if opening else '')
    if not role_title:
        return ToolResult(
            ok=False, error='no role to work from',
            text='Give either a job_opening_id or a title, otherwise there is nothing '
                 'to derive requirements from.')

    level = str(seniority or '').strip() or (opening.seniority if opening else 'mid')
    rows, family = _generate_requirements(role_title, level, count)

    listing = '\n'.join(
        f'{index}. {row["text"]} (weight {row["weight"]}'
        f'{", must-have" if row["must_have"] else ""})'
        for index, row in enumerate(rows, start=1))

    if opening is not None:
        previous = len(opening.requirement_lines)
        opening.requirements = rows
        opening.save(update_fields=['requirements', 'updated_at'])
        head = (f'Wrote {len(rows)} scoreable requirements onto opening #{opening.pk} '
                f'"{opening.title}", replacing {previous}. Recognised as a '
                f'{family} role at {level} level.')
    else:
        head = (f'Drafted {len(rows)} scoreable requirements for "{role_title}" '
                f'({family} role, {level} level). Not saved anywhere -- pass a '
                f'job_opening_id, or hand them to hr.create_job_opening.')

    must_haves = sum(1 for row in rows if row['must_have'])
    return ToolResult(
        ok=True,
        text=(f'{head} {must_haves} are must-haves, which means a candidate missing '
              f'one cannot be recommended to advance however high the total.\n'
              f'{listing}'),
        data={'job_opening_id': opening.pk if opening else None,
              'requirements': rows, 'family': family, 'seniority': level,
              'must_haves': must_haves},
        subject_label='marketing.jobopening',
        subject_id=opening.pk if opening else 0)


# ===========================================================================
# GROUP: SCREENING
# ===========================================================================

@tool(
    name='hr.create_candidate',
    title='Record a candidate',
    description=(
        'Add a person to an opening, with their resume text where available. Paste the '
        'whole resume into resume_text: it is the only input screening has, and a '
        'candidate recorded without it can only ever score zero. Acts immediately on '
        'our own database.'),
    group='Screening',
    agent_types=('hr',),
    icon='fa-user-plus',
    parameters=_schema({
        'full_name': _p('string', 'The candidate name.'),
        'email': _p('string', 'Email address, needed before any invitation can be '
                              'prepared.'),
        'job_opening_id': _p('integer', 'Which opening they are being considered for.'),
        'resume_text': _p('string', 'The full text of the resume. The more complete, '
                                    'the more defensible the score.'),
        'phone': _p('string', 'Contact number.'),
        'current_title': _p('string', 'Their current job title.'),
        'current_company': _p('string', 'Their current employer.'),
        'years_experience': _p('number', 'Years of relevant experience, if known.'),
        'skills': dict(_STRING_LIST, description='Skills, if already extracted.'),
        'linkedin_url': _p('string', 'LinkedIn profile URL.'),
        'location': _p('string', 'Where they are based.'),
        'source': _p('string', 'application, referral, sourced, agency or drive.'),
    }, required=('full_name',)),
)
def create_candidate(ctx, full_name, email='', job_opening_id=None, resume_text='',
                     phone='', current_title='', current_company='',
                     years_experience=None, skills=None, linkedin_url='',
                     location='', source='application'):
    from ..models_hr import Candidate

    name = str(full_name or '').strip()
    if not name:
        return ToolResult(ok=False, text='A candidate needs a name.',
                          error='full_name was empty')

    opening = _opening(job_opening_id) if job_opening_id else None
    if job_opening_id and opening is None:
        return _not_found('Job opening', job_opening_id, 'hr.list_job_openings')

    body = str(resume_text or '')
    facts = _analyse_resume_text(body) if body.strip() else None

    resolved_skills = _plain_list(skills)
    if facts and not resolved_skills:
        resolved_skills = facts['skills']

    years = _decimal(years_experience)
    if years is None and facts and facts['years_experience'] is not None:
        years = _decimal(facts['years_experience'])

    title_now = str(current_title or '').strip()
    if not title_now and facts:
        title_now = facts['current_title']

    candidate = Candidate.objects.create(
        full_name=name[:160],
        email=str(email or '').strip()[:254],
        phone=str(phone or '')[:40],
        location=str(location or '')[:140],
        linkedin_url=str(linkedin_url or '')[:200],
        job_opening=opening,
        current_title=title_now[:160],
        current_company=str(current_company or '')[:160],
        years_experience=years,
        skills=resolved_skills,
        resume_text=body,
        resume_source='supplied to hr.create_candidate' if body else '',
        source=str(source or 'application'),
        status='new',
        created_by_agent=_agent(ctx),
    )

    notes = []
    if facts:
        notes.append(f'Read {facts["word_count"]} words of resume and found '
                     f'{len(facts["skills"])} known skills.')
        if facts['years_experience'] is not None:
            notes.append(f'Experience read as {facts["years_experience"]:g} years '
                         f'({facts["years_source"]}).')
    else:
        notes.append('No resume text was given, so this candidate cannot be scored '
                     'yet. Add it with hr.analyse_resume or hr.create_candidate again.')
    if not candidate.email:
        notes.append('No email address, so no invitation or decision can be prepared '
                     'until one is added.')
    if opening is not None:
        notes.append(f'Attached to opening #{opening.pk} "{opening.title}"; score with '
                     f'hr.score_candidate candidate_id={candidate.pk}.')
    else:
        notes.append('Not attached to an opening, so there is nothing to score against '
                     'yet.')

    return ToolResult(
        ok=True,
        text=f'Recorded candidate #{candidate.pk} {candidate.full_name}. '
             + ' '.join(notes),
        data={'candidate_id': candidate.pk, 'full_name': candidate.full_name,
              'email': candidate.email, 'status': candidate.status,
              'job_opening_id': opening.pk if opening else None,
              'skills': resolved_skills,
              'years_experience': float(years) if years is not None else None},
        subject_label='marketing.candidate', subject_id=candidate.pk)


@tool(
    name='hr.analyse_resume',
    title='Parse a resume into structured facts',
    description=(
        'Extract skills, duration of experience, job titles, education, '
        'certifications, contact details, date ranges and any employment breaks of six '
        'months or more from resume text. Purely mechanical extraction, so the result '
        'says how each fact was arrived at. Where a candidate_id is given, the skills, '
        'years and current title found are written onto the candidate. Use this before '
        'scoring when a candidate record is thin.'),
    group='Screening',
    agent_types=('hr',),
    icon='fa-file-magnifying-glass',
    parameters=_schema({
        'candidate_id': _p('integer', 'Candidate to read and update. Optional -- '
                                      'their stored resume text is used when '
                                      'resume_text is not given.'),
        'resume_text': _p('string', 'Resume text to parse. Overrides what is stored.'),
    }),
)
def analyse_resume(ctx, candidate_id=None, resume_text=''):
    candidate = _candidate(candidate_id) if candidate_id else None
    if candidate_id and candidate is None:
        return _not_found('Candidate', candidate_id, 'hr.list_candidates')

    body = str(resume_text or '') or (candidate.resume_text if candidate else '')
    if not body.strip():
        target = f'candidate #{candidate.pk} {candidate.full_name}' if candidate \
            else 'the arguments given'
        return ToolResult(
            ok=False, error='no resume text',
            text=f'There is no resume text on {target}, so there is nothing to parse. '
                 f'Pass the resume in resume_text.')

    facts = _analyse_resume_text(body)
    summary = _resume_summary(facts)
    updated = []

    if candidate is not None:
        if not candidate.resume_text and resume_text:
            candidate.resume_text = body
            candidate.resume_source = 'supplied to hr.analyse_resume'
            updated.append('resume_text')
        merged = list(candidate.skills or [])
        for skill in facts['skills']:
            if skill not in merged:
                merged.append(skill)
        if merged != list(candidate.skills or []):
            candidate.skills = merged
            updated.append('skills')
        if facts['years_experience'] is not None:
            years = _decimal(facts['years_experience'])
            if years is not None and years != candidate.years_experience:
                candidate.years_experience = years
                updated.append('years_experience')
        if facts['current_title'] and not candidate.current_title:
            candidate.current_title = facts['current_title'][:160]
            updated.append('current_title')
        if updated:
            candidate.save()

    head = (f'Parsed the resume for candidate #{candidate.pk} {candidate.full_name}. '
            if candidate else 'Parsed the resume text supplied. ')
    tail = (f' Updated on the candidate record: {", ".join(updated)}.' if updated
            else (' Nothing on the candidate record needed changing.' if candidate
                  else ' Nothing was saved, because no candidate_id was given.'))

    return ToolResult(
        ok=True, text=head + summary + tail,
        data={'candidate_id': candidate.pk if candidate else None,
              'facts': facts, 'summary': summary, 'updated_fields': updated},
        subject_label='marketing.candidate',
        subject_id=candidate.pk if candidate else 0)


@tool(
    name='hr.score_candidate',
    title='Score a candidate against an opening',
    description=(
        'Score one candidate against the opening requirements, one requirement at a '
        'time, weighting must-haves double. Produces a 0-100 total, a per-requirement '
        'breakdown quoting the evidence found in the resume, strengths, gaps and a '
        'recommendation of advance, hold or reject. Entirely deterministic, so the same '
        'inputs always give the same score. Requirement lines that refer to age, '
        'gender, nationality, marital status, health or religion are refused and '
        'excluded from the total. Call this before shortlisting anybody.'),
    group='Screening',
    agent_types=('hr',),
    icon='fa-scale-balanced',
    parameters=_schema({
        'candidate_id': _p('integer', 'The candidate to score.'),
        'job_opening_id': _p('integer', 'The opening to score against. Defaults to '
                                        'the one the candidate is attached to.'),
    }, required=('candidate_id',)),
)
def score_candidate(ctx, candidate_id, job_opening_id=None):
    from ..models_hr import CandidateEvaluation

    candidate = _candidate(candidate_id)
    if candidate is None:
        return _not_found('Candidate', candidate_id, 'hr.list_candidates')

    opening = _opening(job_opening_id) if job_opening_id else candidate.job_opening
    if job_opening_id and opening is None:
        return _not_found('Job opening', job_opening_id, 'hr.list_job_openings')
    if opening is None:
        return ToolResult(
            ok=False, error='no opening to score against',
            text=f'Candidate #{candidate.pk} {candidate.full_name} is not attached to '
                 f'an opening, so there are no requirements to score against. Pass a '
                 f'job_opening_id, or attach them with hr.update_candidate_status '
                 f'after setting the opening.')

    outcome = _screen(opening, candidate)

    evaluation = CandidateEvaluation.objects.create(
        candidate=candidate, job_opening=opening, agent=_agent(ctx),
        score=_decimal(outcome['total']) or Decimal('0'),
        score_breakdown=outcome['breakdown'],
        strengths=outcome['strengths'], gaps=outcome['gaps'],
        recommendation=outcome['recommendation'], rationale=outcome['rationale'])

    candidate.score = evaluation.score
    if candidate.status == 'new':
        candidate.status = 'screening'
    candidate.save(update_fields=['score', 'status', 'updated_at'])

    breakdown = '\n'.join(
        f'  {row["requirement"]}: {row["score"]:g}/{row["max"]:g}'
        f'{" (must-have)" if row.get("must_have") else ""} -- {row["evidence"]}'
        for row in outcome['breakdown'])

    return ToolResult(
        ok=True,
        text=(f'Scored candidate #{candidate.pk} {candidate.full_name} against opening '
              f'#{opening.pk} "{opening.title}": {outcome["total"]:g}/100 '
              f'({candidate.score_band}), recommendation {outcome["recommendation"]}. '
              f'Evaluation #{evaluation.pk} recorded.\n'
              f'Requirement by requirement:\n{breakdown}\n'
              f'Rationale: {outcome["rationale"]}'),
        data={'candidate_id': candidate.pk, 'job_opening_id': opening.pk,
              'evaluation_id': evaluation.pk, 'score': outcome['total'],
              'band': candidate.score_band,
              'recommendation': outcome['recommendation'],
              'breakdown': outcome['breakdown'],
              'strengths': outcome['strengths'], 'gaps': outcome['gaps'],
              'unmet_must_haves': outcome['unmet_must_haves'],
              'excluded_requirements': outcome['unscoreable'],
              'rationale': outcome['rationale']},
        subject_label='marketing.candidate', subject_id=candidate.pk)


@tool(
    name='hr.compare_candidates',
    title='Rank candidates side by side',
    description=(
        'Rank the candidates on an opening by score, showing each one strongest and '
        'weakest requirement and any must-have they do not evidence. Any candidate not '
        'yet scored is scored as part of the comparison, so nobody is silently left '
        'out of a ranking. Use this when asked who the best applicants are.'),
    group='Screening',
    agent_types=('hr',),
    icon='fa-ranking-star',
    parameters=_schema({
        'job_opening_id': _p('integer', 'The opening whose candidates to rank.'),
        'candidate_ids': {'type': 'array', 'items': {'type': 'integer'},
                          'description': 'Restrict to these candidates. Optional.'},
        'limit': _p('integer', 'How many to show. Defaults to 5.'),
    }, required=('job_opening_id',)),
)
def compare_candidates(ctx, job_opening_id, candidate_ids=None, limit=5):
    from ..models_hr import Candidate, CandidateEvaluation

    opening = _opening(job_opening_id)
    if opening is None:
        return _not_found('Job opening', job_opening_id, 'hr.list_job_openings')

    if candidate_ids:
        wanted = [pk for pk in (_int(value) for value in candidate_ids) if pk]
        people = list(Candidate.objects.filter(pk__in=wanted))
        unknown = sorted(set(wanted) - {row.pk for row in people})
    else:
        people = list(opening.candidates.exclude(status='withdrawn'))
        unknown = []

    if not people:
        return ToolResult(
            ok=True,
            text=f'No candidates to compare on opening #{opening.pk} '
                 f'"{opening.title}". Add them with hr.create_candidate.',
            data={'job_opening_id': opening.pk, 'ranking': []})

    ranking, newly_scored = [], []
    for person in people:
        evaluation = person.evaluations.filter(job_opening=opening).first()
        if evaluation is None:
            outcome = _screen(opening, person)
            evaluation = CandidateEvaluation.objects.create(
                candidate=person, job_opening=opening, agent=_agent(ctx),
                score=_decimal(outcome['total']) or Decimal('0'),
                score_breakdown=outcome['breakdown'],
                strengths=outcome['strengths'], gaps=outcome['gaps'],
                recommendation=outcome['recommendation'],
                rationale=outcome['rationale'])
            person.score = evaluation.score
            if person.status == 'new':
                person.status = 'screening'
            person.save(update_fields=['score', 'status', 'updated_at'])
            newly_scored.append(person.pk)

        best, worst = evaluation.strongest, evaluation.weakest
        unmet = [row['requirement'] for row in (evaluation.score_breakdown or [])
                 if isinstance(row, dict) and row.get('must_have') and row.get('max')
                 and row.get('score', 0) / row['max'] < 0.5]
        ranking.append({
            'candidate_id': person.pk, 'full_name': person.full_name,
            'score': float(evaluation.score or 0), 'band': evaluation.band,
            'status': person.status,
            'recommendation': evaluation.recommendation,
            'strongest': (best or {}).get('requirement', ''),
            'weakest': (worst or {}).get('requirement', ''),
            'weakest_evidence': (worst or {}).get('evidence', ''),
            'unmet_must_haves': unmet,
            'evaluation_id': evaluation.pk,
        })

    ranking.sort(key=lambda row: -row['score'])
    shown = ranking[:max(1, min(_int(limit, 5) or 5, 25))]

    lines = []
    for place, row in enumerate(shown, start=1):
        blocker = (f' Blocked on must-haves: {"; ".join(row["unmet_must_haves"][:2])}.'
                   if row['unmet_must_haves'] else '')
        lines.append(
            f'{place}. #{row["candidate_id"]} {row["full_name"]} -- '
            f'{row["score"]:g}/100 ({row["band"]}), {row["recommendation"]}. '
            f'Strongest: {row["strongest"] or "nothing stood out"}. '
            f'Weakest: {row["weakest"] or "nothing stood out"}.{blocker}')

    notes = []
    if newly_scored:
        notes.append(f'Scored {len(newly_scored)} candidate'
                     f'{"s" if len(newly_scored) > 1 else ""} for the first time as '
                     f'part of this comparison (ids '
                     f'{", ".join(str(pk) for pk in newly_scored)}).')
    if unknown:
        notes.append(f'No candidate exists with id '
                     f'{", ".join(str(pk) for pk in unknown)}; skipped.')
    if len(ranking) > len(shown):
        notes.append(f'{len(ranking) - len(shown)} further candidates were ranked but '
                     f'not shown; raise limit to see them.')

    return ToolResult(
        ok=True,
        text=(f'Ranked {len(ranking)} candidates on opening #{opening.pk} '
              f'"{opening.title}" against its {len(opening.requirement_lines)} '
              f'requirements.\n' + '\n'.join(lines) +
              (('\n' + ' '.join(notes)) if notes else '')),
        data={'job_opening_id': opening.pk, 'ranking': ranking,
              'shown': len(shown), 'newly_scored': newly_scored,
              'unknown_ids': unknown},
        subject_label='marketing.jobopening', subject_id=opening.pk)


@tool(
    name='hr.shortlist_candidate',
    title='Shortlist a candidate',
    description=(
        'Mark a candidate as shortlisted and record why. An internal record only, so '
        'it takes effect immediately; the candidate is not told anything by this tool. '
        'Score the candidate first so the reason can refer to the requirements they '
        'actually met.'),
    group='Screening',
    agent_types=('hr',),
    icon='fa-star',
    parameters=_schema({
        'candidate_id': _p('integer', 'The candidate to shortlist.'),
        'reason': _p('string', 'Why, in terms of the opening requirements.'),
    }, required=('candidate_id',)),
)
def shortlist_candidate(ctx, candidate_id, reason=''):
    candidate = _candidate(candidate_id)
    if candidate is None:
        return _not_found('Candidate', candidate_id, 'hr.list_candidates')

    was = candidate.get_status_display()
    evaluation = candidate.latest_evaluation
    stated = str(reason or '').strip()
    if not stated and evaluation:
        stated = evaluation.rationale
    if not stated:
        stated = 'No reason was recorded.'

    stamp = timezone.now().strftime('%d %b %Y')
    candidate.notes = (f'{candidate.notes}\n' if candidate.notes else '') + \
                      f'[{stamp}] Shortlisted: {stated}'
    candidate.status = 'shortlisted'
    candidate.save(update_fields=['status', 'notes', 'updated_at'])

    score_note = (f'Their recorded score is {float(candidate.score):g}/100 '
                  f'({candidate.score_band}).' if candidate.score is not None
                  else 'They have never been scored, so this shortlisting rests on '
                       'nothing recorded. Run hr.score_candidate.')

    return ToolResult(
        ok=True,
        text=(f'Shortlisted candidate #{candidate.pk} {candidate.full_name} '
              f'(was {was}). Reason recorded on the candidate: {stated} {score_note} '
              f'Nothing has been sent to them -- prepare an invitation with '
              f'hr.send_interview_invitation, which goes to the approval queue.'),
        data={'candidate_id': candidate.pk, 'status': candidate.status,
              'reason': stated,
              'score': float(candidate.score) if candidate.score is not None else None},
        subject_label='marketing.candidate', subject_id=candidate.pk)


@tool(
    name='hr.update_candidate_status',
    title='Change a candidate status',
    description=(
        'Move a candidate through the pipeline: new, screening, shortlisted, '
        'interviewing, offer, hired, rejected or withdrawn. Records the note against '
        'the candidate. Internal only -- it tells the candidate nothing, so use the '
        'email tools where they need to hear from us.'),
    group='Screening',
    agent_types=('hr',),
    icon='fa-arrow-right-arrow-left',
    parameters=_schema({
        'candidate_id': _p('integer', 'The candidate to move.'),
        'status': _p('string', 'new, screening, shortlisted, interviewing, offer, '
                               'hired, rejected or withdrawn.'),
        'note': _p('string', 'Why the status changed.'),
    }, required=('candidate_id', 'status')),
)
def update_candidate_status(ctx, candidate_id, status, note=''):
    from ..models_hr import Candidate

    candidate = _candidate(candidate_id)
    if candidate is None:
        return _not_found('Candidate', candidate_id, 'hr.list_candidates')

    allowed = [key for key, _label in Candidate.STATUS_CHOICES]
    wanted = str(status or '').strip().lower()
    if wanted not in allowed:
        return ToolResult(
            ok=False, error=f'{status} is not a candidate status',
            text=f'"{status}" is not a candidate status. Use one of: '
                 f'{", ".join(allowed)}. Nothing was changed.')

    was = candidate.get_status_display()
    candidate.status = wanted
    stated = str(note or '').strip()
    if stated:
        stamp = timezone.now().strftime('%d %b %Y')
        candidate.notes = (f'{candidate.notes}\n' if candidate.notes else '') + \
                          f'[{stamp}] {was} to {wanted}: {stated}'
    candidate.save(update_fields=['status', 'notes', 'updated_at'])

    warning = ''
    if wanted in ('rejected', 'offer') and not stated:
        warning = (' No reason was recorded for a decision the candidate will be told '
                   'about; add one so the email can be honest about it.')

    return ToolResult(
        ok=True,
        text=(f'Candidate #{candidate.pk} {candidate.full_name} moved from {was} to '
              f'{candidate.get_status_display()}. Nothing was sent to them.{warning}'),
        data={'candidate_id': candidate.pk, 'status': candidate.status,
              'previous_status': was, 'note': stated},
        subject_label='marketing.candidate', subject_id=candidate.pk)


@tool(
    name='hr.list_candidates',
    title='List candidates',
    description=(
        'List candidates with their ids, scores, bands and statuses, optionally '
        'filtered by opening or status. Call this before any tool needing a '
        'candidate_id.'),
    group='Screening',
    agent_types=('hr',),
    icon='fa-users',
    reads_only=True,
    parameters=_schema({
        'job_opening_id': _p('integer', 'Restrict to one opening.'),
        'status': _p('string', 'Restrict to one status.'),
        'limit': _p('integer', 'How many to return. Defaults to 25.'),
    }),
)
def list_candidates(ctx, job_opening_id=None, status='', limit=25):
    from ..models_hr import Candidate

    rows = Candidate.objects.all()
    if job_opening_id:
        opening = _opening(job_opening_id)
        if opening is None:
            return _not_found('Job opening', job_opening_id, 'hr.list_job_openings')
        rows = rows.filter(job_opening=opening)
    if status:
        rows = rows.filter(status=str(status))
    rows = list(rows.select_related('job_opening')[
        :max(1, min(_int(limit, 25) or 25, 100))])

    if not rows:
        return ToolResult(
            ok=True,
            text='No candidates match that. Add one with hr.create_candidate.',
            data={'candidates': [], 'count': 0})

    lines, payload = [], []
    for row in rows:
        score = f'{float(row.score):g}/100 ({row.score_band})' \
            if row.score is not None else 'unscored'
        lines.append(
            f'#{row.pk} {row.full_name} -- {score}, {row.get_status_display()}, '
            f'{row.current_title or "title not recorded"}, opening '
            f'{("#" + str(row.job_opening_id) + " " + row.job_opening.title) if row.job_opening else "none"}'
            f'{"" if row.email else ", no email on file"}')
        payload.append({'id': row.pk, 'full_name': row.full_name,
                        'email': row.email, 'status': row.status,
                        'score': float(row.score) if row.score is not None else None,
                        'band': row.score_band,
                        'current_title': row.current_title,
                        'job_opening_id': row.job_opening_id,
                        'has_resume': bool(row.resume_text)})

    return ToolResult(
        ok=True,
        text=f'{len(rows)} candidate{"s" if len(rows) > 1 else ""}:\n' +
             '\n'.join(lines),
        data={'candidates': payload, 'count': len(payload)})


# ===========================================================================
# GROUP: INTERVIEWING
# ===========================================================================

@tool(
    name='hr.create_interview',
    title='Plan an interview round',
    description=(
        'Create an interview round for a candidate with status proposed. This is the '
        'planning record: it holds the round, the format, the duration, the '
        'interviewers, the questions and the evaluation form. It does not book '
        'anything and nobody is invited. Booking a calendar entry is '
        'hr.schedule_interview and needs approval.'),
    group='Interviewing',
    agent_types=('hr',),
    icon='fa-clipboard-list',
    parameters=_schema({
        'candidate_id': _p('integer', 'Who is being interviewed.'),
        'round_name': _p('string', 'e.g. "First Interview", "Technical Round", '
                                   '"Final with the hiring manager".'),
        'mode': _p('string', 'video, phone or onsite.'),
        'duration_minutes': _p('integer', 'How long. Defaults to 45.'),
        'interviewers': dict(_STRING_LIST,
                             description='Names or email addresses of the panel.'),
        'job_opening_id': _p('integer', 'The opening. Defaults to the candidate one.'),
    }, required=('candidate_id',)),
)
def create_interview(ctx, candidate_id, round_name='First Interview', mode='video',
                     duration_minutes=45, interviewers=None, job_opening_id=None):
    from ..models_hr import Interview

    candidate = _candidate(candidate_id)
    if candidate is None:
        return _not_found('Candidate', candidate_id, 'hr.list_candidates')

    opening = _opening(job_opening_id) if job_opening_id else candidate.job_opening
    if job_opening_id and opening is None:
        return _not_found('Job opening', job_opening_id, 'hr.list_job_openings')

    round_number = candidate.interviews.count() + 1
    interview = Interview.objects.create(
        candidate=candidate, job_opening=opening,
        round_name=str(round_name or 'First Interview')[:120],
        round_number=round_number,
        duration_minutes=max(10, _int(duration_minutes, 45) or 45),
        mode=str(mode or 'video'),
        interviewers=_plain_list(interviewers),
        status='proposed',
        created_by_agent=_agent(ctx))

    if candidate.status in ('new', 'screening', 'shortlisted'):
        candidate.status = 'interviewing'
        candidate.save(update_fields=['status', 'updated_at'])

    panel = ', '.join(interview.interviewer_list) or 'no interviewers named yet'
    return ToolResult(
        ok=True,
        text=(f'Planned interview #{interview.pk}: {interview.round_name} (round '
              f'{round_number}) with candidate #{candidate.pk} '
              f'{candidate.full_name}, {interview.duration_minutes} minutes by '
              f'{interview.get_mode_display()}. Panel: {panel}. Status is proposed and '
              f'nothing is booked. Next: hr.generate_interview_questions '
              f'interview_id={interview.pk} to fill it out, then '
              f'hr.schedule_interview to put it on a calendar, which needs approval.'),
        data={'interview_id': interview.pk, 'candidate_id': candidate.pk,
              'job_opening_id': opening.pk if opening else None,
              'round_name': interview.round_name, 'round_number': round_number,
              'status': interview.status,
              'duration_minutes': interview.duration_minutes,
              'interviewers': interview.interviewer_list},
        subject_label='marketing.interview', subject_id=interview.pk)


@tool(
    name='hr.generate_interview_questions',
    title='Write competency-based interview questions',
    description=(
        'Produce interview questions derived from the requirement set on the opening, '
        'each with the competency it tests and what a good answer looks like. Where an '
        'interview_id is given the questions are saved onto that interview so the '
        'panel reads the same set. Questions about protected or personal attributes '
        'are never produced.'),
    group='Interviewing',
    agent_types=('hr',),
    icon='fa-comments',
    parameters=_schema({
        'job_opening_id': _p('integer', 'The opening whose requirements to derive '
                                        'from.'),
        'candidate_id': _p('integer', 'Candidate the round is for. Used to find the '
                                      'opening and the latest interview.'),
        'interview_id': _p('integer', 'Save the questions onto this interview.'),
        'round_name': _p('string', 'Which round these are for. Defaults to the '
                                   'name on the interview record.'),
        'count': _p('integer', 'How many questions. Defaults to 8.'),
    }),
)
def generate_interview_questions(ctx, job_opening_id=None, candidate_id=None,
                                 interview_id=None, round_name='', count=8):
    interview = _interview(interview_id) if interview_id else None
    if interview_id and interview is None:
        return _not_found('Interview', interview_id, 'hr.list_interviews')

    candidate = _candidate(candidate_id) if candidate_id else (
        interview.candidate if interview else None)
    if candidate_id and candidate is None:
        return _not_found('Candidate', candidate_id, 'hr.list_candidates')

    if interview is None and candidate is not None:
        interview = candidate.interviews.filter(
            status__in=('proposed', 'scheduled', 'rescheduled')).first()

    opening = _opening(job_opening_id) if job_opening_id else None
    if job_opening_id and opening is None:
        return _not_found('Job opening', job_opening_id, 'hr.list_job_openings')
    if opening is None:
        opening = (interview.job_opening if interview else None) or \
                  (candidate.job_opening if candidate else None)

    if opening is None:
        return ToolResult(
            ok=False, error='no opening to derive from',
            text='Competency questions come from an opening requirements. Give a '
                 'job_opening_id, or a candidate or interview attached to an opening.')

    requirements = _normalise_requirements(opening.requirements)
    if not requirements:
        return ToolResult(
            ok=False, error='opening has no requirements',
            text=f'Opening #{opening.pk} "{opening.title}" has no requirements, so '
                 f'there is nothing to build competency questions from. Run '
                 f'hr.generate_job_requirements on it first.')

    label = str(round_name or '').strip() or (
        interview.round_name if interview else 'First Interview')
    questions = _generate_questions(requirements, label, _int(count, 8) or 8)

    saved = ''
    if interview is not None:
        interview.questions = questions
        if not interview.round_name:
            interview.round_name = label[:120]
        interview.save(update_fields=['questions', 'round_name', 'updated_at'])
        saved = (f' Saved onto interview #{interview.pk} '
                 f'({interview.round_name} with {interview.candidate.full_name}).')

    listing = '\n'.join(
        f'{index}. {row["question"]}\n   Tests: {row["competency"]}\n'
        f'   Look for: {row["what_to_look_for"]}'
        for index, row in enumerate(questions, start=1))

    return ToolResult(
        ok=True,
        text=(f'{len(questions)} competency questions for the {label} on opening '
              f'#{opening.pk} "{opening.title}", derived from its own '
              f'requirements.{saved}\n{listing}'),
        data={'interview_id': interview.pk if interview else None,
              'job_opening_id': opening.pk, 'round_name': label,
              'questions': questions},
        subject_label='marketing.interview',
        subject_id=interview.pk if interview else 0)


@tool(
    name='hr.generate_evaluation_form',
    title='Write an interview evaluation form',
    description=(
        'Produce the scoring criteria an interviewer fills in: the requirements on the '
        'opening first, each with a scale and guidance on how to score it, then '
        'the standard criteria. Where an interview_id is given the form is saved onto '
        'that interview so every panel member scores the same things.'),
    group='Interviewing',
    agent_types=('hr',),
    icon='fa-square-poll-vertical',
    parameters=_schema({
        'job_opening_id': _p('integer', 'The opening whose requirements to score.'),
        'interview_id': _p('integer', 'Save the form onto this interview.'),
    }),
)
def generate_evaluation_form(ctx, job_opening_id=None, interview_id=None):
    interview = _interview(interview_id) if interview_id else None
    if interview_id and interview is None:
        return _not_found('Interview', interview_id, 'hr.list_interviews')

    opening = _opening(job_opening_id) if job_opening_id else (
        interview.job_opening if interview else None)
    if job_opening_id and opening is None:
        return _not_found('Job opening', job_opening_id, 'hr.list_job_openings')
    if opening is None:
        return ToolResult(
            ok=False, error='no opening to derive from',
            text='An evaluation form is built from an opening requirements. Give a '
                 'job_opening_id, or an interview attached to an opening.')

    rows = _generate_evaluation_form(opening.requirements)

    saved = ''
    if interview is not None:
        interview.evaluation_form = rows
        interview.save(update_fields=['evaluation_form', 'updated_at'])
        saved = f' Saved onto interview #{interview.pk}.'

    listing = '\n'.join(
        f'{index}. {row["criterion"]} [{row["scale"]}]\n   {row["guidance"]}'
        for index, row in enumerate(rows, start=1))

    return ToolResult(
        ok=True,
        text=(f'{len(rows)} scoring criteria for opening #{opening.pk} '
              f'"{opening.title}", requirement-led then general.{saved}\n{listing}'),
        data={'job_opening_id': opening.pk,
              'interview_id': interview.pk if interview else None,
              'criteria': rows},
        subject_label='marketing.interview',
        subject_id=interview.pk if interview else 0)


@tool(
    name='hr.list_interviews',
    title='List interviews',
    description=(
        'List interviews with their ids, rounds, candidates, statuses and scheduled '
        'times. Call this before any tool needing an interview_id.'),
    group='Interviewing',
    agent_types=('hr',),
    icon='fa-calendar-check',
    reads_only=True,
    parameters=_schema({
        'candidate_id': _p('integer', 'Restrict to one candidate.'),
        'status': _p('string', 'proposed, scheduled, rescheduled, completed, '
                               'cancelled or no_show.'),
        'limit': _p('integer', 'How many to return. Defaults to 20.'),
    }),
)
def list_interviews(ctx, candidate_id=None, status='', limit=20):
    from ..models_hr import Interview

    rows = Interview.objects.select_related('candidate', 'job_opening')
    if candidate_id:
        candidate = _candidate(candidate_id)
        if candidate is None:
            return _not_found('Candidate', candidate_id, 'hr.list_candidates')
        rows = rows.filter(candidate=candidate)
    if status:
        rows = rows.filter(status=str(status))
    rows = list(rows[:max(1, min(_int(limit, 20) or 20, 100))])

    if not rows:
        return ToolResult(
            ok=True,
            text='No interviews match that. Plan one with hr.create_interview.',
            data={'interviews': [], 'count': 0})

    lines, payload = [], []
    for row in rows:
        lines.append(
            f'#{row.pk} {row.round_name} with #{row.candidate_id} '
            f'{row.candidate.full_name} -- {row.get_status_display()}, '
            f'{row.when_readable}, {row.duration_minutes} minutes by '
            f'{row.get_mode_display()}, panel '
            f'{", ".join(row.interviewer_list) or "not named"}, '
            f'{row.question_count} questions prepared')
        payload.append({'id': row.pk, 'round_name': row.round_name,
                        'candidate_id': row.candidate_id,
                        'candidate': row.candidate.full_name,
                        'status': row.status,
                        'scheduled_at': row.scheduled_at.isoformat()
                        if row.scheduled_at else None,
                        'duration_minutes': row.duration_minutes,
                        'mode': row.mode, 'interviewers': row.interviewer_list,
                        'questions': row.question_count,
                        'job_opening_id': row.job_opening_id,
                        'calendar_event_id': row.calendar_event_id})

    return ToolResult(
        ok=True,
        text=f'{len(rows)} interview{"s" if len(rows) > 1 else ""}:\n' +
             '\n'.join(lines),
        data={'interviews': payload, 'count': len(payload)})


@tool(
    name='hr.schedule_interview',
    title='Book an interview on the calendar',
    description=(
        'Put a planned interview on the calendar and invite the panel and the '
        'candidate. This reaches outside the company, so it is prepared and queued for '
        'a person to approve rather than booked. Give the start time as an ISO '
        'timestamp or "YYYY-MM-DD HH:MM"; the result says exactly what was understood, '
        'which is worth reading back before anybody approves it.'),
    group='Interviewing',
    agent_types=('hr',),
    integration='google_calendar',
    requires_approval=True,
    risk='high',
    icon='fa-calendar-plus',
    parameters=_schema({
        'start_at': _p('string', 'When it starts, ISO or "YYYY-MM-DD HH:MM".'),
        'interview_id': _p('integer', 'The planned interview to book.'),
        'candidate_id': _p('integer', 'Used instead of interview_id: books the '
                                      'candidate open round, creating one if needed.'),
        'duration_minutes': _p('integer', 'How long. Defaults to the duration on the '
                                          'interview record, or 45.'),
        'mode': _p('string', 'video, phone or onsite.'),
        'interviewers': dict(_STRING_LIST, description='Panel email addresses.'),
        'title': _p('string', 'Calendar entry title. One is written if omitted.'),
    }, required=('start_at',)),
)
def schedule_interview(ctx, start_at, interview_id=None, candidate_id=None,
                       duration_minutes=45, mode='video', interviewers=None,
                       title=''):
    from ..models_hr import Interview

    interview = _interview(interview_id) if interview_id else None
    if interview_id and interview is None:
        return _not_found('Interview', interview_id, 'hr.list_interviews')

    created_round = False
    if interview is None:
        candidate = _candidate(candidate_id)
        if candidate is None:
            return ToolResult(
                ok=False, error='no interview or candidate given',
                text='Give either an interview_id from hr.list_interviews or a '
                     'candidate_id, otherwise there is nothing to book.')
        interview = candidate.interviews.filter(
            status__in=('proposed', 'scheduled', 'rescheduled')).first()
        if interview is None:
            interview = Interview.objects.create(
                candidate=candidate, job_opening=candidate.job_opening,
                round_name='First Interview',
                round_number=candidate.interviews.count() + 1,
                duration_minutes=max(10, _int(duration_minutes, 45) or 45),
                mode=str(mode or 'video'),
                interviewers=_plain_list(interviewers),
                status='proposed', created_by_agent=_agent(ctx))
            created_round = True

    when, described = _parse_datetime(start_at)
    if when is None:
        return ToolResult(ok=False, error='unreadable start time',
                          text=f'Nothing was queued: {described}')

    minutes = max(10, _int(duration_minutes, interview.duration_minutes) or 45)
    panel = _plain_list(interviewers) or interview.interviewer_list
    candidate = interview.candidate
    opening = interview.job_opening

    attendees = list(panel)
    if candidate.email and candidate.email not in attendees:
        attendees.append(candidate.email)

    entry_title = str(title or '').strip() or (
        f'{interview.round_name}: {candidate.full_name}'
        f'{" -- " + opening.title if opening else ""}')

    format_line = {'video': 'Video call', 'phone': 'Telephone',
                   'onsite': 'On site'}.get(str(mode or interview.mode), 'Video call')

    description = (
        f'{interview.round_name} for {candidate.full_name}'
        f'{" for " + opening.title + " (opening #" + str(opening.pk) + ")" if opening else ""}.\n'
        f'Format: {format_line}. Duration: {minutes} minutes.\n'
        f'Panel: {", ".join(panel) or "to be confirmed"}.\n'
        f'Candidate: {candidate.full_name}'
        f'{" <" + candidate.email + ">" if candidate.email else " (no email on file)"}'
        f'{", currently " + candidate.current_title if candidate.current_title else ""}.\n'
        f'{"Questions and the evaluation form are on interview #" + str(interview.pk) + " in the platform." if interview.questions else "No questions have been prepared yet; run hr.generate_interview_questions."}')

    warnings = []
    if not attendees:
        warnings.append('There are no attendees: the panel is unnamed and the '
                        'candidate has no email address on file.')
    if not candidate.email:
        warnings.append('The candidate has no email address, so they will not receive '
                        'the invitation.')
    if created_round:
        warnings.append(f'No planned round existed, so interview #{interview.pk} was '
                        f'created for this booking.')

    return Proposal(
        title=f'Book {interview.round_name} with {candidate.full_name}',
        summary=(f'Calendar entry "{entry_title}" for {described}, {minutes} minutes, '
                 f'{format_line.lower()}. Attendees: '
                 f'{", ".join(attendees) or "none"}. '
                 + (' '.join(warnings) if warnings else '')),
        payload={'interview_id': interview.pk,
                 'candidate_id': candidate.pk,
                 'title': entry_title,
                 'start_at': when.isoformat(),
                 'duration_minutes': minutes,
                 'mode': str(mode or interview.mode),
                 'attendees': attendees,
                 'description': description,
                 'location': interview.location_or_link or (
                     'Video call link to be added' if format_line == 'Video call'
                     else '')},
        editable_fields=[
            editable('title', 'Calendar entry title'),
            editable('start_at', 'Start time', help_text='ISO timestamp.'),
            editable('duration_minutes', 'Duration in minutes'),
            editable('location', 'Location or meeting link'),
            editable('description', 'Description', 'longtext', rows=10),
        ],
        risk='high',
        subject_label='marketing.interview',
        subject_id=interview.pk,
        subject_display=f'{interview.round_name} with {candidate.full_name}',
        confirmation=(f'Prepared the calendar booking for interview #{interview.pk}, '
                      f'{interview.round_name} with {candidate.full_name}, read as '
                      f'{described}. It is in the approval queue and nothing has been '
                      f'booked or sent yet. '
                      + (' '.join(warnings) if warnings else '')))


@executor('hr.schedule_interview')
def execute_schedule_interview(action):
    from ..models_platform import CalendarEvent

    payload = action.payload or {}
    interview = _interview(payload.get('interview_id'))
    when, described = _parse_datetime(payload.get('start_at'))
    if when is None:
        return ToolResult(ok=False, error='unreadable start time',
                          text=f'Nothing was booked: {described}')

    minutes = max(10, _int(payload.get('duration_minutes'), 45) or 45)
    attendees = _plain_list(payload.get('attendees'))
    entry_title = str(payload.get('title') or 'Interview')[:250]

    result = integrations.call(
        'google_calendar', 'create_event',
        title=entry_title, start=when.isoformat(), duration_minutes=minutes,
        attendees=attendees, description=str(payload.get('description') or ''),
        location=str(payload.get('location') or ''))

    event = CalendarEvent.objects.create(
        title=entry_title, description=str(payload.get('description') or ''),
        start_at=when, end_at=when + timedelta(minutes=minutes),
        duration_minutes=minutes, attendees=attendees,
        location=str(payload.get('location') or '')[:300],
        meeting_link=str((result.data or {}).get('meeting_link')
                         or (result.data or {}).get('hangout_link') or '')[:400],
        status='simulated' if result.demo else 'scheduled',
        external_id=_external_id(result),
        agent=action.agent, action=action,
        subject_label='marketing.interview',
        subject_id=interview.pk if interview else None)

    if interview is not None and result.ok:
        interview.calendar_event = event
        interview.scheduled_at = when
        interview.status = 'scheduled'
        interview.duration_minutes = minutes
        if payload.get('location'):
            interview.location_or_link = str(payload['location'])[:400]
        interview.save(update_fields=['calendar_event', 'scheduled_at', 'status',
                                      'duration_minutes', 'location_or_link',
                                      'updated_at'])

    who = interview.candidate.full_name if interview else 'the candidate'
    return _executed(result,
                     f'Booked "{entry_title}" with {who} for {described}, {minutes} '
                     f'minutes, calendar event record #{event.pk}',
                     prepared_but_not_sent=f'The booking for {who}')


@tool(
    name='hr.reschedule_interview',
    title='Move an interview',
    description=(
        'Move a booked interview to a new time and update the calendar entry, '
        'recording the reason. Reaches outside the company, so it is queued for '
        'approval. Use this rather than booking a second entry, which would leave the '
        'candidate holding two invitations.'),
    group='Interviewing',
    agent_types=('hr',),
    integration='google_calendar',
    requires_approval=True,
    risk='high',
    icon='fa-calendar-day',
    parameters=_schema({
        'interview_id': _p('integer', 'The interview to move.'),
        'new_start_at': _p('string', 'The new start, ISO or "YYYY-MM-DD HH:MM".'),
        'reason': _p('string', 'Why it is moving. The candidate is owed one.'),
    }, required=('interview_id', 'new_start_at')),
)
def reschedule_interview(ctx, interview_id, new_start_at, reason=''):
    interview = _interview(interview_id)
    if interview is None:
        return _not_found('Interview', interview_id, 'hr.list_interviews')

    when, described = _parse_datetime(new_start_at)
    if when is None:
        return ToolResult(ok=False, error='unreadable start time',
                          text=f'Nothing was queued: {described}')

    event = interview.calendar_event
    candidate = interview.candidate
    stated = str(reason or '').strip()

    notes = []
    if event is None:
        notes.append('This interview has no calendar entry, so approving this will '
                     'create one rather than move one.')
    if not stated:
        notes.append('No reason was given. A candidate rearranged without a reason '
                     'reads as disorganised, so add one.')

    attendees = list(interview.interviewer_list)
    if candidate.email and candidate.email not in attendees:
        attendees.append(candidate.email)

    entry_title = (event.title if event else
                   f'{interview.round_name}: {candidate.full_name}')

    return Proposal(
        title=f'Move {interview.round_name} with {candidate.full_name} to {described}',
        summary=(f'Was {interview.when_readable}, moving to {described}. '
                 f'Reason: {stated or "none given"}. '
                 f'Attendees: {", ".join(attendees) or "none"}. '
                 + ' '.join(notes)),
        payload={'interview_id': interview.pk,
                 'calendar_event_id': event.pk if event else None,
                 'external_event_id': event.external_id if event else '',
                 'title': entry_title,
                 'start_at': when.isoformat(),
                 'duration_minutes': interview.duration_minutes,
                 'attendees': attendees,
                 'reason': stated,
                 'description': (f'{interview.round_name} for {candidate.full_name}, '
                                 f'moved from {interview.when_readable}.'
                                 + (f' Reason: {stated}' if stated else '')),
                 'location': interview.location_or_link},
        editable_fields=[
            editable('title', 'Calendar entry title'),
            editable('start_at', 'New start time', help_text='ISO timestamp.'),
            editable('duration_minutes', 'Duration in minutes'),
            editable('reason', 'Reason for moving it', 'longtext', rows=3),
            editable('location', 'Location or meeting link'),
        ],
        risk='high',
        subject_label='marketing.interview',
        subject_id=interview.pk,
        subject_display=f'{interview.round_name} with {candidate.full_name}',
        confirmation=(f'Prepared the move of interview #{interview.pk} from '
                      f'{interview.when_readable} to {described}. Queued for approval; '
                      f'the calendar has not changed. ' + ' '.join(notes)))


@executor('hr.reschedule_interview')
def execute_reschedule_interview(action):
    from ..models_platform import CalendarEvent

    payload = action.payload or {}
    interview = _interview(payload.get('interview_id'))
    when, described = _parse_datetime(payload.get('start_at'))
    if when is None:
        return ToolResult(ok=False, error='unreadable start time',
                          text=f'Nothing was moved: {described}')

    minutes = max(10, _int(payload.get('duration_minutes'), 45) or 45)
    attendees = _plain_list(payload.get('attendees'))
    entry_title = str(payload.get('title') or 'Interview')[:250]

    result = integrations.call(
        'google_calendar', 'update_event',
        event_id=str(payload.get('external_event_id') or ''),
        title=entry_title, start=when.isoformat(), duration_minutes=minutes,
        attendees=attendees, description=str(payload.get('description') or ''),
        location=str(payload.get('location') or ''))

    event = None
    event_id = _int(payload.get('calendar_event_id'))
    if event_id:
        event = CalendarEvent.objects.filter(pk=event_id).first()

    if event is None:
        event = CalendarEvent.objects.create(
            title=entry_title, description=str(payload.get('description') or ''),
            start_at=when, end_at=when + timedelta(minutes=minutes),
            duration_minutes=minutes, attendees=attendees,
            location=str(payload.get('location') or '')[:300],
            status='simulated' if result.demo else 'rescheduled',
            external_id=_external_id(result),
            agent=action.agent, action=action,
            subject_label='marketing.interview',
            subject_id=interview.pk if interview else None)
    else:
        event.start_at = when
        event.end_at = when + timedelta(minutes=minutes)
        event.duration_minutes = minutes
        event.attendees = attendees
        event.title = entry_title
        event.description = str(payload.get('description') or '')
        event.status = 'simulated' if result.demo else 'rescheduled'
        event.save()

    if interview is not None and result.ok:
        interview.calendar_event = event
        interview.scheduled_at = when
        interview.status = 'rescheduled'
        interview.duration_minutes = minutes
        interview.reschedule_reason = str(payload.get('reason') or '')
        interview.save(update_fields=['calendar_event', 'scheduled_at', 'status',
                                      'duration_minutes', 'reschedule_reason',
                                      'updated_at'])

    who = interview.candidate.full_name if interview else 'the candidate'
    return _executed(result,
                     f'Moved the interview with {who} to {described} and updated '
                     f'calendar event record #{event.pk}',
                     prepared_but_not_sent=f'The move of the interview with {who}')


# ===========================================================================
# GROUP: CANDIDATE COMMUNICATION
#
# Every tool in this group returns a Proposal. Candidate email is the company
# face, and it is the one place where a wrong sentence cannot be edited after
# the fact, so the complete text goes into the queue for somebody to read.
#
# Each composes a full default message when none is supplied, because a
# proposal containing an empty body is not reviewable: the reviewer would be
# approving the idea of an email rather than an email.
# ===========================================================================

_SIGN_OFF = 'People Operations'


def _role_title_for(candidate):
    if candidate.job_opening:
        return candidate.job_opening.title
    return 'the role you applied for'


def _email_editables():
    return [editable('to', 'To'),
            editable('subject', 'Subject'),
            editable('body', 'Body', 'longtext', rows=16)]


def _needs_email(candidate):
    if candidate.email:
        return None
    return ToolResult(
        ok=False, error='candidate has no email address',
        text=f'Candidate #{candidate.pk} {candidate.full_name} has no email address on '
             f'file, so nothing can be prepared. Add one with hr.create_candidate or '
             f'ask whoever has it.')


@tool(
    name='hr.send_interview_invitation',
    title='Invite a candidate to interview',
    description=(
        'Prepare an interview invitation to a candidate stating the round, the time '
        'with its timezone, the duration, the format and who will be there. Where the '
        'subject and body are not supplied a complete invitation is written from the '
        'interview record. Reaches a person outside the company, so it is queued for '
        'approval and not sent.'),
    group='Candidate Communication',
    agent_types=('hr',),
    integration='gmail',
    requires_approval=True,
    risk='high',
    icon='fa-envelope-open-text',
    parameters=_schema({
        'candidate_id': _p('integer', 'Who is being invited.'),
        'interview_id': _p('integer', 'The interview being invited to. Defaults to '
                                      'their open round.'),
        'subject': _p('string', 'Subject line. One is written if omitted.'),
        'body': _p('string', 'Body. A complete invitation is written if omitted.'),
        'proposed_time': _p('string', 'The time to state, where the interview has not '
                                      'been booked yet.'),
    }, required=('candidate_id',)),
)
def send_interview_invitation(ctx, candidate_id, interview_id=None, subject='',
                              body='', proposed_time=''):
    candidate = _candidate(candidate_id)
    if candidate is None:
        return _not_found('Candidate', candidate_id, 'hr.list_candidates')

    blocked = _needs_email(candidate)
    if blocked:
        return blocked

    interview = _interview(interview_id) if interview_id else None
    if interview_id and interview is None:
        return _not_found('Interview', interview_id, 'hr.list_interviews')
    if interview is None:
        interview = candidate.interviews.filter(
            status__in=('scheduled', 'rescheduled', 'proposed')).first()

    role = _role_title_for(candidate)
    round_name = interview.round_name if interview else 'a first conversation'
    minutes = interview.duration_minutes if interview else 45
    mode_label = {'video': 'a video call', 'phone': 'a telephone call',
                  'onsite': 'an in-person meeting at our office'}.get(
        interview.mode if interview else 'video', 'a video call')
    panel = ', '.join(interview.interviewer_list) if interview else ''

    if proposed_time:
        parsed, when_text = _parse_datetime(proposed_time)
        when_text = when_text if parsed else str(proposed_time)
    elif interview and interview.scheduled_at:
        when_text = interview.when_readable
    else:
        when_text = ''

    time_line = (f'when: {when_text}' if when_text else
                 'a time still to be agreed with you')

    resolved_subject = str(subject or '').strip() or (
        f'{round_name} for {role}')

    if str(body or '').strip():
        resolved_body = str(body)
    else:
        lines = [f'Dear {candidate.full_name.split()[0] if candidate.full_name else "there"},',
                 '',
                 f'Thank you for your interest in the {role} role. We would like to '
                 f'invite you to {round_name.lower()}.']
        detail = [f'Format: {mode_label}.',
                  f'Length: {minutes} minutes.']
        if when_text:
            detail.insert(0, f'When: {when_text}.')
        else:
            detail.insert(0, 'When: we will confirm a time that suits you.')
        if panel:
            detail.append(f'You will be meeting: {panel}.')
        if interview and interview.location_or_link:
            detail.append(f'Where: {interview.location_or_link}.')
        lines.append('')
        lines.extend(detail)
        lines.extend([
            '',
            'The conversation is about your experience against what the role actually '
            'needs, so please come ready to talk through specific pieces of work you '
            'have done.',
            '',
            'If that time does not work, reply and tell us what does and we will move '
            'it.',
            '',
            'Kind regards,',
            _SIGN_OFF,
        ])
        resolved_body = '\n'.join(lines)

    gaps = []
    if not when_text:
        gaps.append('No time is stated in this invitation, because the interview has '
                    'not been booked. Either book it with hr.schedule_interview first '
                    'or edit a time into the body before approving.')
    if interview is None:
        gaps.append('There is no interview record behind this message, so the round, '
                    'duration and panel are defaults rather than facts.')
    elif not panel:
        gaps.append('The panel is unnamed.')

    return Proposal(
        title=f'Interview invitation to {candidate.full_name}',
        summary=(f'To {candidate.email} about {role}. {round_name}, {minutes} minutes, '
                 f'{mode_label}, {time_line}. ' + ' '.join(gaps)),
        payload={'candidate_id': candidate.pk,
                 'interview_id': interview.pk if interview else None,
                 'to': candidate.email,
                 'subject': resolved_subject,
                 'body': resolved_body},
        editable_fields=_email_editables(),
        risk='high',
        subject_label='marketing.candidate',
        subject_id=candidate.pk,
        subject_display=candidate.full_name,
        confirmation=(f'Prepared the interview invitation for candidate #{candidate.pk} '
                      f'{candidate.full_name} to {candidate.email}. It is in the '
                      f'approval queue and has not been sent. '
                      + ' '.join(gaps)))


@executor('hr.send_interview_invitation')
def execute_interview_invitation(action):
    payload = action.payload or {}
    to = str(payload.get('to') or '')
    subject = str(payload.get('subject') or '')
    body = str(payload.get('body') or '')

    result = integrations.call('gmail', 'send_email', to=to, subject=subject, body=body)
    _record_message(action, result, channel='email', recipient=to, subject=subject,
                    body=body,
                    metadata={'kind': 'interview_invitation',
                              'candidate_id': payload.get('candidate_id'),
                              'interview_id': payload.get('interview_id')})

    return _executed(result, f'Sent the interview invitation to {to}',
                     prepared_but_not_sent=f'The interview invitation to {to}')


@tool(
    name='hr.send_rejection_email',
    title='Tell a candidate they were not successful',
    description=(
        'Prepare a short, warm and honest rejection. Where the body is not supplied '
        'one is written that thanks them, states the decision plainly, and gives the '
        'reason where one was recorded, without pretending the decision was closer '
        'than it was. Queued for approval. On approval the candidate status is also '
        'set to rejected.'),
    group='Candidate Communication',
    agent_types=('hr',),
    integration='gmail',
    requires_approval=True,
    risk='high',
    icon='fa-envelope',
    parameters=_schema({
        'candidate_id': _p('integer', 'Who is being told.'),
        'subject': _p('string', 'Subject line. One is written if omitted.'),
        'body': _p('string', 'Body. One is written if omitted.'),
        'reason': _p('string', 'The real reason, in terms of the role requirements. '
                               'Used in the body where it is specific enough to say.'),
    }, required=('candidate_id',)),
)
def send_rejection_email(ctx, candidate_id, subject='', body='', reason=''):
    candidate = _candidate(candidate_id)
    if candidate is None:
        return _not_found('Candidate', candidate_id, 'hr.list_candidates')

    blocked = _needs_email(candidate)
    if blocked:
        return blocked

    role = _role_title_for(candidate)
    evaluation = candidate.latest_evaluation
    stated = str(reason or '').strip()
    if not stated and evaluation and evaluation.gaps:
        stated = (f'we were looking for stronger evidence of '
                  f'{"; ".join(evaluation.gaps[:2])}')

    resolved_subject = str(subject or '').strip() or f'Your application for {role}'

    if str(body or '').strip():
        resolved_body = str(body)
    else:
        first = candidate.full_name.split()[0] if candidate.full_name else 'there'
        lines = [f'Dear {first},', '',
                 f'Thank you for applying for the {role} role, and for the time you '
                 f'put into it.',
                 '',
                 'We will not be taking your application further on this occasion.']
        if stated:
            lines.extend(['', f'For what it is worth, {stated}. That is a comment on '
                              f'the fit with this particular role and nothing more.'])
        lines.extend([
            '',
            'We would be glad to hear from you again if something closer to your '
            'experience comes up.',
            '',
            'We wish you well with your search.',
            '',
            'Kind regards,',
            _SIGN_OFF,
        ])
        resolved_body = '\n'.join(lines)

    notes = []
    if not stated:
        notes.append('No reason is stated, which is acceptable but tells the candidate '
                     'nothing useful.')
    if candidate.score is None:
        notes.append('This candidate has never been scored, so there is no record '
                     'behind the decision. Consider running hr.score_candidate first.')

    return Proposal(
        title=f'Rejection to {candidate.full_name}',
        summary=(f'To {candidate.email} about {role}. '
                 f'Reason recorded: {stated or "none"}. '
                 f'On approval the candidate is also marked rejected. '
                 + ' '.join(notes)),
        payload={'candidate_id': candidate.pk,
                 'to': candidate.email,
                 'subject': resolved_subject,
                 'body': resolved_body,
                 'reason': stated},
        editable_fields=_email_editables() + [
            editable('reason', 'Reason recorded internally', 'longtext', rows=3)],
        risk='high',
        subject_label='marketing.candidate',
        subject_id=candidate.pk,
        subject_display=candidate.full_name,
        confirmation=(f'Prepared the rejection for candidate #{candidate.pk} '
                      f'{candidate.full_name} to {candidate.email}. Queued for '
                      f'approval; nothing has been sent and their status is unchanged '
                      f'until it is. ' + ' '.join(notes)))


@executor('hr.send_rejection_email')
def execute_rejection_email(action):
    payload = action.payload or {}
    to = str(payload.get('to') or '')
    subject = str(payload.get('subject') or '')
    body = str(payload.get('body') or '')
    candidate = _candidate(payload.get('candidate_id'))

    result = integrations.call('gmail', 'send_email', to=to, subject=subject, body=body)
    _record_message(action, result, channel='email', recipient=to, subject=subject,
                    body=body,
                    metadata={'kind': 'rejection',
                              'candidate_id': payload.get('candidate_id'),
                              'reason': payload.get('reason', '')})

    tail = ''
    if candidate is not None and result.ok:
        candidate.status = 'rejected'
        stamp = timezone.now().strftime('%d %b %Y')
        reason = str(payload.get('reason') or '').strip()
        candidate.notes = (f'{candidate.notes}\n' if candidate.notes else '') + \
                          f'[{stamp}] Rejection sent.' + (f' Reason: {reason}' if reason else '')
        candidate.save(update_fields=['status', 'notes', 'updated_at'])
        tail = f' Candidate #{candidate.pk} is now marked rejected.'

    return _executed(result, f'Sent the rejection to {to}.{tail}'.rstrip('.'),
                     prepared_but_not_sent=f'The rejection to {to}')


@tool(
    name='hr.send_selection_email',
    title='Tell a candidate they were successful',
    description=(
        'Prepare the good-news message: they have been selected, what the role is, and '
        'what happens next. Where the body is not supplied one is written. Do not '
        'promise a timeline, a figure or a condition that was not given to you. Queued '
        'for approval. On approval the candidate status is set to offer.'),
    group='Candidate Communication',
    agent_types=('hr',),
    integration='gmail',
    requires_approval=True,
    risk='high',
    icon='fa-envelope-circle-check',
    parameters=_schema({
        'candidate_id': _p('integer', 'Who is being told.'),
        'subject': _p('string', 'Subject line. One is written if omitted.'),
        'body': _p('string', 'Body. One is written if omitted.'),
        'role_title': _p('string', 'The role, where it differs from the opening title.'),
        'next_steps': _p('string', 'What happens next. State only what you were told.'),
    }, required=('candidate_id',)),
)
def send_selection_email(ctx, candidate_id, subject='', body='', role_title='',
                         next_steps=''):
    candidate = _candidate(candidate_id)
    if candidate is None:
        return _not_found('Candidate', candidate_id, 'hr.list_candidates')

    blocked = _needs_email(candidate)
    if blocked:
        return blocked

    role = str(role_title or '').strip() or _role_title_for(candidate)
    steps = str(next_steps or '').strip()

    resolved_subject = str(subject or '').strip() or f'Good news about {role}'

    if str(body or '').strip():
        resolved_body = str(body)
    else:
        first = candidate.full_name.split()[0] if candidate.full_name else 'there'
        lines = [f'Dear {first},', '',
                 f'We would like to offer you the {role} role. Everyone you met was '
                 f'clear that you were the right person for it, and we are glad to be '
                 f'able to say so.']
        if steps:
            lines.extend(['', f'What happens next: {steps}'])
        else:
            lines.extend(['', 'We will be in touch shortly with the written offer and '
                              'the details that go with it.'])
        lines.extend([
            '',
            'If there is anything you want to ask before then, reply to this message '
            'and we will answer it.',
            '',
            'Congratulations, and we hope to hear from you soon.',
            '',
            'Kind regards,',
            _SIGN_OFF,
        ])
        resolved_body = '\n'.join(lines)

    notes = []
    if not steps:
        notes.append('No next steps were given, so the body says only that the written '
                     'offer will follow. Do not add a date that nobody has agreed.')
    if 'salary' in resolved_body.lower() or '$' in resolved_body:
        notes.append('The body appears to mention money. Check that the figure is one '
                     'that was actually authorised.')

    return Proposal(
        title=f'Selection message to {candidate.full_name}',
        summary=(f'To {candidate.email} offering {role}. '
                 f'Next steps: {steps or "not stated"}. '
                 f'On approval the candidate is marked as having an offer. '
                 + ' '.join(notes)),
        payload={'candidate_id': candidate.pk,
                 'to': candidate.email,
                 'subject': resolved_subject,
                 'body': resolved_body,
                 'role_title': role,
                 'next_steps': steps},
        editable_fields=_email_editables() + [
            editable('next_steps', 'Next steps', 'longtext', rows=3)],
        risk='high',
        subject_label='marketing.candidate',
        subject_id=candidate.pk,
        subject_display=candidate.full_name,
        confirmation=(f'Prepared the selection message for candidate #{candidate.pk} '
                      f'{candidate.full_name} to {candidate.email}. Queued for '
                      f'approval; nothing has been sent. ' + ' '.join(notes)))


@executor('hr.send_selection_email')
def execute_selection_email(action):
    payload = action.payload or {}
    to = str(payload.get('to') or '')
    subject = str(payload.get('subject') or '')
    body = str(payload.get('body') or '')
    candidate = _candidate(payload.get('candidate_id'))

    result = integrations.call('gmail', 'send_email', to=to, subject=subject, body=body)
    _record_message(action, result, channel='email', recipient=to, subject=subject,
                    body=body,
                    metadata={'kind': 'selection',
                              'candidate_id': payload.get('candidate_id'),
                              'role_title': payload.get('role_title', '')})

    tail = ''
    if candidate is not None and result.ok:
        candidate.status = 'offer'
        candidate.save(update_fields=['status', 'updated_at'])
        tail = f' Candidate #{candidate.pk} is now at offer stage.'

    return _executed(result, f'Sent the selection message to {to}.{tail}'.rstrip('.'),
                     prepared_but_not_sent=f'The selection message to {to}')


@tool(
    name='hr.send_interview_reminder',
    title='Remind a candidate about an interview',
    description=(
        'Prepare a short reminder restating the time, format, duration and panel for a '
        'booked interview. Use it the day before, or whenever a candidate has gone '
        'quiet after accepting. Queued for approval.'),
    group='Candidate Communication',
    agent_types=('hr',),
    integration='gmail',
    requires_approval=True,
    risk='high',
    icon='fa-bell',
    parameters=_schema({
        'interview_id': _p('integer', 'The interview to remind about.'),
        'hours_before': _p('integer', 'How many hours ahead this is going out. '
                                      'Defaults to 24.'),
        'subject': _p('string', 'Subject line. One is written if omitted.'),
        'body': _p('string', 'Body. One is written if omitted.'),
    }, required=('interview_id',)),
)
def send_interview_reminder(ctx, interview_id, hours_before=24, subject='', body=''):
    interview = _interview(interview_id)
    if interview is None:
        return _not_found('Interview', interview_id, 'hr.list_interviews')

    candidate = interview.candidate
    blocked = _needs_email(candidate)
    if blocked:
        return blocked

    hours = max(1, _int(hours_before, 24) or 24)
    mode_label = {'video': 'a video call', 'phone': 'a telephone call',
                  'onsite': 'an in-person meeting at our office'}.get(
        interview.mode, 'a video call')
    panel = ', '.join(interview.interviewer_list)

    resolved_subject = str(subject or '').strip() or (
        f'Reminder: {interview.round_name}'
        + (f' on {interview.scheduled_at:%d %B}' if interview.scheduled_at else ''))

    if str(body or '').strip():
        resolved_body = str(body)
    else:
        first = candidate.full_name.split()[0] if candidate.full_name else 'there'
        lines = [f'Dear {first},', '',
                 f'A short reminder about your {interview.round_name.lower()} with us.']
        detail = [f'When: {interview.when_readable}.',
                  f'Format: {mode_label}.',
                  f'Length: {interview.duration_minutes} minutes.']
        if panel:
            detail.append(f'You will be meeting: {panel}.')
        if interview.location_or_link:
            detail.append(f'Where: {interview.location_or_link}.')
        lines.append('')
        lines.extend(detail)
        lines.extend(['',
                      'If anything has changed at your end, reply and let us know and '
                      'we will rearrange.',
                      '',
                      'We look forward to speaking with you.',
                      '',
                      'Kind regards,',
                      _SIGN_OFF])
        resolved_body = '\n'.join(lines)

    notes = []
    if not interview.scheduled_at:
        notes.append('This interview has no confirmed time, so a reminder about it '
                     'would confuse the candidate. Book it with hr.schedule_interview '
                     'before approving this.')
    if interview.status in ('cancelled', 'completed', 'no_show'):
        notes.append(f'The interview status is {interview.get_status_display()}, so a '
                     f'reminder is probably not what is wanted.')

    return Proposal(
        title=f'Reminder to {candidate.full_name} about {interview.round_name}',
        summary=(f'To {candidate.email}, roughly {hours} hours ahead of '
                 f'{interview.when_readable}. ' + ' '.join(notes)),
        payload={'candidate_id': candidate.pk,
                 'interview_id': interview.pk,
                 'to': candidate.email,
                 'subject': resolved_subject,
                 'body': resolved_body,
                 'hours_before': hours},
        editable_fields=_email_editables(),
        risk='high',
        subject_label='marketing.interview',
        subject_id=interview.pk,
        subject_display=f'{interview.round_name} with {candidate.full_name}',
        confirmation=(f'Prepared the reminder for interview #{interview.pk} to '
                      f'{candidate.email}. Queued for approval; nothing has been sent. '
                      + ' '.join(notes)))


@executor('hr.send_interview_reminder')
def execute_interview_reminder(action):
    payload = action.payload or {}
    to = str(payload.get('to') or '')
    subject = str(payload.get('subject') or '')
    body = str(payload.get('body') or '')

    result = integrations.call('gmail', 'send_email', to=to, subject=subject, body=body)
    _record_message(action, result, channel='email', recipient=to, subject=subject,
                    body=body,
                    metadata={'kind': 'interview_reminder',
                              'interview_id': payload.get('interview_id'),
                              'candidate_id': payload.get('candidate_id'),
                              'hours_before': payload.get('hours_before')})

    return _executed(result, f'Sent the interview reminder to {to}',
                     prepared_but_not_sent=f'The interview reminder to {to}')


# ===========================================================================
# GROUP: EMPLOYEES AND ONBOARDING
# ===========================================================================

@tool(
    name='hr.create_employee',
    title='Create an employee record',
    description=(
        'Create the staff record for a new starter, optionally carried over from the '
        'candidate who was hired so the reason for the hire stays reachable from the '
        'person. Status begins as onboarding. Acts immediately; no account is created '
        'and nobody is emailed.'),
    group='Employees & Onboarding',
    agent_types=('hr',),
    icon='fa-id-badge',
    parameters=_schema({
        'full_name': _p('string', 'Their name.'),
        'email': _p('string', 'Work or personal email address.'),
        'job_title': _p('string', 'Their job title.'),
        'department': _p('string', 'Which department.'),
        'start_date': _p('string', 'First day, as YYYY-MM-DD. Onboarding due dates '
                                   'are calculated from it.'),
        'manager_id': _p('integer', 'Employee id of their manager.'),
        'from_candidate_id': _p('integer', 'The candidate this hire came from.'),
        'location': _p('string', 'Where they are based.'),
    }, required=('full_name',)),
)
def create_employee(ctx, full_name, email='', job_title='', department='',
                    start_date=None, manager_id=None, from_candidate_id=None,
                    location=''):
    from ..models_hr import Employee

    name = str(full_name or '').strip()
    if not name:
        return ToolResult(ok=False, text='An employee record needs a name.',
                          error='full_name was empty')

    manager = _employee(manager_id) if manager_id else None
    if manager_id and manager is None:
        return _not_found('Employee', manager_id, 'hr.list_employees')

    candidate = _candidate(from_candidate_id) if from_candidate_id else None
    if from_candidate_id and candidate is None:
        return _not_found('Candidate', from_candidate_id, 'hr.list_candidates')

    first_day = None
    if start_date:
        first_day = _parse_date(start_date)
        if first_day is None:
            return ToolResult(
                ok=False, error='unreadable start date',
                text=f'Could not read "{start_date}" as a date. Give it as '
                     f'YYYY-MM-DD. Nothing was created.')

    employee = Employee.objects.create(
        full_name=name[:160],
        email=(str(email or '').strip() or (candidate.email if candidate else ''))[:254],
        job_title=(str(job_title or '').strip()
                   or (candidate.job_opening.title if candidate and candidate.job_opening
                       else ''))[:160],
        department=(str(department or '').strip()
                    or (candidate.job_opening.department
                        if candidate and candidate.job_opening else ''))[:100],
        location=(str(location or '').strip()
                  or (candidate.location if candidate else ''))[:140],
        manager=manager,
        employment_status='onboarding',
        start_date=first_day,
        from_candidate=candidate,
        skills=_plain_list(candidate.skills) if candidate else [])

    if candidate is not None and candidate.status != 'hired':
        candidate.status = 'hired'
        candidate.save(update_fields=['status', 'updated_at'])

    notes = []
    if candidate is not None:
        notes.append(f'Carried over from candidate #{candidate.pk}, now marked hired.')
    if first_day is None:
        notes.append('No start date was given, so onboarding tasks will have offsets '
                     'but no calendar dates. Add one with a start date to fix that.')
    else:
        notes.append(f'Start date {first_day:%d %B %Y}.')
    if manager is not None:
        notes.append(f'Reports to #{manager.pk} {manager.full_name}.')

    return ToolResult(
        ok=True,
        text=(f'Created employee #{employee.pk} {employee.full_name}, '
              f'{employee.job_title or "no title recorded"} in '
              f'{employee.department or "no department recorded"}, status onboarding. '
              + ' '.join(notes) +
              f' Next: hr.generate_onboarding_checklist employee_id={employee.pk}.'),
        data={'employee_id': employee.pk, 'full_name': employee.full_name,
              'email': employee.email, 'job_title': employee.job_title,
              'department': employee.department,
              'start_date': first_day.isoformat() if first_day else None,
              'manager_id': manager.pk if manager else None,
              'from_candidate_id': candidate.pk if candidate else None},
        subject_label='marketing.employee', subject_id=employee.pk)


@tool(
    name='hr.generate_onboarding_checklist',
    title='Build an onboarding checklist',
    description=(
        'Create the full set of onboarding tasks for a new starter across paperwork, '
        'equipment, systems access, introductions, training and compliance, each with '
        'an owner and a due date relative to their first day. Contracts and equipment '
        'land before day one, access on day one, training in week one and compliance '
        'in week two. Pass a role_hint to add role-specific tasks. Acts immediately.'),
    group='Employees & Onboarding',
    agent_types=('hr',),
    icon='fa-clipboard-check',
    parameters=_schema({
        'employee_id': _p('integer', 'The new starter.'),
        'role_hint': _p('string', 'Their role, so role-specific tasks are added, '
                                  'e.g. "backend developer" or "support agent".'),
        'include_categories': dict(
            _STRING_LIST,
            description=('Restrict to these categories: paperwork, access, equipment, '
                         'training, introduction, compliance. Omit for all.')),
    }, required=('employee_id',)),
)
def generate_onboarding_checklist(ctx, employee_id, role_hint='',
                                  include_categories=None):
    from ..models_hr import OnboardingTask

    employee = _employee(employee_id)
    if employee is None:
        return _not_found('Employee', employee_id, 'hr.list_employees')

    hint = str(role_hint or '').strip() or employee.job_title
    rows = _onboarding_rows(hint, include_categories)
    if not rows:
        return ToolResult(
            ok=False, error='no tasks matched',
            text=f'No onboarding tasks matched the categories given '
                 f'({", ".join(_plain_list(include_categories)) or "none"}). Valid '
                 f'categories are paperwork, access, equipment, training, '
                 f'introduction and compliance.')

    existing = {task.title for task in employee.onboarding_tasks.all()}
    created, skipped = [], 0

    for order, (category, offset, title, owner, description) in enumerate(rows):
        if title in existing:
            skipped += 1
            continue
        due = (employee.start_date + timedelta(days=offset)) \
            if employee.start_date else None
        created.append(OnboardingTask.objects.create(
            employee=employee, title=title[:200], description=description,
            category=category, owner_role=owner[:120], due_offset_days=offset,
            due_date=due, status='todo', display_order=order,
            created_by_agent=_agent(ctx)))

    if not created:
        return ToolResult(
            ok=True,
            text=f'Employee #{employee.pk} {employee.full_name} already has all '
                 f'{skipped} of these onboarding tasks; nothing was added. See them '
                 f'with hr.list_employees or update one with '
                 f'hr.update_onboarding_task.',
            data={'employee_id': employee.pk, 'created': 0, 'already_present': skipped})

    by_category = {}
    for task in created:
        by_category.setdefault(task.get_category_display(), []).append(task)

    blocks = []
    for label, tasks in by_category.items():
        lines = '\n'.join(
            f'  #{task.pk} {task.title} -- {task.timing_label}'
            f'{", due " + task.due_date.strftime("%d %b %Y") if task.due_date else ""}'
            f' ({task.owner_role or "owner not set"})'
            for task in tasks)
        blocks.append(f'{label}:\n{lines}')

    date_note = (f'Due dates are calculated from a start date of '
                 f'{employee.start_date:%d %B %Y}.' if employee.start_date else
                 'This employee has no start date, so the tasks carry offsets but no '
                 'dates. Set a start date and regenerate to get real due dates.')

    return ToolResult(
        ok=True,
        text=(f'Created {len(created)} onboarding tasks for employee #{employee.pk} '
              f'{employee.full_name}'
              f'{f" (role read as {hint})" if hint else ""}'
              f'{f", skipping {skipped} that already existed" if skipped else ""}. '
              f'{date_note}\n' + '\n'.join(blocks)),
        data={'employee_id': employee.pk, 'created': len(created),
              'already_present': skipped,
              'tasks': [{'id': task.pk, 'title': task.title,
                         'category': task.category,
                         'owner_role': task.owner_role,
                         'due_offset_days': task.due_offset_days,
                         'due_date': task.due_date.isoformat()
                         if task.due_date else None,
                         'status': task.status} for task in created]},
        subject_label='marketing.employee', subject_id=employee.pk)


@tool(
    name='hr.update_onboarding_task',
    title='Update an onboarding task',
    description=(
        'Mark an onboarding task as todo, in_progress, done, blocked or skipped, with '
        'a note. Marking one blocked and saying what it is blocked on is more useful '
        'than leaving it open, because a blocked access request is the usual reason a '
        'new starter sits idle on day two.'),
    group='Employees & Onboarding',
    agent_types=('hr',),
    icon='fa-circle-check',
    parameters=_schema({
        'task_id': _p('integer', 'The task to update.'),
        'status': _p('string', 'todo, in_progress, done, blocked or skipped.'),
        'note': _p('string', 'What happened, or what it is blocked on.'),
    }, required=('task_id', 'status')),
)
def update_onboarding_task(ctx, task_id, status, note=''):
    from ..models_hr import OnboardingTask

    task = _onboarding_task(task_id)
    if task is None:
        return _not_found('Onboarding task', task_id,
                          'hr.list_employees followed by hr.generate_onboarding_checklist')

    allowed = [key for key, _label in OnboardingTask.STATUS_CHOICES]
    wanted = str(status or '').strip().lower()
    if wanted not in allowed:
        return ToolResult(
            ok=False, error=f'{status} is not a task status',
            text=f'"{status}" is not an onboarding task status. Use one of: '
                 f'{", ".join(allowed)}. Nothing was changed.')

    was = task.get_status_display()
    task.status = wanted
    stated = str(note or '').strip()
    if stated:
        stamp = timezone.now().strftime('%d %b %Y')
        task.description = (f'{task.description}\n' if task.description else '') + \
                           f'[{stamp}] {stated}'
    task.completed_at = timezone.now() if wanted == 'done' else None
    task.save(update_fields=['status', 'description', 'completed_at'])

    employee = task.employee
    remaining = employee.open_onboarding_count
    return ToolResult(
        ok=True,
        text=(f'Task #{task.pk} "{task.title}" for employee #{employee.pk} '
              f'{employee.full_name} moved from {was} to '
              f'{task.get_status_display()}'
              f'{". Note recorded: " + stated if stated else ""}. '
              f'{remaining} onboarding task{"s" if remaining != 1 else ""} still open, '
              f'onboarding {employee.onboarding_progress} per cent complete.'),
        data={'task_id': task.pk, 'status': task.status, 'previous_status': was,
              'employee_id': employee.pk, 'open_tasks': remaining,
              'progress_percent': employee.onboarding_progress},
        subject_label='marketing.employee', subject_id=employee.pk)


@tool(
    name='hr.list_employees',
    title='List employees',
    description=(
        'List staff with their ids, titles, departments, statuses and open onboarding '
        'task counts. Call this before any tool needing an employee_id or a '
        'manager_id.'),
    group='Employees & Onboarding',
    agent_types=('hr',),
    icon='fa-address-book',
    reads_only=True,
    parameters=_schema({
        'department': _p('string', 'Restrict to one department.'),
        'status': _p('string', 'onboarding, active, leave, notice or exited.'),
        'limit': _p('integer', 'How many to return. Defaults to 30.'),
    }),
)
def list_employees(ctx, department='', status='', limit=30):
    from ..models_hr import Employee

    rows = Employee.objects.select_related('manager')
    if department:
        rows = rows.filter(department__icontains=str(department))
    if status:
        rows = rows.filter(employment_status=str(status))
    rows = list(rows[:max(1, min(_int(limit, 30) or 30, 200))])

    if not rows:
        where = []
        if department:
            where.append(f'department {department}')
        if status:
            where.append(f'status {status}')
        return ToolResult(
            ok=True,
            text=f'No employees found{" for " + " and ".join(where) if where else ""}. '
                 f'Create one with hr.create_employee.',
            data={'employees': [], 'count': 0})

    lines, payload = [], []
    for row in rows:
        open_tasks = row.open_onboarding_count
        lines.append(
            f'#{row.pk} {row.full_name} -- {row.job_title or "no title"}, '
            f'{row.department or "no department"}, '
            f'{row.get_employment_status_display()}'
            f'{", starts " + row.start_date.strftime("%d %b %Y") if row.start_date else ""}'
            f'{", manager " + row.manager.full_name if row.manager else ""}'
            f'{f", {open_tasks} onboarding tasks open" if open_tasks else ""}, '
            f'{float(row.leave_balance_days):g} leave days')
        payload.append({'id': row.pk, 'full_name': row.full_name,
                        'email': row.email, 'job_title': row.job_title,
                        'department': row.department,
                        'status': row.employment_status,
                        'start_date': row.start_date.isoformat()
                        if row.start_date else None,
                        'manager_id': row.manager_id,
                        'open_onboarding_tasks': open_tasks,
                        'onboarding_progress': row.onboarding_progress,
                        'leave_balance_days': float(row.leave_balance_days)})

    return ToolResult(
        ok=True,
        text=f'{len(rows)} employee{"s" if len(rows) > 1 else ""}:\n' +
             '\n'.join(lines),
        data={'employees': payload, 'count': len(payload)})


@tool(
    name='hr.generate_performance_review_template',
    title='Create a performance review template',
    description=(
        'Create a review record for an employee and period with the sections a '
        'reviewer fills in, each with a prompt and a scale. The template is stored '
        'separately from the answers, so regenerating it can never erase what somebody '
        'has already written. Acts immediately.'),
    group='Employees & Onboarding',
    agent_types=('hr',),
    icon='fa-chart-line',
    parameters=_schema({
        'employee_id': _p('integer', 'Who the review is for.'),
        'period': _p('string', 'The period, e.g. "H1 2026" or "Probation".'),
        'sections': dict(_STRING_LIST,
                         description='Custom section titles. Omit for the standard set.'),
    }, required=('employee_id', 'period')),
)
def generate_performance_review_template(ctx, employee_id, period, sections=None):
    from ..models_hr import PerformanceReview

    employee = _employee(employee_id)
    if employee is None:
        return _not_found('Employee', employee_id, 'hr.list_employees')

    label = str(period or '').strip()
    if not label:
        return ToolResult(ok=False, error='no period given',
                          text='A review needs a period, e.g. "H1 2026".')

    rows = _generate_review_sections(sections)
    review = PerformanceReview.objects.create(
        employee=employee, period=label[:80],
        reviewer=employee.manager.full_name if employee.manager else '',
        sections=rows, responses={}, status='template',
        created_by_agent=_agent(ctx))

    listing = '\n'.join(f'{index}. {row["title"]}\n   {row["prompt"]}'
                        for index, row in enumerate(rows, start=1))

    return ToolResult(
        ok=True,
        text=(f'Created performance review #{review.pk} for employee #{employee.pk} '
              f'{employee.full_name}, period {review.period}, with {len(rows)} '
              f'sections. Reviewer recorded as '
              f'{review.reviewer or "nobody -- this employee has no manager on record"}. '
              f'Status is template: no answers have been written. Each section is '
              f'scored {_SCALE}.\n{listing}'),
        data={'review_id': review.pk, 'employee_id': employee.pk,
              'period': review.period, 'reviewer': review.reviewer,
              'sections': rows, 'status': review.status},
        subject_label='marketing.employee', subject_id=employee.pk)


@tool(
    name='hr.summarise_employee_feedback',
    title='Summarise feedback about an employee',
    description=(
        'Group free-text feedback into themes, separate the strengths from the '
        'development areas, and recommend one concrete action. Works sentence by '
        'sentence, because a single review usually contains both praise and criticism '
        'and a document-level verdict would average them into something nobody wrote. '
        'Where an employee_id is given without feedback_text, the notes on their most '
        'recent review are used.'),
    group='Employees & Onboarding',
    agent_types=('hr',),
    icon='fa-comment-dots',
    parameters=_schema({
        'employee_id': _p('integer', 'Who the feedback is about. Optional.'),
        'feedback_text': _p('string', 'The feedback to read. Paste it all in.'),
    }),
)
def summarise_employee_feedback(ctx, employee_id=None, feedback_text=''):
    employee = _employee(employee_id) if employee_id else None
    if employee_id and employee is None:
        return _not_found('Employee', employee_id, 'hr.list_employees')

    text = str(feedback_text or '').strip()
    source = 'the text supplied'
    if not text and employee is not None:
        review = employee.reviews.first()
        parts = []
        if review:
            parts.append(review.summary or '')
            parts.extend(str(value) for value in (review.responses or {}).values())
            source = f'performance review #{review.pk} ({review.period})'
        if employee.notes:
            parts.append(employee.notes)
            source = source if review else 'the notes on the employee record'
        text = '\n'.join(part for part in parts if part.strip())

    if not text:
        who = f'employee #{employee.pk} {employee.full_name}' if employee \
            else 'the arguments given'
        return ToolResult(
            ok=False, error='no feedback text',
            text=f'There is no feedback to read for {who}. Paste it into '
                 f'feedback_text.')

    outcome = _summarise_feedback(text)

    theme_lines = '\n'.join(
        f'  {row["theme"]}: {row["mentions"]} mention'
        f'{"s" if row["mentions"] != 1 else ""} '
        f'({row["positive"]} positive, {row["negative"]} critical). '
        f'e.g. "{row["examples"][0]}"' if row['examples'] else
        f'  {row["theme"]}: {row["mentions"]} mentions'
        for row in outcome['themes']) or '  no recognisable themes'

    strengths = '\n'.join(f'  - {line}' for line in outcome['strengths']) \
        or '  none stated positively'
    development = '\n'.join(f'  - {line}' for line in outcome['development_areas']) \
        or '  none stated critically'

    return ToolResult(
        ok=True,
        text=(f'Read {outcome["sentences_read"]} sentences of feedback'
              f'{f" about employee #{employee.pk} {employee.full_name}" if employee else ""} '
              f'from {source}.\n'
              f'Themes:\n{theme_lines}\n'
              f'Stated as strengths:\n{strengths}\n'
              f'Stated as development areas:\n{development}\n'
              f'Recommended action: {outcome["recommended_action"]}\n'
              f'These groupings come from word matching, not judgement -- read the '
              f'quoted sentences before repeating any of it to the employee.'),
        data={'employee_id': employee.pk if employee else None,
              'source': source, 'themes': outcome['themes'],
              'strengths': outcome['strengths'],
              'development_areas': outcome['development_areas'],
              'recommended_action': outcome['recommended_action']},
        subject_label='marketing.employee',
        subject_id=employee.pk if employee else 0)


@tool(
    name='hr.send_welcome_email',
    title='Welcome a new starter',
    description=(
        'Prepare a welcome message for a new starter with their first-day details: '
        'when to arrive, who is meeting them, what to bring and what the first day '
        'looks like. Where the body is not supplied one is written from the employee '
        'record and their onboarding checklist. Queued for approval.'),
    group='Employees & Onboarding',
    agent_types=('hr',),
    integration='gmail',
    requires_approval=True,
    risk='high',
    icon='fa-hand-sparkles',
    parameters=_schema({
        'employee_id': _p('integer', 'The new starter.'),
        'subject': _p('string', 'Subject line. One is written if omitted.'),
        'body': _p('string', 'Body. One is written if omitted.'),
    }, required=('employee_id',)),
)
def send_welcome_email(ctx, employee_id, subject='', body=''):
    employee = _employee(employee_id)
    if employee is None:
        return _not_found('Employee', employee_id, 'hr.list_employees')

    if not employee.email:
        return ToolResult(
            ok=False, error='employee has no email address',
            text=f'Employee #{employee.pk} {employee.full_name} has no email address '
                 f'on file, so nothing can be prepared. Add one first.')

    day_one = employee.start_date
    greeter = employee.manager.full_name if employee.manager else ''
    first_day_tasks = list(employee.onboarding_tasks.filter(
        due_offset_days__lte=0, category__in=('introduction', 'access', 'paperwork')))

    resolved_subject = str(subject or '').strip() or (
        f'Welcome to the team, {employee.full_name.split()[0] if employee.full_name else ""}'
        .strip())

    if str(body or '').strip():
        resolved_body = str(body)
    else:
        first = employee.full_name.split()[0] if employee.full_name else 'there'
        lines = [f'Dear {first},', '',
                 f'Welcome to the team. We are looking forward to having you with us'
                 f'{f" as {employee.job_title}" if employee.job_title else ""}'
                 f'{f" in {employee.department}" if employee.department else ""}.']
        detail = []
        if day_one:
            detail.append(f'Your first day is {day_one:%A %d %B %Y}.')
        else:
            detail.append('We will confirm your first day shortly.')
        if greeter:
            detail.append(f'{greeter} will meet you and take you through the day.')
        if employee.location:
            detail.append(f'Where: {employee.location}.')
        detail.append('Please bring photo identification and your bank and '
                      'superannuation details if you have not already sent them.')
        lines.extend(['', ' '.join(detail)])
        if first_day_tasks:
            lines.extend(['', 'What the first day looks like:'])
            lines.extend(f'  - {task.title}' for task in first_day_tasks[:5])
        lines.extend(['',
                      'Your laptop and system access will be ready when you arrive, so '
                      'there is nothing you need to set up beforehand.',
                      '',
                      'If anything is unclear before then, reply to this message and we '
                      'will sort it out.',
                      '',
                      'Kind regards,',
                      _SIGN_OFF])
        resolved_body = '\n'.join(lines)

    notes = []
    if not day_one:
        notes.append('There is no start date on this record, so the message cannot '
                     'state one. Set it before approving.')
    if not greeter:
        notes.append('No manager is recorded, so nobody is named as meeting them on '
                     'day one, which is the detail new starters remember.')
    if not employee.onboarding_tasks.exists():
        notes.append('No onboarding checklist exists yet. Run '
                     'hr.generate_onboarding_checklist so the promises in this message '
                     'are backed by tasks somebody owns.')

    return Proposal(
        title=f'Welcome message to {employee.full_name}',
        summary=(f'To {employee.email}'
                 f'{f", starting {day_one:%d %B %Y}" if day_one else ", no start date set"}. '
                 + ' '.join(notes)),
        payload={'employee_id': employee.pk,
                 'to': employee.email,
                 'subject': resolved_subject,
                 'body': resolved_body},
        editable_fields=_email_editables(),
        risk='high',
        subject_label='marketing.employee',
        subject_id=employee.pk,
        subject_display=employee.full_name,
        confirmation=(f'Prepared the welcome message for employee #{employee.pk} '
                      f'{employee.full_name} to {employee.email}. Queued for approval; '
                      f'nothing has been sent. ' + ' '.join(notes)))


@executor('hr.send_welcome_email')
def execute_welcome_email(action):
    payload = action.payload or {}
    to = str(payload.get('to') or '')
    subject = str(payload.get('subject') or '')
    body = str(payload.get('body') or '')

    result = integrations.call('gmail', 'send_email', to=to, subject=subject, body=body)
    _record_message(action, result, channel='email', recipient=to, subject=subject,
                    body=body,
                    metadata={'kind': 'welcome',
                              'employee_id': payload.get('employee_id')})

    return _executed(result, f'Sent the welcome message to {to}',
                     prepared_but_not_sent=f'The welcome message to {to}')


# ===========================================================================
# GROUP: HR OPERATIONS
# ===========================================================================

@tool(
    name='hr.answer_policy_question',
    title='Answer a policy question from the handbook',
    description=(
        'Search the company knowledge base for the documents covering a policy '
        'question and return the passages found with their titles and links. Use this '
        'for every question about leave, pay, conduct, probation, notice, expenses, '
        'working hours, remote work or anything else an employee could later quote back '
        'at us. Answer only from what this returns, and name the document. Where '
        'nothing relevant is found it says so, and no policy should then be '
        'improvised.'),
    group='HR Operations',
    agent_types=('hr',),
    integration='knowledge_base',
    icon='fa-book-open',
    reads_only=True,
    parameters=_schema({
        'question': _p('string', 'The question as the employee asked it.'),
        'department': _p('string', 'Their department, where it narrows the answer.'),
    }, required=('question',)),
)
def answer_policy_question(ctx, question, department=''):
    asked = str(question or '').strip()
    if not asked:
        return ToolResult(ok=False, error='no question given',
                          text='There is no question to look up.')

    query = f'{asked} {department}'.strip()
    hits, problem = _knowledge_search(query, limit=5)
    text, citations = _cited_answer(asked, hits, problem, subject='policy')

    return ToolResult(
        ok=bool(citations) or not problem,
        text=text,
        data={'question': asked, 'department': str(department or ''),
              'citations': citations, 'found': len(citations),
              'problem': problem})


@tool(
    name='hr.search_hr_documents',
    title='Search the HR documents',
    description=(
        'Search the knowledge base narrowed to policies, handbooks, procedures, '
        'guidelines and contracts. Use this when asked whether a document exists, or '
        'to find the right document before drafting anything that has to agree with '
        'it.'),
    group='HR Operations',
    agent_types=('hr',),
    integration='knowledge_base',
    icon='fa-magnifying-glass',
    reads_only=True,
    parameters=_schema({
        'query': _p('string', 'What to search for.'),
        'limit': _p('integer', 'How many documents. Defaults to 6.'),
    }, required=('query',)),
)
def search_hr_documents(ctx, query, limit=6):
    asked = str(query or '').strip()
    if not asked:
        return ToolResult(ok=False, error='no query given',
                          text='There is nothing to search for.')

    hits, problem = _knowledge_search(asked, limit=_int(limit, 6) or 6,
                                      doc_types=_POLICY_DOC_TYPES, strict=True)
    if problem:
        return ToolResult(ok=False, error=problem,
                          text=f'{problem} No HR document search could be run.',
                          data={'query': asked, 'citations': []})

    citations = [_citation(hit) for hit in hits]
    if not citations:
        return ToolResult(
            ok=True,
            text=(f'No HR document in the knowledge base matches "{asked}". Say so '
                  f'plainly rather than answering from memory, and recommend that a '
                  f'document covering it be written and loaded.'),
            data={'query': asked, 'citations': [], 'found': 0})

    lines = [f'{index}. "{row["title"]}"'
             f'{" [" + row["doc_type"] + "]" if row["doc_type"] else ""}'
             f'{" " + row["url"] if row["url"] else ""}\n   "{row["snippet"]}"'
             for index, row in enumerate(citations, start=1)]

    return ToolResult(
        ok=True,
        text=(f'{len(citations)} HR document'
              f'{"s" if len(citations) > 1 else ""} match "{asked}":\n'
              + '\n'.join(lines)),
        data={'query': asked, 'citations': citations, 'found': len(citations)})


@tool(
    name='hr.generate_leave_policy_answer',
    title='Answer a leave question',
    description=(
        'Answer a question about leave from the leave policy in the knowledge base, '
        'and where an employee is named include their recorded leave balance. Use this '
        'rather than answering from memory: an entitlement stated wrongly is a real '
        'problem when it is quoted back months later. The balance shown is what the '
        'platform holds, which is not the payroll system.'),
    group='HR Operations',
    agent_types=('hr',),
    integration='knowledge_base',
    icon='fa-umbrella-beach',
    reads_only=True,
    parameters=_schema({
        'question': _p('string', 'The leave question as it was asked.'),
        'employee_id': _p('integer', 'Whose balance to include. Optional.'),
    }, required=('question',)),
)
def generate_leave_policy_answer(ctx, question, employee_id=None):
    asked = str(question or '').strip()
    if not asked:
        return ToolResult(ok=False, error='no question given',
                          text='There is no leave question to look up.')

    employee = _employee(employee_id) if employee_id else None
    if employee_id and employee is None:
        return _not_found('Employee', employee_id, 'hr.list_employees')

    hits, problem = _knowledge_search(f'leave policy {asked}', limit=5,
                                      doc_types=_POLICY_DOC_TYPES)
    text, citations = _cited_answer(asked, hits, problem, subject='leave policy')

    balance_line = ''
    if employee is not None:
        balance_line = (
            f'\nEmployee #{employee.pk} {employee.full_name} has '
            f'{float(employee.leave_balance_days):g} leave days recorded in this '
            f'platform, and is currently '
            f'{employee.get_employment_status_display().lower()}'
            f'{f" with a start date of {employee.start_date:%d %B %Y}" if employee.start_date else ""}. '
            f'This figure is the platform record, not the payroll system, so say so '
            f'when quoting it.')

    return ToolResult(
        ok=bool(citations) or not problem,
        text=text + balance_line,
        data={'question': asked,
              'employee_id': employee.pk if employee else None,
              'leave_balance_days': float(employee.leave_balance_days)
              if employee else None,
              'employment_status': employee.employment_status if employee else '',
              'citations': citations, 'found': len(citations), 'problem': problem},
        subject_label='marketing.employee',
        subject_id=employee.pk if employee else 0)


@tool(
    name='hr.send_hr_announcement',
    title='Announce something to the company',
    description=(
        'Prepare a People Operations announcement for Slack, email, or both. Reaches '
        'everybody, so it is queued for approval with the full text attached. On '
        'approval it is sent and recorded as a published announcement, so what staff '
        'were told stays answerable later.'),
    group='HR Operations',
    agent_types=('hr',),
    integration='slack',
    requires_approval=True,
    risk='high',
    icon='fa-bullhorn',
    parameters=_schema({
        'title': _p('string', 'The headline.'),
        'body': _p('string', 'The announcement itself.'),
        'channel': _p('string', 'slack, email or both.'),
        'audience': _p('string', 'Who it is for. Defaults to "All staff".'),
    }, required=('title', 'body')),
)
def send_hr_announcement(ctx, title, body, channel='slack', audience='All staff'):
    from ..models_hr import Employee

    headline = str(title or '').strip()
    text = str(body or '').strip()
    if not headline or not text:
        return ToolResult(ok=False, error='title and body are both required',
                          text='An announcement needs both a title and a body.')

    where = str(channel or 'slack').strip().lower()
    if where not in ('slack', 'email', 'both'):
        return ToolResult(
            ok=False, error=f'{channel} is not a channel',
            text=f'"{channel}" is not a channel. Use slack, email or both.')

    recipients = []
    if where in ('email', 'both'):
        recipients = [row.email for row in Employee.objects.filter(
            employment_status__in=('onboarding', 'active', 'leave')).exclude(email='')]

    slack_channel = ''
    if where in ('slack', 'both'):
        integration = integrations.get_integration('slack')
        slack_channel = str((integration.config or {}).get('default_channel')
                            if integration else '') or '#general'

    notes = []
    if where in ('email', 'both') and not recipients:
        notes.append('No employee email addresses are on file, so the email half of '
                     'this cannot go anywhere. Add the recipients before approving.')
    if where in ('slack', 'both') and not slack_channel:
        notes.append('No default Slack channel is configured on the integration.')

    composed = f'*{headline}*\n\n{text}\n\n{_SIGN_OFF}'

    return Proposal(
        title=f'Announcement to {audience or "all staff"}: {headline}',
        summary=(f'Channel: {where}. Audience: {audience or "All staff"}. '
                 f'{f"Slack {slack_channel}. " if slack_channel else ""}'
                 f'{f"{len(recipients)} email recipients. " if recipients else ""}'
                 + ' '.join(notes)),
        payload={'title': headline, 'body': text, 'channel': where,
                 'audience': str(audience or 'All staff'),
                 'slack_channel': slack_channel,
                 'slack_text': composed,
                 'email_to': ', '.join(recipients),
                 'email_subject': headline},
        editable_fields=[
            editable('title', 'Headline'),
            editable('body', 'Announcement', 'longtext', rows=12),
            editable('audience', 'Audience'),
            editable('slack_channel', 'Slack channel'),
            editable('slack_text', 'Slack message as it will appear', 'longtext',
                     rows=12),
            editable('email_to', 'Email recipients', 'longtext', rows=3,
                     help_text='Comma separated.'),
            editable('email_subject', 'Email subject'),
        ],
        risk='high',
        subject_label='marketing.hrannouncement',
        subject_id=0,
        subject_display=headline[:200],
        confirmation=(f'Prepared the announcement "{headline}" for {where} and queued '
                      f'it for approval. Nothing has been posted or sent. '
                      + ' '.join(notes)))


@executor('hr.send_hr_announcement')
def execute_hr_announcement(action):
    from ..models_hr import HRAnnouncement

    payload = action.payload or {}
    where = str(payload.get('channel') or 'slack').lower()
    headline = str(payload.get('title') or 'Announcement')
    text = str(payload.get('body') or '')
    slack_text = str(payload.get('slack_text') or f'*{headline}*\n\n{text}')
    slack_channel = str(payload.get('slack_channel') or '#general')
    recipients = [part.strip() for part in
                  str(payload.get('email_to') or '').replace(';', ',').split(',')
                  if part.strip()]
    email_subject = str(payload.get('email_subject') or headline)

    done, demo, failures = [], False, []

    if where in ('slack', 'both'):
        result = integrations.call('slack', 'post_message', channel=slack_channel,
                                   text=slack_text)
        _record_message(action, result, channel='slack', recipient=slack_channel,
                        subject=headline, body=slack_text,
                        metadata={'kind': 'hr_announcement'})
        demo = demo or result.demo
        if result.ok:
            done.append(f'posted to Slack {slack_channel}')
        else:
            failures.append(f'Slack: {result.error}')

    if where in ('email', 'both'):
        if not recipients:
            failures.append('email: no recipients were listed')
        for address in recipients:
            result = integrations.call('gmail', 'send_email', to=address,
                                       subject=email_subject, body=text)
            _record_message(action, result, channel='email', recipient=address,
                            subject=email_subject, body=text,
                            metadata={'kind': 'hr_announcement'})
            demo = demo or result.demo
            if not result.ok:
                failures.append(f'{address}: {result.error}')
        sent = len(recipients) - len([f for f in failures if '@' in f])
        if sent > 0:
            done.append(f'emailed {sent} recipient{"s" if sent != 1 else ""}')

    announcement = HRAnnouncement.objects.create(
        title=headline[:200], body=text, channel=where,
        audience=str(payload.get('audience') or 'All staff')[:140],
        status='published' if done else 'draft',
        published_at=timezone.now() if done else None,
        created_by_agent=action.agent)

    if not done:
        return ToolResult(
            ok=False, demo=demo,
            error='; '.join(failures) or 'nothing was sent',
            text=(f'The announcement "{headline}" did not go out: '
                  f'{"; ".join(failures) or "no channel produced a result"}. '
                  f'Recorded as announcement #{announcement.pk} in draft.'),
            data={'announcement_id': announcement.pk, 'failures': failures})

    marker = (' (simulated -- the integrations are not connected, so nothing left the '
              'building)' if demo else '')
    tail = f' Some deliveries failed: {"; ".join(failures)}.' if failures else ''
    return ToolResult(
        ok=True, demo=demo,
        text=(f'Published announcement #{announcement.pk} "{headline}": '
              f'{" and ".join(done)}{marker}.{tail}'),
        data={'announcement_id': announcement.pk, 'channel': where,
              'delivered': done, 'failures': failures})


@tool(
    name='hr.notify_hiring_manager',
    title='Message the hiring manager',
    description=(
        'Prepare a Slack message to the hiring manager about an opening: a shortlist '
        'is ready, a candidate has withdrawn, interviews are booked. Keep it short and '
        'specific -- what, about which opening, and what you need from them. Queued for '
        'approval.'),
    group='HR Operations',
    agent_types=('hr',),
    integration='slack',
    requires_approval=True,
    risk='high',
    icon='fa-paper-plane',
    parameters=_schema({
        'job_opening_id': _p('integer', 'The opening this is about.'),
        'message': _p('string', 'What you need to tell them.'),
        'channel': _p('string', 'Slack channel or user handle. Defaults to the '
                                'integration default channel.'),
    }, required=('job_opening_id', 'message')),
)
def notify_hiring_manager(ctx, job_opening_id, message, channel=''):
    opening = _opening(job_opening_id)
    if opening is None:
        return _not_found('Job opening', job_opening_id, 'hr.list_job_openings')

    text = str(message or '').strip()
    if not text:
        return ToolResult(ok=False, error='no message given',
                          text='There is nothing to send. Write the message.')

    target = str(channel or '').strip()
    if not target:
        integration = integrations.get_integration('slack')
        target = str((integration.config or {}).get('default_channel')
                     if integration else '') or '#general'

    manager = opening.hiring_manager or 'the hiring manager'
    composed = (f'*{opening.title}* (opening #{opening.pk}, '
                f'{opening.get_status_display()})\n{text}\n\n'
                f'{opening.candidate_count} candidates, '
                f'{opening.shortlisted_count} shortlisted or further along. '
                f'-- {_SIGN_OFF}')

    notes = []
    if not opening.hiring_manager:
        notes.append('No hiring manager is recorded on this opening, so this goes to '
                     'the channel rather than to a named person.')

    return Proposal(
        title=f'Slack {manager} about {opening.title}',
        summary=f'To {target}. {text[:200]} ' + ' '.join(notes),
        payload={'job_opening_id': opening.pk, 'channel': target,
                 'text': composed, 'message': text},
        editable_fields=[
            editable('channel', 'Slack channel or handle'),
            editable('text', 'Message as it will appear', 'longtext', rows=10),
        ],
        risk='high',
        subject_label='marketing.jobopening',
        subject_id=opening.pk,
        subject_display=opening.title,
        confirmation=(f'Prepared a Slack message to {target} about opening '
                      f'#{opening.pk} "{opening.title}" and queued it for approval. '
                      f'Nothing has been posted. ' + ' '.join(notes)))


@executor('hr.notify_hiring_manager')
def execute_notify_hiring_manager(action):
    payload = action.payload or {}
    target = str(payload.get('channel') or '#general')
    text = str(payload.get('text') or '')

    result = integrations.call('slack', 'post_message', channel=target, text=text)
    _record_message(action, result, channel='slack', recipient=target,
                    subject='Hiring update', body=text,
                    metadata={'kind': 'hiring_manager_notification',
                              'job_opening_id': payload.get('job_opening_id')})

    return _executed(result, f'Posted the hiring update to {target}',
                     prepared_but_not_sent=f'The hiring update to {target}')


@tool(
    name='hr.upload_hr_document',
    title='Upload a document to Drive',
    description=(
        'Prepare the upload of an HR document -- a position description, an onboarding '
        'plan, a policy draft -- to the shared Drive so colleagues can read it. Changes '
        'a company system rather than reaching a person outside it, so it is queued for '
        'approval at medium risk.'),
    group='HR Operations',
    agent_types=('hr',),
    integration='google_drive',
    requires_approval=True,
    risk='medium',
    icon='fa-file-arrow-up',
    parameters=_schema({
        'name': _p('string', 'The file name, including an extension.'),
        'content': _p('string', 'The document text.'),
        'folder_id': _p('string', 'Drive folder id. Defaults to the configured one.'),
    }, required=('name', 'content')),
)
def upload_hr_document(ctx, name, content, folder_id=''):
    filename = str(name or '').strip()
    body = str(content or '')
    if not filename or not body.strip():
        return ToolResult(
            ok=False, error='name and content are both required',
            text='An upload needs both a file name and some content.')

    folder = str(folder_id or '').strip()
    if not folder:
        integration = integrations.get_integration('google_drive')
        folder = str((integration.config or {}).get('folder_id')
                     if integration else '')

    words = len(body.split())
    counted = f'{words} word{"s" if words != 1 else ""}'
    return Proposal(
        title=f'Upload "{filename}" to Drive',
        summary=(f'{counted}, {len(body)} characters, into '
                 f'{folder or "the default Drive location"}. '
                 f'Anybody with access to that folder will be able to read it.'),
        payload={'name': filename, 'content': body, 'folder_id': folder},
        editable_fields=[
            editable('name', 'File name'),
            editable('folder_id', 'Drive folder id'),
            editable('content', 'Document content', 'longtext', rows=20),
        ],
        risk='medium',
        subject_label='marketing.hrdocument',
        subject_id=0,
        subject_display=filename[:200],
        confirmation=(f'Prepared the upload of "{filename}" ({counted}) and queued '
                      f'it for approval. Nothing has been uploaded.'))


@executor('hr.upload_hr_document')
def execute_upload_hr_document(action):
    payload = action.payload or {}
    filename = str(payload.get('name') or 'document.txt')
    body = str(payload.get('content') or '')
    folder = str(payload.get('folder_id') or '')

    result = integrations.call('google_drive', 'upload_file', name=filename,
                               content=body, folder_id=folder)

    return _executed(result,
                     f'Uploaded "{filename}" ({len(body.split())} words) to '
                     f'{folder or "the default Drive location"}',
                     prepared_but_not_sent=f'The upload of "{filename}"')

