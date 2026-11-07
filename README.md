# CineVault API

API Flask du tiers données de CineVault. Deux modes explicites : le catalogue pédagogique legacy, sans authentification par défaut, et Enterprise opt-in avec comptes, catalogue partagé, exemplaires, collections privées et prêts. Le stockage principal reste JSON. TLS n'est pas fourni par l'application : ne pas exposer directement son port à un réseau non fiable. Le frontend y accède sur le réseau privé de l'application.

## Mode Enterprise

Définir `ENTERPRISE_ENABLED=true`, `ENTERPRISE_DATA_FILE` vers un document v2 déjà initialisé ou migré, et `AUTH_THROTTLE_KEY_FILE` vers un fichier privé d'au moins 32 octets. Aucun fichier manquant n'est recréé silencieusement. Le premier administrateur se crée par l'outil local interactif `python -m app.bootstrap --store <chemin>` ; aucun compte ou secret prédéfini. Les mutations des routes legacy sont refusées dans ce mode ; les lectures legacy conservent leur fichier pédagogique distinct.

- [Comptes et sessions](docs/auth-contract.md), [protections et limites](docs/accounts-milestone.md).
- [Catalogue et exemplaires versionnés](docs/catalogue-v1-contract.md).
- [Collections privées et audit minimal](docs/collections-contract.md).
- [Prêts et réservations](docs/lending-contract.md), [file et expiration](docs/reservations.md).
- [Protection des créations contre les doubles envois](docs/idempotency.md).
- [Import/export JSON et CSV du catalogue](docs/import-export-contract.md).
- [Statistiques et recommandations explicables](docs/insights-contract.md).
- [Sauvegarde et reprise contrôlée](docs/recovery.md).
- [Métadonnées Wikidata/Commons, cache et affiches](docs/metadata-contract.md).
- [Spécification OpenAPI](docs/openapi-v1.json), schéma de stockage sous `schemas/`.

Les décisions d'autorisation, les versions et les règles d'intégrité sont vérifiées sous le même verrou de transaction. Les appels externes sont effectués entre deux contrôles d'autorisation, hors verrou métier. Une seule réplique API détient ce fichier. Le projet reste en développement : contrôles navigateur complets, infrastructure distribuée et audit final restent à terminer.

## Lancement et tests

Python 3.13 : créer un environnement virtuel, puis `python -m pip install -r requirements-dev.txt` et `python -m pytest -q`. Pour un lancement local, créer un répertoire de données accessible en écriture, définir `DATA_FILE` au chemin complet d'un fichier dans ce répertoire puis `flask --app wsgi run --port 8000`. Sous Linux en déploiement : `gunicorn --bind 0.0.0.0:8000 --workers 2 wsgi:app`. L'image Docker écoute sur 8000 et vérifie `/health`.

`DATA_FILE` vaut `/data/films.json` par défaut. Monter un volume persistant sur `/data`, avec droit d'écriture pour UID/GID 10001. Un volume nommé neuf reçoit les droits initiaux du répertoire de l'image ; un bind mount ou un volume préexistant exige de vérifier les droits. L'absence initiale du fichier crée `{"films": []}` ; un fichier présent invalide produit une erreur 503 et n'est jamais remplacé automatiquement. Le volume de données doit être sauvegardé et restauré. Les créations utilisent des UUID et les écritures un verrou interprocessus, une écriture temporaire, `fsync` et un remplacement atomique. Une unique réplique API et un volume local sur le nœud désigné sont requis ; cette configuration ne fournit ni haute disponibilité ni réplication.

La sonde vérifie que le document est lisible et valide et que l'initialisation peut être effectuée ; elle ne prouve pas que le système peut continuer à écrire après une panne. En cas d'erreur après remplacement mais avant synchronisation du répertoire, une requête peut signaler l'échec bien que la modification soit déjà visible : ne pas réessayer automatiquement les mutations, vérifier d'abord l'état.

## Contrat pédagogique legacy

| Méthode | Chemin | Résultat |
|---|---|---|
| GET | `/health` | 200 `{"status":"ok"}` ou 503 |
| GET | `/api/films?q=...&support=...` | liste `{"films":[...]}`, recherche insensible à la casse sur titre/réalisateur |
| GET | `/api/films/<id>` | film ou 404 |
| POST | `/api/films` | 201, film et en-tête Location |
| PUT | `/api/films/<id>` | remplacement complet, 200 ou 404 |
| DELETE | `/api/films/<id>` | 204 sans corps ou 404 |

Corps d'entrée JSON : `titre` (1–200), `annee` (entier 1888–2100), `realisateur` (1–120), `acteurs` (tableau, 0–30 noms de 1–120 caractères), `support` (`numerique`, `dvd`, `blu-ray`, `vhs`, `autre`), `synopsis` facultatif (0–4000 caractères). L'API génère `id`. Taille maximale 64 Kio ; propriétés inconnues et clés dupliquées rejetées. Exemple :

```json
{"titre":"Exemple","annee":2001,"realisateur":"A. Martin","acteurs":["B. Durand"],"support":"dvd","synopsis":"Fiction."}
```

Erreurs au format `{"error":{"code":"validation_error","message":"Données invalides.","fields":{"titre":"..."}}}` ; `fields` est facultatif. HTTP 400 JSON malformé, 415 type incompatible, 422 validation, 413 taille excessive, 404 absent, 405 méthode interdite, 503 stockage indisponible, 500 erreur interne. Les noms de champ ou messages ne contiennent aucun secret.

## Stockage v2 et migration

`app/storage_v2.py` et `app/migrate_v2.py` fournissent le document transactionnel versionné et sa migration. Les routes Enterprise sont raccordées à ce document ; le mode legacy conserve son fichier distinct et ses mutations ne sont permises que lorsque Enterprise est désactivé. Le schéma décrit comptes, sessions, compteurs anti-abus, films, exemplaires, collections, prêts, réservations, audit minimal et reçus de création d'exemplaire ou d'import. Les autres opérations mémorisées restent refusées jusqu'à leur implémentation. La validation Python contrôle aussi UTF-8, unicité, références, transitions cohérentes et dates, propriétés que le schéma seul ne garantit pas. Les exemples par type restent à compléter avant l'audit académique final.

`V2Store(path).initialize()` crée explicitement un document vierge sans écraser une cible. `read()` refuse un fichier manquant ou invalide. `transact(callback)` verrouille, lit, appelle le traitement, valide l'ensemble, incrémente la révision et remplace atomiquement le document ; une exception avant remplacement laisse l'ancien contenu. Le callback ne doit pas faire d'I/O externe ni retenir de référence mutable ; toute règle métier future et autorisation devra être vérifiée dans cette même transaction. Le stockage est à instance API unique sur un seul volume local ; aucune réplication ou haute disponibilité.

Pour sauvegarder : `digest = store.snapshot(destination_absente)` prend sous verrou une copie validée sans écraser la cible ; `V2Store(cible_absente).restore(snapshot, digest)` vérifie SHA-256 et schéma avant restauration sans écraser. La valeur de contrôle doit être conservée séparément dans un endroit fiable. Le stockage, les sauvegardes et les répertoires doivent être protégés par des permissions adaptées ; SHA-256 seul n'assure pas leur authenticité face à un acteur malveillant.

Une sauvegarde Enterprise contient des hachages de mots de passe et des empreintes de sessions : elle est confidentielle. La primitive restaure les octets validés, y compris l'ancien état de comptes/sessions. La commande opérationnelle `python -m app.recovery restore` crée au contraire un nouveau candidat sans écrasement, révoque les sessions, met les anciens comptes en quarantaine avec mots de passe invalidés et crée un administrateur de reprise distinct. L'outil local `reset-password` permet ensuite le retour individuel d'un compte vérifié. Voir la [procédure et ses limites](docs/recovery.md), notamment arrêt des écrivains, renouvellement des clés, contrôles et rapprochement des changements perdus depuis la sauvegarde.

Migration réservée à un opérateur après arrêt complet des anciennes écritures et sauvegarde vérifiée : `report = create_candidate(source_legacy, candidat_absent)` crée à côté un candidat v2, conserve l'identifiant de chaque film, crée un exemplaire au support d'origine avec identifiant déterministe et n'altère pas la source. Inspecter/valider les nombres et les empreintes avant `promote_candidate(candidat, cible_absente, source=source_legacy, expected_source_sha256=report['source_sha256'], expected_candidate_sha256=report['candidate_sha256'])`. La promotion prend les verrous source puis cible, vérifie les deux empreintes et ne remplace jamais une cible existante. L'ancien service doit rester arrêté pendant et après le basculement : le verrou ne l'empêche pas de réécrire le legacy après la promotion. Ne jamais lancer la migration sur un catalogue réel sans sauvegarde et plan de retour testés.

Les créations sans écrasement exigent la prise en charge des liens physiques sur le système de fichiers de destination ; toute erreur est remontée, sans repli qui écraserait une cible. Une erreur après remplacement/lien réussi mais avant synchronisation du répertoire rend le résultat de l'opération ambigu : inspecter le fichier et son empreinte avant toute nouvelle tentative. Après une panne brutale, les fichiers temporaires laissés par le système nécessitent une inspection manuelle avant suppression ; aucune suppression automatique de données préexistantes.

## Qualité et CI

Voir les [commandes, contrôles et limites](docs/quality.md). Les configurations de CI ne constituent pas une preuve d’exécution distante ; aucun badge de succès n’est ajouté avant un résultat GitHub réel.

Le [socle d’exécution spécialisé et ses limites de reconstruction](docs/runtime.md) décrit les deux étapes de construction et les composants conservés.
