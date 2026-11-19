"""Un passage unique : inventaire durable puis assistant sur chaque photo."""
from .ai_review import analyse_index
from .inventory import TaskStopped


def index_with_assistant(archive,roots,client,model,preferences,gate,report):
    stats={'analysed':0,'reused':0,'ai_errors':0,'ai_paused':False}
    consecutive=0
    def on_photo(photo):
        nonlocal consecutive
        gate.check()
        report('indexed_photo',photo)
        if stats['ai_paused']:
            return
        result=analyse_index([photo],archive.root/'assistant-tri.sqlite3',client,model,preferences,gate)
        stats['analysed']+=result['processed']
        stats['reused']+=result['cached']
        stats['ai_errors']+=len(result['errors'])
        if result['cancelled']:
            raise TaskStopped
        if result['errors']:
            consecutive+=1
            for error in result['errors']: report('error',error)
        else:
            consecutive=0
        report('assistant_progress',f'Indexation + assistant : {stats["analysed"]} analyse(s), '
                                   f'{stats["reused"]} résultat(s) réutilisé(s)')
        if consecutive>=3:
            stats['ai_paused']=True
            report('error','Assistant interrompu après trois erreurs. L’inventaire continue ; les analyses pourront reprendre.')
    found,errors=archive.scan(roots,gate,report,on_photo=on_photo)
    return {**stats,'indexed':found,'index_errors':errors}
