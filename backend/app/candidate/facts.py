from datetime import datetime
from copy import deepcopy
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import CandidateFact, CandidateProfile


def store_user_fact(db: Session, profile: CandidateProfile, path: str, value: Any) -> CandidateFact:
    current = deepcopy(profile.data or {})
    node = current
    parts = path.split(".")
    for index, part in enumerate(parts):
        last = index == len(parts) - 1
        next_is_index = not last and parts[index + 1].isdigit()
        if isinstance(node, list):
            if not part.isdigit() or int(part) > len(node):
                raise ValueError('A global list fact requires a canonical existing or next item index; use application-only scope otherwise')
            position = int(part)
            if position == len(node):
                node.append(None if last else [] if next_is_index else {})
            if last:
                node[position] = value
                break
            if not isinstance(node[position], (dict, list)):
                raise ValueError('The global fact path conflicts with the candidate profile structure')
            node = node[position]
        elif isinstance(node, dict):
            if last:
                node[part] = value
                break
            if part not in node or node[part] is None:
                node[part] = [] if next_is_index else {}
            if not isinstance(node[part], (dict, list)):
                raise ValueError('The global fact path conflicts with the candidate profile structure')
            node = node[part]
        else:
            raise ValueError('The global fact path is not a candidate container')
    profile.data = current
    fact = db.execute(select(CandidateFact).where(
        CandidateFact.profile_id == profile.id,
        CandidateFact.path == path,
    )).scalar_one_or_none()
    if fact is None:
        fact = CandidateFact(
            profile_id=profile.id, path=path, concept=path.replace(".", "_"),
            value=value, source="USER_ENTERED", confidence=1.0,
            user_confirmed=True, confirmed_at=datetime.utcnow(),
            last_confirmed_at=datetime.utcnow(),
        )
        db.add(fact)
    else:
        fact.value = value
        fact.source = "USER_ENTERED"
        fact.confidence = 1.0
        fact.user_confirmed = True
        fact.confirmed_at = datetime.utcnow()
        fact.last_confirmed_at = datetime.utcnow()
    return fact
