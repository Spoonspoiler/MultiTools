"""Plan déterministe issu des réponses humaines, jamais des tags IA seuls."""
from dataclasses import dataclass
from pathlib import Path

from .inventory import Photo


@dataclass(frozen=True)
class Route:
    photo: Photo
    destination: Path | None
    status: str


def plan_routes(store,context,photos,rejections=None,gate=None):
    plan=[]
    for photo in photos:
        if gate is not None: gate.check()
        question=store.question(context,photo)
        answer=question['answer'] if question else None
        if not answer:
            plan.append(Route(photo,None,'Tags / décision non validés'))
            continue
        if answer['decision']=='a_revoir':
            plan.append(Route(photo,None,'À revoir'))
            continue
        if answer['decision']=='a_jeter':
            destination=Path(rejections) if rejections else None
            if destination is None:
                plan.append(Route(photo,None,'Choisir le dossier unique À jeter'))
                continue
        else:
            selected=question['destination']
            if not selected:
                reason='Plusieurs destinations : choisir dans la question' if len(question['destinations'])>1 else 'Conservée dans la source — aucun tag associé à un dossier'
                plan.append(Route(photo,None,reason))
                continue
            destination=Path(selected)
        if photo.path.is_relative_to(destination):
            plan.append(Route(photo,None,'Déjà dans le dossier cible'))
        else:
            plan.append(Route(photo,destination,'Classement validé'))
    return plan
