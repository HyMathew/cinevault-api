# Contrat catalogue Enterprise

Extension du jalon comptes. Lecture publique ; mutations réservées aux rôles `manager` et `admin`, vérifiés par bearer sous verrou. L'inscription ne permet jamais de choisir ces rôles.

- `GET /api/v1/movies` : `{movies,total,limit,offset}`. Paramètres bornés `q`, `genre`, `year`, `support`, `sort` (`title`, `-title`, `year`, `-year`), `limit` (1–100, défaut 24), `offset` (0–10000). Recherche titre/réalisateur/acteurs, filtres conjoints, tri déterministe avec ID pour départager.
- `GET /api/v1/movies/{id}` : objet film v2 et `_etag`, en-tête ETag correspondant à cette version. Les exemplaires sont une ressource distincte.
- `POST /api/v1/movies` : title, year, director, actors, synopsis ; genres facultatif (défaut []). `initial_copy` facultatif ({support,location,condition}) permet de créer un premier exemplaire dans la même transaction que le film. 201, Location, film et `_etag`. Doublon titre/réalisateur/année normalisés : 409, aucune modification.
- `PUT /api/v1/movies/{id}` : remplacement des métadonnées, sans initial_copy, If-Match requis. 200 ; ne modifie jamais un exemplaire implicitement.
- `DELETE /api/v1/movies/{id}` : If-Match requis ; 204 uniquement sans exemplaire ni référence métier. Les exemplaires doivent être traités explicitement avant la suppression du film.
- `GET /api/v1/copies` : liste publique `{copies}` filtrable par `movie_id`. Chaque copie expose `_etag`.
- `POST /api/v1/copies` : movie_id, support, location, condition ; 201. Référence film existante obligatoire.
- `GET`, `PUT`, `DELETE /api/v1/copies/{id}` : consultation, remplacement des attributs, suppression. Mutations If-Match ; la référence movie_id n'est pas modifiable.

La version `_etag` est calculée sur les attributs persistés de l'entité, et non sur la révision globale qui varie avec les sessions. If-Match absent : 428 ; version périmée : 412. Aucun changement métier n'est appliqué dans ces cas. Les listes ne modifient pas le fichier. Les attributs inconnus sont refusés ; les règles du schéma v2 restent applicables. Le catalogue legacy reste réservé à la pédagogie et ses mutations demeurent refusées en mode Enterprise.

À ce jalon, prêts, réservations et collections ne sont pas encore activés ; leurs intégrités devront être ajoutées avant de permettre leurs données non vides dans le validateur. Les nouveaux contrats devront conserver les protections de suppression et de concurrence.
