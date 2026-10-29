"""Ground compound language controls in persisted facts; never infer CEFR."""
from app.forms.answer_engine import ProposedAnswer, _fold
from app.candidate.fact_resolver import CandidateFactResolver


def exact_option(field, value):
    matches = [o.id for o in field.options if not o.disabled and _fold(o.label) == _fold(str(value or ''))]
    return matches[0] if len(matches) == 1 else None


async def plan_collections(engine, candidate, fields, executor):
    overrides = {}
    commits = {}
    if not any(f.metadata.get('compound_role') == 'language' for f in fields):
        return overrides, commits
    resolver = CandidateFactResolver(candidate.data, engine._facts(candidate.id).values())
    memories = engine._memory(candidate.id)
    for language_field in fields:
        if language_field.metadata.get('compound_role') != 'language':
            continue
        level_field = next(f for f in fields if f.metadata.get('compound_id') == language_field.metadata['compound_id']
                           and f.metadata.get('compound_role') == 'level')
        for f in (language_field, level_field):
            overrides[f.id] = ProposedAnswer(f.id, f.question, None, 'COLLECTION', 1, action='skip')
        languages = (candidate.data or {}).get('languages') or []
        if not languages and (language_field.required or level_field.required):
            languages = [{}]  # Questions, never an inferred candidate language.
        for index, language in enumerate(languages):
            if not isinstance(language, dict):
                continue
            name_path = 'languages.%d.language' % index
            level_path = 'languages.%d.level' % index
            name = resolver.resolve(name_path)
            level = resolver.resolve(level_path)
            name_value = name.value if name.exists else None
            name_source = name.source
            source = level.source
            value = level.value if level.exists and level.source in {'CV_EXPLICIT', 'USER_ENTERED', 'USER_CONFIRMED'} else None
            for memory in memories.values():
                if getattr(memory, 'fact_path', None) == name_path:
                    name_value = memory.value
                    name_source = 'USER_CONFIRMED'
                if getattr(memory, 'fact_path', None) == level_path:
                    value = memory.value
                    source = 'USER_CONFIRMED'
            language_option = exact_option(language_field, name_value)
            level_option = exact_option(level_field, value)
            if language_option and level_option and await executor.collection_contains(
                    language_field, language_option, level_field, level_option):
                continue
            overrides[language_field.id] = ProposedAnswer(
                language_field.id, language_field.question, language_option, name_source or 'NEEDS_USER', 1,
                needs_user=language_option is None, source_paths=[name_path],
                requested_fact_path=name_path, action='select_option', execution_confidence='EXACT')
            overrides[level_field.id] = ProposedAnswer(
                level_field.id, '%s — %s' % (name_value or language_field.question, level_field.question),
                level_option, source or 'NEEDS_USER', 1, needs_user=level_option is None,
                source_paths=[level_path] if value is not None else [], requested_fact_path=level_path,
                reason='An exact language level is required; proficiency descriptions do not imply CEFR.',
                action='select_option', execution_confidence='EXACT')
            if language_option and level_option:
                commits[level_field.id] = (language_field, language_option, level_field, level_option)
            break  # One verified UI addition, then rescan before planning the next.
    return overrides, commits
