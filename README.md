# Multitools

Atelier Tkinter hors ligne. PhotoArchiviste compare et organise des archives photographiques. La même fenêtre regroupe les convertisseurs, la bibliothèque Kiwix, l’assistant Ollama, le stockage et l’état du serveur. Le [guide du serveur Ubuntu](SERVEUR.md) décrit l’installation, le Wi-Fi privé et les essais hors ligne.

## Démarrage sur Ubuntu ou dans PyCharm

Copie le dossier `MultitoolsApp` sur ta clé USB. Il contient tout le code de l’application, ses dépendances déclarées dans `requirements.txt`, ses tests et les anciens scripts de conversion avec leurs ressources dans `archives/`. Le code utilisé par l’interface se trouve dans `multitools/`.

Ouvre `MultitoolsApp` comme projet dans PyCharm, choisis un interpréteur Python 3.12 avec Tkinter et lance le fichier `__main__.py`. Le même interpréteur sert à PhotoArchiviste et aux convertisseurs.

Sur Ubuntu, ouvre un terminal dans le dossier copié et crée son environnement Python local à partir du même `requirements.txt`.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python __main__.py
```

Si l’import `tkinter` manque, installe le paquet Ubuntu `python3-tk` associé à ton interpréteur. Le module `venv` est fourni par `python3-venv` sur Ubuntu. Une fois ses dépendances présentes, Multitools démarre et fonctionne hors ligne.

## Trier des photos

1. Dans **PhotoArchiviste**, sélectionne un **Dossier de travail** existant sur un volume local pris en charge. Il contiendra `PhotoArchiviste.sqlite3`, `TRIEES` et `A_JETER`.
2. Ajoute tes dossiers avec **Sources…**, puis clique sur **Indexer**. Les sources peuvent être sur des disques différents. Les dossiers `TRIEES` et `A_JETER` de la session sont exclus de l’inventaire.
3. **Proches** présente les séries repérées par pHash, forme et couleur, avec leurs copies exactes. Une série peut relier plusieurs photos par des ressemblances successives. **Exacts** isole les groupes ayant la même taille et le même SHA-256. Les anciennes entrées dHash sont lisibles et sont recalculées lors d’une nouvelle indexation. **Toutes** permet aussi de traiter les photos uniques. **Reportées** conserve les décisions remises à plus tard.
4. Parcours la série par pages de six miniatures. Clique sur une miniature pour l’agrandir à gauche. Pour garder plusieurs photos, coche **Garder** sous chacune, y compris en changeant de page. Choisis la catégorie de gauche et clique sur **Garder la sélection**. Avec les cases décochées, le bouton prend la photo agrandie à gauche.
5. Les boutons à deux photos permettent de choisir une catégorie par côté ou de préparer une copie dans `A_JETER`. L’option année utilise la date de prise de vue EXIF lorsqu’elle est disponible et valide.
6. Relis **Plan et historique**, puis clique sur **Appliquer le plan**. En mode conservatoire, toutes les actions exécutables sont des copies. Le déplacement et le retrait des originaux sont désactivés. Chaque sortie est relue pour vérifier son SHA-256. Un conflit de nom ajoute un numéro.
7. Dans **À jeter · copies** ou **Plan et historique**, sélectionne une copie vérifiée, puis **Restaurer par copie…**. Le dossier d’origine et son volume local doivent être vérifiables. Si le nom est occupé, la restauration crée un nom numéroté. La copie classée ou rejetée reste en place.

**Pause** et **Reprendre** agissent sur la tâche active. Pour reprendre après avoir fermé l’application, rouvre le même dossier de travail. Utilise **Reprendre l’indexation** ou **Appliquer le plan** selon la tâche interrompue. La reprise exige les mêmes preuves de fichier, de répertoire et de montage. Les anciens plans sans preuves restent visibles et bloqués. Pour refaire une copie, retire l’ancienne action du plan, réindexe la source et prépare une nouvelle action. Les temporaires d’un transfert interrompu sont conservés. **Exacts** décrit les empreintes de l’index. **Copie vérifiée** décrit une sortie au moment de son transfert. PhotoArchiviste ne propose aucune autorisation de purge.

## Convertir des fichiers

Dans **Images** ou **Vidéo et audio**, ajoute des fichiers, choisis le format et le dossier de sortie, puis clique sur **Convertir**. Double-clique sur un résultat pour afficher son chemin ou le détail d’une erreur. Les sorties existantes sont conservées et les conflits de noms sont numérotés.

- Images en PNG, JPEG, WebP, AVIF, TIFF, BMP, GIF et ICO. Les images animées sont converties à partir de la première image. Les sorties ICO incluent plusieurs tailles. L’onglet contient aussi les contours sur fond transparent et les silhouettes SVG monochromes.
- Vidéo en MP4 H.264/AAC, MOV ProRes 422 HQ avec audio PCM et WebM VP9/Opus. Les modes MOV et MKV par copie conservent les pistes vidéo et audio compatibles, sans réencodage.
- Extraction ou conversion de la première piste audio en MP3, WAV, FLAC, M4A (AAC), Ogg Vorbis ou Opus. Le FLAC conserve la fréquence d’échantillonnage de la source. Les autres profils audio dédiés produisent une sortie stéréo à 48 kHz.

## Tests

```text
python -B -m unittest discover -s tests -v
```

Les tests génèrent leurs propres images et journaux dans des dossiers temporaires sous `MultitoolsApp/tests`.

## Architecture

`inventory.py` indexe les photos et produit les preuves de correspondance. `review.py` transforme une décision en propositions. `journal.py` conserve la session, les décisions et les états SQLite. `transfers.py` exécute les transferts autorisés et reprend à partir du dernier état vérifiable. La fenêtre Tkinter reçoit les résultats par file de messages. La comparaison, la prévisualisation et les transferts s’exécutent dans des workers.

`ollama.py` partage une file d’attente IA entre les outils. `kiwix.py` inventorie les fichiers et délègue la lecture et la recherche ZIM à Kiwix. `documents.py` maintient un index SQLite FTS5 des dossiers documentaires choisis. Les onglets utilisent `server_tasks.py` pour envoyer les tâches aux workers et présenter les états dans Tkinter. `storage.py` vérifie les rôles de stockage configurés. `photoarchiviste/path_safety.py` contrôle les identités et les chemins utilisés par le journal et le moteur de copie.
