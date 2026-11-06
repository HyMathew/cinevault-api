# Protection des créations d'exemplaires

`POST /api/v1/copies` accepte `Idempotency-Key`, 16–128 caractères ASCII parmi lettres, chiffres, tiret et soulignement. Le formulaire web génère et conserve sa clé aléatoire propre à l'envoi. Les clients API qui l'omettent conservent leur comportement historique et ne bénéficient pas de la déduplication.

Sous le même verrou que la création : identité et rôle courants vérifiés, recherche du reçu par acteur et empreinte de clé, comparaison de l'empreinte JSON canonique. Une répétition identique restitue 201, le même identifiant, ETag et Location, avec `Idempotency-Replayed: true`. Un contenu différent reçoit 409 sans création. Deux processus concurrents obtiennent un seul exemplaire et le même reçu. L'opération initiale et le reçu sont validés puis écrits ensemble.

Conservation : **24 heures**, 10000 reçus maximum, purge des reçus expirés lors des opérations concernées. Après expiration, une clé réutilisée constitue une nouvelle opération et peut créer un autre exemplaire. Ce n'est pas une garantie illimitée. Un reçu décrit le résultat initial : si la ressource a ensuite été retirée, le rejeu restitue ce résultat et son URL peut répondre 404 ; il ne recrée pas la ressource.

La réponse mémorisée est limitée par un validateur strict aux attributs publics d'un exemplaire et à sa version. Aucun bearer, cookie, mot de passe ou corps d'authentification ; la clé elle-même n'est pas stockée, uniquement SHA-256. Les références du reçu sont historiques et n'empêchent pas une suppression du catalogue. Les droits sont revérifiés avant restitution, même pour un acteur dont le rôle a changé.

Le formulaire conserve ses valeurs et sa clé lorsqu'une indisponibilité laisse le résultat incertain ; aucun retry automatique. Une tentative modifiant les données avec la même clé exige de vérifier le résultat puis d'ouvrir un nouveau formulaire. Le service reconstruit Location depuis l'identifiant et le routage Flask courant ; aucune URL arbitraire n'est mémorisée.

L'import `POST /api/v1/catalogue/import` utilise le même mécanisme avec clé obligatoire, opération `catalog.import` et statut 200. Son reçu contient seulement un identifiant d'opération et quatre compteurs bornés ; le contenu du fichier n'est pas conservé dans le reçu. La clé est commune aux opérations d'un acteur : la réutiliser pour un autre type d'opération reçoit 409. Les autres créations ne disposent pas encore de ce mécanisme ; aucun reçu de connexion n'est autorisé.
