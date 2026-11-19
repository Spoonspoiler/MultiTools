# Images originales et versions traitées

Les conversions d'images de Multitools enregistrent désormais un fichier compagnon
`nom-image.ext.multitools.json`. Il contient la source, les empreintes SHA-256,
le traitement et la date. Les originaux ne sont pas modifiés. Conserver ce fichier
avec le dérivé : un déplacement ou renommage externe sans ce compagnon perd le lien.
La source doit encore se trouver à son emplacement enregistré pour être comparée.

Dans PhotoArchiviste, **Lier à la source…** permet de rattacher un ancien dérivé à
son image source, sans déduire cette relation d'un nom ou d'une ressemblance.
Une relation existante n'est pas écrasée. Relancer l'analyse IA après le rattachement :
le cache tient compte de la nouvelle provenance.

Quand la source est disponible et conforme à son empreinte, Qwen reçoit une vue
étiquetée source/version traitée, ainsi que la liste des traitements. Une description
antérieure de la source (corrigée si disponible dans ce projet) est fournie comme
contexte à vérifier. Les relations successives sont suivies jusqu'à huit étapes.
Une source absente ou modifiée est signalée ; elle n'est pas utilisée comme preuve
visuelle. Une image sans relation connue reste de provenance inconnue, elle n'est
pas automatiquement déclarée originale. Ces liens ne créent pas de doublons exacts
et ne déclenchent aucune suppression ni décision de classement.

Le client Ollama compose désormais les pixels transparents sur du blanc avant JPEG,
ce qui évite d'envoyer des contours noirs sur un fond noir. Le mode documentaire
transmet également la liste de ses transformations et la provenance externe connue.
Dans ce mode, l'analyse reste celle de la vue courante avec ses exclusions ; le
montage source/dérivé est utilisé dans PhotoArchiviste.

Limites : aucune reconnaissance automatique des anciens traitements effectués hors
Multitools, pas de transfert automatique des géométries entre fichiers, pas de
réentraînement. Le modèle visuel reste limité par la réduction de la vue à 640 px :
le lien documentaire devient explicite mais ne garantit pas la lecture des inscriptions.
La comparaison réelle avec Qwen et le bouton sont à vérifier sur le poste utilisateur.
Les tests hors ligne couvrent provenance, intégrité, cache, montage et transparence.
