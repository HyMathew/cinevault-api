# Collections personnelles

Une entrée appartient à son créateur ; le client ne fournit jamais `owner_id`. Même un administrateur ne lit ni ne modifie les collections d'un autre compte par ces routes. Une entrée étrangère reçoit 404. Rôles membre, gestionnaire et administrateur possèdent chacun leur espace privé.

`GET /api/v1/collections` liste uniquement les entrées du compte courant, avec filtres facultatifs `kind` et `list_name`, pagination `limit` 1–100 (24 par défaut) et `offset` 0–10000. Réponse `{entries,movies,total,limit,offset}` ; `movies` associe les IDs référencés dans la page aux métadonnées publiques des films, sans requête supplémentaire par entrée. Chaque entrée possède id, owner_id, movie_id, kind, list_name, supports, created_at et `_etag` dérivé de ses attributs persistés.

`POST /api/v1/collections` accepte `movie_id`, `kind`, facultativement `list_name` et `supports`. Types : `owned` (possédés), `favorite`, `watched`, `watchlist`, `custom`. Le nom de liste est requis pour `custom` (1–80 caractères) et vide pour les autres types. Les supports personnels ne sont autorisés que pour `owned`, sans doublon. Une même combinaison propriétaire/film/type/nom de liste normalisé ne peut exister deux fois : 409 sans ajout partiel. Film absent : 404.

`GET /api/v1/collections/{id}` consulte une entrée propre. `PUT` remplace `list_name` et `supports` sans changer film, propriétaire ou type. `DELETE` retire cette entrée. Mutations If-Match requis : 428 si absent, 412 si périmé. Les références privées empêchent la suppression du film concerné (409) ; aucune suppression silencieuse de collection n'est autorisée.

Les mutations réussies ajoutent un événement d'audit minimal : UUID, actor_id, action, entity_type, entity_id, timestamp. Aucun titre, nom de liste, mot de passe, token, contenu de formulaire ou adresse IP. L'historique circulaire conserve les 2000 événements les plus récents ; cette limite est explicite et ne constitue pas un archivage illimité. Les administrateurs peuvent lire les événements avec `GET /api/v1/audit-events`, pagination bornée. L'audit des tentatives refusées et l'archivage externe restent distincts de ce jalon.

Les dates persistées utilisent des secondes UTC Unix entières, comme le module de sessions existant ; le rendu utilisateur peut les convertir en date locale. Cette décision précise l'ancienne proposition ISO-8601 dans le document de conception.
