"""Stable field identity and structure, excluding volatile answers and ordinals."""
from app.utils.hashing import stable_hash


def field_key(field):
    anchor = (field.metadata.get('group_name') or field.metadata.get('html_id')
              or field.metadata.get('name') or field.metadata.get('ownership_selector'))
    return 'k' + stable_hash([field.frame, anchor, field.question, field.control_kind])[:24]


def structure(form):
    def persistent_options(field):
        # Lazy overlay options disappear when their popup closes. Their
        # presentation lifecycle is not a change to the form's structure.
        if field.control_kind in {'CUSTOM_SELECT', 'COMBOBOX', 'AUTOCOMPLETE'}:
            return []
        return [(option.label, option.disabled) for option in field.options]
    return stable_hash([
        [(field_key(f), f.required, persistent_options(f)) for f in form.fields],
        [(a.get('label'), a.get('type')) for a in form.actions if a.get('type') in {'next', 'submit'}],
    ])
