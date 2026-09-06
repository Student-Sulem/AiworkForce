"""Template filters for rendering chat content.

Registered automatically because the module lives in a `templatetags` package
inside an installed app. Load it in a template with {% load chat_extras %}.
"""

from django import template

from .. import markdown as md

register = template.Library()


@register.filter(name='markdown')
def markdown(value):
    """Render an AI reply's Markdown as safe HTML.

    Safety is argued in marketing/markdown.py: the input is escaped before any
    tag is generated, so the filter cannot emit markup the model supplied.
    """
    return md.render(value)
