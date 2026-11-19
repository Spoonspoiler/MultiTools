# Atelier documentaire — première version de développement

## Audit avant modification

- Interface Tkinter dans `multitools/ui.py` : onglets existants conservés.
- Moteur visuel : Pillow et NumPy ; client Ollama partagé dans `ollama.py`, modèle local, vérification de capacité vision, sérialisation, schéma JSON et annulation. L'encodage existant réduit l'image à 640 px et la génération est bornée à 512 tokens : ce ne sont pas des conditions de lecture exhaustive d'un cadastre.
- Traitements : conversions de formats dans `conversions/image.py` ; extraction locale d'encre et nettoyage des composantes dans `conversions/contours.py`. L'extraction d'encre conserve son rôle dans le convertisseur. Le filtre de contours documentaire est un autre traitement, léger, sur prévisualisation.
- Stockage : SQLite pour l'inventaire/journal PhotoArchiviste, cache IA/corrections dans `photoarchiviste/ai_review.py`, index texte FTS dans `documents.py`.
- Aperçus existants : `photoarchiviste/preview.py`, zoom, déplacement, pleine résolution recadrée, chargement asynchrone ; modèles Photo liés à l'inventaire. Aucun éditeur de géométrie trouvé. Les conventions Canvas/Pillow sont reprises ; le nouveau canevas utilise des coordonnées natives indépendantes du zoom.
- Reprise existante : inventaire interrompu et journaux de déplacement PhotoArchiviste. Ils ne sont pas modifiés. Le nouvel onglet enregistre automatiquement chaque édition et recharge les documents depuis SQLite.

Plan suivi : modèle indépendant de Tk et tables documentaires, édition/synchronisation, outils sur prévisualisation, connexion Ollama, corrections/exemples, tests. Pas de nouvelle dépendance ni réécriture globale.

## Architecture et données

`documentary/model.py` : fiche, objets à identifiant stable, historique, corrections, annulation/rétablissement en mémoire, validation des résultats IA et Store. Table `visual_documents` ajoutée à la base existante `server_state/documents.sqlite3` ; les tables FTS sont conservées. Chaque document est un agrégat JSON transactionnel unique : liste et carte utilisent les mêmes objets.

`documentary/imaging.py` : prévisualisation bornée à 1800 px, opérations autorisées et superposition. `documentary/ui.py` : onglet principal, canevas, fiche défilante, objets et comparaison. Réutilisation directe de Services.documents.database et Services.ollama. Instructions et catégories cadastrales regroupées dans SKILL ; interface de chargement d'autres compétences reportée.

Les originaux sont référencés par chemin absolu et SHA-256, jamais réécrits. Une image modifiée est refusée à la reprise. Les corrections incluent identifiant documentaire, cible, état avant/après (géométrie incluse), catégorie, date UTC et statut ; contributeur prévu mais non renseigné. Les propriétés chronologiques, relations et territoire sont persistées ; pas encore d'interface temporelle.

## Utilisation réellement implémentée

Onglet **Analyse documentaire** : Ouvrir charge une image ; Reprendre liste les documents enregistrés ; Enregistrer force une sauvegarde. Déplacement : outil `deplacement`, glisser. Zoom : molette, centré sur le pointeur.

Choisir `line`, `polyline` ou `polygon`, cliquer les sommets puis Terminer tracé (ligne terminée après deux points). En sélection, cliquer un tracé, glisser l'objet ou ses poignées. La liste sélectionne le même objet. Le formulaire permet numéro, section, localité, commentaire, inscription et épaisseur. Appliquer actualise la carte et enregistre une correction ; Valider comme exemple ajoute un exemple récupérable. Ajouter/retirer un sommet sont des opérations simples (milieu du dernier segment / dernier sommet). Les calques peuvent être masqués. Annuler/rétablir couvre l'état documentaire de la session ; les piles ne sont pas restaurées au redémarrage, contrairement aux objets et à l'historique.

La fiche comporte les 19 champs historiques demandés, modifiables. Les champs IA ont provenance visible/hypothèse, justification et source Qwen ; modifications et validations humaines sont distinguées. Le sélecteur de champ donne accès à la justification, la validation et la liaison à une région dessinée ; Afficher la région recentre la carte.

Transformations non destructives enregistrées : luminosité, contraste, saturation, gris, inversion, seuil, extraction du rouge, contours Pillow, médiane, accentuation, dilatation/érosion/ouverture/fermeture, niveaux automatiques. Original/résultat permet la comparaison. Morphologie sur intensités, pas de segmentation sémantique.

Comparer : seconde carte, opacité, translation en pixels de prévisualisation, rotation et échelle de la seconde image ; paramètres persistants. Après reprise, Appliquer recharge la seconde carte. Masquer revient à la première carte transformée.

Identifier avec Qwen : tâche en arrière-plan, annulation, validation stricte du JSON. Les champs humains ne sont pas remplacés ; une correction effectuée pendant l'analyse est préservée. Les polygones ignorer sont blanchis dans l'image d'analyse, sans devenir des exemples négatifs. Les derniers exemples validés sont joints au contexte. Pas de réentraînement du modèle.

## Fonctions partielles et reportées

- Pas encore de propositions de géométries/OCR localisées par Qwen ni de boucle IA demandant/examinant des outils. Le validateur des outils existe mais seule l'interface humaine l'utilise. La génération courte peut produire un JSON incomplet ; l'erreur est affichée, aucun faux résultat n'est créé.
- Métadonnées : régions liées manuellement, pas de région IA ; un clic sur le champ ne recentre pas automatiquement. Pas de recherche sémantique d'exemples : récupération des derniers validés, filtre de catégorie disponible dans Store.
- Calques : couleurs persistées dans le modèle mais pas de sélecteur de couleurs ni de renommage des conventions dans l'interface. Pas de filtre de liste par catégorie, ni de masquage des numéros.
- Pas d'exemples négatifs explicitement validés dans l'interface ; une zone ignorée reste une exclusion.
- Rotation/recadrage de l'image principale et perspective reportés pour éviter une incohérence entre coordonnées et géométries. RGB/HSV et extraction d'autres couleurs reportés. Rotation disponible pour la seconde image de comparaison uniquement.
- Pas de curseur avant/après, alignement automatique, SIG, fusion/division ni chronologie graphique.
- Le canvas rend une portion visible d'une prévisualisation ; le zoom n'ajoute pas les détails natifs. Décompression initiale d'un très grand TIFF peut encore demander beaucoup de mémoire malgré la miniature. Traitements de prévisualisation synchrones courts ; pas de cache multiétape ni annulation des filtres. La table documentaire réécrit l'agrégat à chaque édition : à faire évoluer pour de très gros projets.
- L'édition graphique n'est pas encore validée sur l'écran réel. Cette branche doit être essayée sur des images de test avant intégration.

## Validation et suites

Tests ciblés : original inchangé, fiche/objets rechargés, identifiant/numéro, géométrie, suppression/annulation/rétablissement, filtrage des exemples validés, miniature bornée, extraction rouge/contours, refus des commandes invalides, contrat de réponse IA et superposition.

Suite existante et nouvelle : 114 tests (résultat final rapporté dans la PR). Trois tests graphiques existants nécessitent un écran. Essai Tk de construction du nouvel onglet impossible : `couldn't connect to display :0`. Pas d'inférence réelle Qwen vérifiée. Les erreurs de tâche sont journalisées via logging et affichées dans l'onglet.

Priorités suivantes : validation graphique avec cartes réelles, tests d'interactions Canvas et d'Ollama simulé, aperçus natifs en tuiles, métadonnées par région avec OCR, puis propositions de géométries et protocole d'outils Qwen borné. Ces développements supplémentaires ne sont pas lancés ici.
