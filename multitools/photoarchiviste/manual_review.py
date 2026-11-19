"""Choix manuels : la photo courante ou tous ses exemplaires exacts."""
from .tag_routing import Route


def manual_targets(rows,photo,include_exact=False):
    if include_exact and not photo.sha256:
        raise ValueError('Réindexer avant de traiter un groupe de doublons.')
    current=next((row for row in rows if row['photo'].path==photo.path and not row['manifest']),None)
    if current is None:
        return []
    if (current['photo'].sha256,current['photo'].size)!=(photo.sha256,photo.size):
        raise ValueError('La photo affichée ne correspond plus à la liste. Actualiser la vue.')
    if not include_exact:
        return [current]
    return [row for row in rows if not row['manifest']
            and (row['photo'].sha256,row['photo'].size)==(photo.sha256,photo.size)]


def with_manual_rejections(plan,rows,destination):
    """Les rejets explicitement choisis ne dépendent pas d'une analyse IA."""
    if len(plan)!=len(rows):
        raise ValueError('Plan incohérent avec les photos sélectionnées.')
    result=[]
    for route,row in zip(plan,rows):
        if route.photo.path!=row['photo'].path:
            raise ValueError('Correspondance de plan invalide.')
        if row['decision']=='a_jeter':
            if destination is None:
                route=Route(route.photo,None,'Choisir le dossier unique À jeter')
            elif route.photo.path.is_relative_to(destination):
                route=Route(route.photo,None,'Déjà dans le dossier À jeter')
            else:
                route=Route(route.photo,destination,'Rejet manuel validé')
        elif row['decision']=='a_revoir':
            route=Route(route.photo,None,'À revoir — décision manuelle')
        elif row['decision']=='garder' and route.status=='Choisir le dossier unique À jeter':
            route=Route(route.photo,None,'Garder — tags à valider')
        result.append(route)
    return result
