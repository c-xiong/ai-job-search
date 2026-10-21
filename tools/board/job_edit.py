"""Validation and posting storage for owner edits. No network calls."""
from datetime import date
from . import add_job, state


def validate(raw):
    if not isinstance(raw, dict):
        raise ValueError('Job details must be an object')
    allowed = {'title': 200, 'company': 200, 'location': 300,
               'posted': 10, 'deadline': 10, 'description': 60000}
    if set(raw) - set(allowed):
        raise ValueError('Unknown job field')
    out = {}
    for key, value in raw.items():
        if not isinstance(value, str) or len(value) > allowed[key]:
            raise ValueError('%s must be text of at most %s characters' % (key, allowed[key]))
        value = value.strip()
        if key == 'title' and not add_job.clean_title(value):
            raise ValueError('Enter a valid job title (not a browser error)')
        if key == 'company' and not value:
            raise ValueError('Company is required')
        if key in ('posted', 'deadline') and value:
            try:
                if date.fromisoformat(value).isoformat() != value:
                    raise ValueError()
            except ValueError:
                raise ValueError('%s must be YYYY-MM-DD' % key)
        out[key] = value if key == 'description' else ' '.join(value.split())
    return out


def apply(entry, url, fields):
    body = fields.get('description')
    entry.update({k: v for k, v in fields.items() if k != 'description'})
    if body is not None:
        entry['posting_manual'] = True
        if body:
            state.postings.commit(url, body)
        for key in state.postings.ENTRY_FIELDS:
            entry.pop(key, None)
        entry.update(state.postings.describe(url, body, source='manual', extractor='job-editor'))
