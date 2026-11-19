# Multitools sur un serveur Ubuntu

## Installation initiale

Pour Ubuntu 24.04 avec Python 3.12, prépare les dépendances depuis un terminal. Les archives ZIM restent dans les dossiers que tu choisis. [Paquet kiwix-tools Ubuntu](https://packages.ubuntu.com/noble/kiwix-tools)

```bash
sudo apt install python3-tk python3-venv kiwix-tools util-linux
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
kiwix-serve --version
```

Exécute les commandes Python depuis le dossier `MultitoolsApp`. Ubuntu Desktop peut ouvrir Tkinter directement. Sur une installation serveur sans session graphique, lance Multitools depuis une session graphique de la machine ou une session distante qui fournit un affichage.

### Ollama

Installe Ollama lors de la préparation connectée en suivant la procédure officielle. Si l’installation a déjà activé son service systemd, utilise `sudo systemctl edit ollama` avec ces réglages, puis `sudo systemctl restart ollama`. [Installation Linux](https://docs.ollama.com/linux)

```ini
[Service]
Environment="OLLAMA_HOST=127.0.0.1:11434"
Environment="OLLAMA_NO_CLOUD=1"
Environment="OLLAMA_MAX_LOADED_MODELS=1"
Environment="OLLAMA_NUM_PARALLEL=1"
```

Pour un lancement manuel, utilise ces mêmes réglages dans un terminal. [Options locales d’Ollama](https://docs.ollama.com/faq)

```bash
OLLAMA_HOST=127.0.0.1:11434 OLLAMA_NO_CLOUD=1 \
OLLAMA_MAX_LOADED_MODELS=1 OLLAMA_NUM_PARALLEL=1 ollama serve
```

Dans un autre terminal, télécharge explicitement le modèle si tu veux l’utiliser. La liste des modèles et la génération restent locales par la suite. [Modèle Qwen](https://ollama.com/library/qwen3.5:2b-q4_K_M)

```bash
ollama pull qwen3.5:2b-q4_K_M
ollama list
curl --noproxy '*' http://127.0.0.1:11434/api/version
```

Lance ensuite `python __main__.py`. Dans **Assistant IA**, clique sur **Détecter Ollama** et choisis un modèle. La conversation s’affiche au fil des fragments reçus. Pour une image jointe, l’assistant envoie une miniature au service local si le modèle déclare la capacité visuelle. **Annuler** ferme la requête active. **Sources locales seules** fonctionne indépendamment d’Ollama.

Le contexte proposé est de 2 048 tokens, avec les choix 3 072 et 4 096. Pour 8 Go de RAM, teste d’abord le modèle 2B et le contexte de 2 048 tokens. Multitools sérialise les générations et demande une libération du modèle après deux minutes d’inactivité. Il affiche la durée et la mémoire signalée par Ollama quand l’API la fournit. Ces mesures sont aussi conservées dans le journal borné `server_state/ollama.jsonl`. Elles décrivent le modèle et sa génération. Les conversations restent en mémoire de la session.

### Kiwix et accès sur le Wi-Fi privé

1. Dans **Bibliothèque**, ajoute les répertoires ZIM ou des fichiers individuels. **Actualiser** relit l’inventaire. Les chemins sont conservés dans la configuration.
2. Utilise **Démarrer** avec `127.0.0.1` et le port `8080` pour une consultation sur le serveur. Pour le téléphone, remplace l’adresse par l’adresse IPv4 privée réelle de l’interface Wi-Fi du serveur. `ip -br -4 addr` affiche les adresses des interfaces sur Ubuntu.
3. Sur Android ou un autre ordinateur du même Wi-Fi, ouvre l’URL affichée dans l’onglet. Une isolation entre clients Wi-Fi ou un filtrage local peut empêcher l’accès. La configuration prévue utilise le réseau privé avec le port autorisé sur ce réseau seulement. Ollama reste sur la boucle locale.
4. Choisis une archive dans le sélecteur, cherche un terme, puis ouvre le résultat. La recherche requiert l’index plein texte du ZIM. Le navigateur permet également de parcourir une archive dont la recherche plein texte manque.

Multitools vérifie les options de la commande et le catalogue OPDS v2 lors du lancement. Les archives sont servies à leur emplacement, avec les liens externes bloqués par l’option Kiwix. Les commandes de l’interface gèrent le processus qu’elle a créé. Pour servir la bibliothèque indépendamment de la fenêtre, lance la commande dans un environnement qui reste actif, puis clique sur **Connecter** dans Multitools. Remplace l’IP et le chemin d’exemple. [Options et API Kiwix](https://kiwix-tools.readthedocs.io/en/latest/kiwix-serve.html)

```bash
kiwix-serve --address=192.168.1.20 --port=8080 --blockexternal --threads=2 /chemin/notice.zim
curl --noproxy '*' 'http://192.168.1.20:8080/catalog/v2/entries?count=1'
```

**Vider la sélection** retire les références du catalogue Multitools. Pour changer les fichiers effectivement servis par le processus lancé dans l’application, arrête-le puis relance-le après avoir modifié la sélection.

## Recherche et preuves

Dans **Assistant IA**, ajoute tes dossiers de notices puis clique sur **Indexer / reprendre**. L’index traite les textes UTF-8, Markdown, RST et HTML de 8 Mio au maximum. Il conserve ses étapes terminées dans SQLite et réutilise les entrées inchangées. Le bouton **Pause / reprise index** agit entre deux fichiers. Un document modifié depuis l’indexation doit être réindexé avant de servir de référence. [SQLite FTS5](https://www.sqlite.org/fts5.html)

**Sources locales seules** montre les passages et les ouvre à partir de leur vrai fichier ou de leur page Kiwix. **Sources locales + IA** envoie une sélection d’extraits et vérifie les identifiants cités dans la réponse. L’interface ne construit aucun lien à partir de texte généré. Une réponse fondée sur un extrait peut quand même mal interpréter son contenu. La liste associée permet d’ouvrir la source et de vérifier l’interprétation.

La recherche utilise l’archive Kiwix sélectionnée quand elle est connectée. Sans sélection, elle consulte jusqu’à deux archives servies. Les PDF et DOCX, la recherche par embeddings et l’indexation complète des ZIM sont des extensions possibles. Les modèles de ZIM actuels restent gérés par Kiwix.

## Photos et disques

Pour importer un téléphone, copie d’abord ses photos dans `A_IMPORTER`, puis choisis ce dossier dans **Sources…**. Une source Samba, Windows ou MTP peut être lue si son montage et ses fichiers sont identifiables. Le mode conservatoire limite tous les transferts à des copies vérifiées.

La comparaison SHA-256, pHash et EXIF reste locale et précède l’IA. La vue **Toutes** permet de préparer des copies d’un lot dans la catégorie choisie. **Examiner avec l’IA** prépare une demande visuelle facultative, que tu peux compléter puis envoyer. L’assistant sépare ses observations, ses indices et son incertitude. La catégorie et la destination affichées restent des propositions à reprendre dans le formulaire de classement.

Dans **Stockage**, détecte les points de montage et associe **SYSTEM**, **DATA** et **BACKUP**. Sur Ubuntu, les rôles employés pour écrire exigent l’UUID du volume attendu. Le moteur contrôle aussi le montage actif et les identités enregistrées pour le transfert. Après un remontage ou redémarrage Ubuntu, retire l’action bloquée du plan, vérifie le disque, réindexe la source et prépare une nouvelle copie. Garde le dossier de travail et la base SQLite sur un système de fichiers local, par exemple ext4. Accède aux sources Samba depuis le serveur et garde le dossier de travail sous le contrôle du compte qui exécute PhotoArchiviste. Le diagnostic utilise les parents rapportés par `lsblk` pour détecter des partitions sur un même disque.

Le contrat `plan_backup` prépare une destination indépendante pour Restic ou Kopia. L’exécution de sauvegardes et la restauration avec ces moteurs feront l’objet d’une étape séparée. L’attribution d’un rôle ne déplace aucune archive ni base. La configuration et l’index documentaire se trouvent dans `server_state/` à côté du lanceur, et les journaux PhotoArchiviste restent dans les dossiers de travail choisis.

## Essai sans Internet et réinstallation

Prépare, sur une machine compatible avec la cible, les paquets Ubuntu nécessaires avec leurs dépendances, la distribution Linux d’Ollama, les archives ZIM choisies et le dossier de modèles d’Ollama avec ses manifests et blobs. Garde aussi les roues Python adaptées à la même version de Python, au même système et à la même architecture.

```bash
# Pendant la préparation connectée
python -m pip download --only-binary=:all: -r requirements.txt -d wheelhouse

# Sur la cible hors ligne, dans un environnement Python déjà créé
python -m pip install --no-index --find-links=wheelhouse -r requirements.txt
```

Pour l’essai, garde le Wi-Fi local et coupe son accès Internet. Vérifie successivement une recherche Kiwix sur Ubuntu et Android, une recherche **Sources locales seules**, une question **Sources locales + IA** avec vérification des pages citées, puis une simulation PhotoArchiviste sur des copies de photos. Arrêter Ollama doit laisser la recherche de sources et tous les outils de fichiers utilisables. Pour tester une interruption de stockage, utilise un jeu d’essai et vérifie que le transfert est journalisé avant de le reprendre.

```bash
python -B -m unittest discover -s tests -v
```

Les services sont simulés dans les tests Python. Le serveur Ubuntu cible, la connexion Android, la mémoire en charge réelle, les gros ZIM et les disques physiques restent à valider sur ton matériel.

## Fichiers ajoutés ou modifiés

- `multitools/local_http.py`, `ollama.py` et `server_tasks.py` pour le transport privé, la génération et les workers.
- `multitools/kiwix.py`, `documents.py`, `storage.py` pour le catalogue, les extraits et les montages.
- `multitools/assistant_ui.py`, `library_ui.py`, `status_ui.py` et `ui.py` pour les onglets et leur intégration à la fenêtre.
- `multitools/photoarchiviste/inventory.py`, `journal.py`, `transfers.py` et `ui.py` pour la similarité visuelle, les propositions de lots, l’examen IA et le contrôle de la destination.
- `tests/test_local_services.py`, `tests/test_photoarchiviste.py`, `README.md` et ce guide pour la validation et le démarrage.
