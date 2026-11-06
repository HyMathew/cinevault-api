# CineVault API

API Flask pour une vidéothèque JSON. Elle appartient au tiers de données ; elle ne fournit ni authentification ni TLS. Ne pas exposer directement son port à un réseau non fiable. Le frontend y accède sur le réseau privé de l'application.

## Lancement et tests

Python 3.13 : créer un environnement virtuel, puis `python -m pip install -r requirements-dev.txt` et `python -m pytest -q`. Pour un lancement local, créer un répertoire de données accessible en écriture, définir `DATA_FILE` au chemin complet d'un fichier dans ce répertoire puis `flask --app wsgi run --port 8000`. Sous Linux en déploiement : `gunicorn --bind 0.0.0.0:8000 --workers 2 wsgi:app`. L'image Docker écoute sur 8000 et vérifie `/health`.

`DATA_FILE` vaut `/data/films.json` par défaut. Monter un volume persistant sur `/data`, avec droit d'écriture pour UID/GID 10001. Un volume nommé neuf reçoit les droits initiaux du répertoire de l'image ; un bind mount ou un volume préexistant exige de vérifier les droits. L'absence initiale du fichier crée `{"films": []}` ; un fichier présent invalide produit une erreur 503 et n'est jamais remplacé automatiquement. Le volume de données doit être sauvegardé et restauré. Les créations utilisent des UUID et les écritures un verrou interprocessus, une écriture temporaire, `fsync` et un remplacement atomique. Une unique réplique API et un volume local sur le nœud désigné sont requis ; cette configuration ne fournit ni haute disponibilité ni réplication.

La sonde vérifie que le document est lisible et valide et que l'initialisation peut être effectuée ; elle ne prouve pas que le système peut continuer à écrire après une panne. En cas d'erreur après remplacement mais avant synchronisation du répertoire, une requête peut signaler l'échec bien que la modification soit déjà visible : ne pas réessayer automatiquement les mutations, vérifier d'abord l'état.

## Contrat

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
