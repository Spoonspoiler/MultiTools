"""Tri validé : extraire vers un dossier unique, sans effacement ni écrasement."""
import json
import os
from pathlib import Path
import tempfile
from uuid import uuid4

from PIL import Image, ImageDraw, ImageFont

from .exact_duplicates import Fingerprint, fingerprint, _verify, _rename
from .path_safety import checked_stat, directory_handle, volume_at, file_evidence
from .preview import _thumbnail

JOURNAL = '.multitools-journal'


def save_review(path, rows, destination=None):
    """État du tri séparé des archives, écrit atomiquement dans le travail."""
    path = Path(path)
    entries = [{'path':str(row['photo'].path),'sha256':row['photo'].sha256,
                'decision':row['decision'],'proposal':row['proposal'],'reason':row['reason'],
                'manifest':str(row['manifest']) if row['manifest'] else None} for row in rows]
    record = {'version':1,'destination':str(destination) if destination else None,'photos':entries}
    with tempfile.NamedTemporaryFile(mode='w',encoding='utf-8',prefix='.tri-',dir=path.parent,delete=False) as stream:
        pending=Path(stream.name)
        json.dump(record,stream,ensure_ascii=False,indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    pending.replace(path)
    with directory_handle(path.parent) as fd:
        if fd is not None: os.fsync(fd)


def read_review(path):
    try:
        record=json.loads(Path(path).read_text(encoding='utf-8'))
    except FileNotFoundError:
        return {},None
    if record.get('version') != 1 or not isinstance(record.get('photos'),list):
        raise ValueError('Journal de décisions invalide.')
    allowed={'garder','a_jeter','a_revoir'}
    entries={}
    for item in record['photos']:
        if item.get('decision') not in allowed or item.get('proposal') not in allowed:
            raise ValueError('Décision inconnue dans le journal.')
        entries[(item['path'],item['sha256'])]=item
    return entries,record.get('destination')


AI_SCHEMA = {'type':'object', 'properties':{'photos':{'type':'array','items':{
    'type':'object','properties':{'id':{'type':'integer'},
    'decision':{'type':'string','enum':['garder','a_jeter','a_revoir']},
    'raison':{'type':'string'}},'required':['id','decision','raison'],'additionalProperties':False}}},
    'required':['photos'],'additionalProperties':False}


def suggest(client, model, photos, preferences, gate):
    """Une planche de six miniatures maximum ; résultat consultatif uniquement."""
    if not 1 <= len(photos) <= 6:
        raise ValueError('Sélectionner entre une et six photos pour la comparaison IA.')
    sheet = Image.new('RGB',(600,220*((len(photos)+2)//3)), 'white')
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.load_default(size=18)
    metadata = []
    valid = set()
    for index,photo in enumerate(photos,1):
        gate.check()
        x,y = ((index-1)%3)*200,((index-1)//3)*220
        try:
            thumb = _thumbnail(photo.path,(190,185)).convert('RGB')
            sheet.paste(thumb,(x+(200-thumb.width)//2,y+25))
            valid.add(index)
        except OSError:
            pass
        draw.text((x+5,y+2),f'Photo {index}',fill='black',font=font)
        metadata.append(f'{index}: {photo.path.name}, {photo.width}x{photo.height}, {photo.format}')
    if not valid:
        raise OSError('Aucune image lisible dans la sélection.')
    with tempfile.TemporaryDirectory(prefix='multitools-planche-') as folder:
        path = Path(folder)/'comparaison.jpg'
        sheet.save(path,quality=85)
        messages = [{'role':'system','content':
            'Tu aides à trier des photos. Réponds en français dans le schéma JSON demandé. '
            'Propose garder, a_jeter ou a_revoir pour chaque numéro. Compare les variantes visibles. '
            'Une ressemblance ne prouve pas un doublon. Une miniature ne prouve pas la qualité de l’original. '
            'Ne rejette pas un souvenir sur la seule esthétique. Si le contexte manque, choisis a_revoir. '
            'Tu proposes uniquement, tu ne commandes aucune opération de fichier.'},
            {'role':'user','content':f'Priorités du propriétaire : {preferences}\n'
             + '\n'.join(metadata) + '\nJustifie brièvement chaque proposition.'}]
        result = client.chat(model,messages,lambda text:None,gate.stopped,image=path,context=2048,schema=AI_SCHEMA)
    content = json.loads(result.text)
    answers = {}
    for item in content.get('photos',[]):
        index = item.get('id')
        if type(index) is not int or index not in valid or index in answers:
            raise ValueError('La réponse IA contient des numéros invalides ou répétés.')
        if item.get('decision') not in {'garder','a_jeter','a_revoir'}:
            raise ValueError('Proposition IA invalide.')
        answers[index] = (item['decision'],str(item.get('raison',''))[:1200])
    return [answers.get(i,('a_revoir','Image non évaluée ou illisible.')) for i in range(1,len(photos)+1)]


def extract(item: Fingerprint, destination: Path, gate):
    """Déplacement atomique local vers un seul dossier ; aucun unlink/delete."""
    destination = Path(destination).absolute()
    if item.path.is_relative_to(destination):
        raise ValueError('Le fichier est déjà dans le dossier À jeter.')
    if not item.path.is_relative_to(item.root):
        raise ValueError('Le fichier est hors de la source.')
    _verify(item,gate)
    info = checked_stat(destination,directory=True)
    root_info = checked_stat(item.root,directory=True)
    if info.st_dev != checked_stat(item.path).st_dev or not volume_at(destination,info).local:
        raise OSError('Choisir À jeter sur le même disque que la source. Aucun déplacement inter-disque automatique.')
    journal = destination/JOURNAL
    journal.mkdir(mode=0o700,exist_ok=True)
    checked_stat(journal,directory=True)
    token = uuid4().hex
    # Un nom unique évite toute collision, tout en conservant nom et extension lisibles.
    stem=item.path.stem.encode('utf-8')[:120].decode('utf-8',errors='ignore')
    target = destination/(stem+'--'+token+item.path.suffix)
    manifest = journal/(token+'.json')
    record = {'version':1,'source_root':str(item.root),'relative':str(item.path.relative_to(item.root)),
              'root_identity':[root_info.st_dev,root_info.st_ino],
              'target':target.name,'sha256':item.sha256,'size':item.size}
    with manifest.open('x',encoding='utf-8') as stream:
        json.dump(record,stream,ensure_ascii=False,indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    with directory_handle(item.path.parent) as srcfd, directory_handle(destination) as dstfd:
        with directory_handle(journal) as fd:
            os.fsync(fd)
        os.fsync(dstfd)
        gate.check()
        _verify(item,gate)
        _rename(item.path.name,target.name,srcfd,dstfd)
        try:
            os.fsync(dstfd)
            os.fsync(srcfd)
        except OSError as error:
            raise OSError(f'Déplacement effectué, synchronisation à vérifier. Journal : {manifest}') from error
    return manifest


def restore_extraction(manifest,gate):
    manifest = Path(manifest).absolute()
    checked_stat(manifest)
    checked_stat(manifest.parent,directory=True)
    if manifest.parent.name != JOURNAL:
        raise ValueError('Ce fichier ne fait pas partie du journal de tri.')
    record = json.loads(manifest.read_text(encoding='utf-8'))
    relative = Path(record['relative'])
    name = Path(record['target'])
    root = Path(record['source_root'])
    if (record.get('version') != 1 or not root.is_absolute() or relative.is_absolute()
            or '..' in relative.parts or not relative.parts or name.name != str(name)):
        raise ValueError('Journal de restauration invalide.')
    info = checked_stat(root,directory=True)
    if [info.st_dev,info.st_ino] != record['root_identity']:
        raise OSError('L’identité de la source a changé. Restauration automatique refusée.')
    payload = manifest.parent.parent/name
    current = fingerprint(payload,manifest.parent.parent,gate)
    if (current.sha256,current.size) != (record['sha256'],record['size']):
        raise OSError('Le fichier extrait a changé depuis le tri.')
    target = root/relative
    with directory_handle(payload.parent) as srcfd, directory_handle(target.parent) as dstfd:
        gate.check()
        if file_evidence(payload) != current.evidence:
            raise OSError('Le fichier a changé pendant la restauration.')
        _rename(payload.name,target.name,srcfd,dstfd)
        os.fsync(dstfd)
        os.fsync(srcfd)
    return target
