"""Un groupe visible et une répartition explicite, sans décision sur fichiers."""


def comparison_group(rows,current,related=()):
    active={row['photo'].path:row['photo'] for row in rows if not row['manifest']}
    actual=active.get(current.path)
    if actual is None: return ()
    if (actual.sha256,actual.size)!=(current.sha256,current.size):
        raise ValueError('La photo affichée a changé dans l’index. Actualiser la comparaison.')
    paths={current.path}
    paths.update(photo.path for photo in related if photo.path in active)
    if current.sha256:
        paths.update(photo.path for photo in active.values()
                     if (photo.sha256,photo.size)==(current.sha256,current.size))
    return tuple(sorted((active[path] for path in paths),key=lambda p:(-p.width*p.height,str(p.path))))


def partition_group(rows,photos,keepers,*,allow_empty=False):
    active={row['photo'].path:row for row in rows if not row['manifest']}
    group={photo.path:photo for photo in photos}
    selected=set(keepers)
    if not group or not selected.issubset(group):
        raise ValueError('Sélection hors du groupe affiché.')
    if not selected and not allow_empty:
        raise ValueError('Cocher au moins une photo à garder ou choisir explicitement Tout rejeter.')
    keep=[]; reject=[]
    for path,photo in group.items():
        row=active.get(path)
        if row is None or (row['photo'].sha256,row['photo'].size)!=(photo.sha256,photo.size):
            raise ValueError('Le groupe a changé. Recharger la comparaison avant de décider.')
        (keep if path in selected else reject).append(row)
    return keep,reject


def highest_resolution(photos):
    area=max((photo.width*photo.height for photo in photos),default=0)
    return {photo.path for photo in photos if area>0 and photo.width*photo.height==area}
