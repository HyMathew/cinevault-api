# Exemplaires et reçus de création communs JSON/MongoDB

`app/services/copies.py` exécute création, remplacement et suppression via les
ports de persistance, avec la politique de session partagée de C1. Les trois
routes HTTP utilisent actuellement le backend JSON du même fichier existant.
Cette extraction ne prétend pas activer un backend HTTP Mongo incomplet.

## Autorité, versions et historique

La session et le rôle sont relus dans l’UoW finale. Seuls manager/admin peuvent
muter un exemplaire. La préparation HTTP contrôle aussi l’autorisation avant
parsing ; pour un remplacement, existence et If-Match sont vérifiés avant le corps,
puis contrôlés à nouveau au commit. Les identifiants de route incorrects restent
404 après autorisation ; ils ne deviennent pas des sélecteurs Mongo arbitraires.

Le remplacement et la suppression exigent l’ETag courant : 428 s’il manque,
412 s’il est périmé. Un historique de prêt, même retourné, bloque la suppression
avec 409. Aucun historique ni référence n’est purgé pour libérer un exemplaire.
Le rendu Flask, ETag et Location sont produits après la transaction métier.

## Rejeu de création

Idempotency-Key reste facultatif pour copy.create, avec 16 à128 caractères
ASCII alphanumériques, tiret ou soulignement. Les reçus conservent le digest de la
clé et du corps, l’acteur, l’opération et la réponse autorisée, pendant86400s.
Les UUID de l’exemplaire et du reçu sont préparés une fois hors callback.

- Même acteur, clé et données : réponse201 d’origine, même ETag/Location et
  `Idempotency-Replayed: true`, sans seconde création.
- Autres données ou opération avec cette clé : 409operation_conflict.
- Recul de l’horloge avant created_at : 409clock_state ; le reçu futur est conservé.
- Le reçu peut rejouer l’opération d’origine après suppression de sa ressource.
  Il prouve l’issue de la création, sans garantir que l’exemplaire existe encore.

La capacité existante de10000 reçus vivants est conservée comme budget de cache.
Un nouveau reçu est refusé avec503 quand ce budget est atteint ; un rejeu connu
et une création sans clé restent possibles. Ce budget ne constitue pas une limite
physique générale de Mongo ni la résolution des problèmes de capacité historique.
Les politiques d’admission équitable restent à compléter dans leur lot propre.

## Adaptateurs et annulation

Le port receipts expose get(actor,key_digest), count, prune_expired et insert.
JSON utilise le vrai fichier atomique et son verrou. Mongo utilise les sélecteurs
fixes, la session transactionnelle et le coordinateur commun. Le registre des UUID
et la suppression des reçus expirés sont modifiés dans la même transaction, par
lots de500 identifiants. La validation finale vérifie le reçu et son acteur ; une
réponse historique ne dépend pas d’une copie encore vivante.

La politique pure de clé/conflit/TTL est aussi utilisée par le helper d’import
JSON transitoire. L’import reste à extraire ; aucun callback JSON ancien n’est
présenté comme une implémentation Mongo prête.

## Preuves du 11 novembre2026

Suite locale : **316 tests réussis, 5 Mongo ignorés faute d’URI, 11 sous-tests**,
69.06s. Les tests supplémentaires vérifient l’ordre HTTP, l’absence de mutation
legacy pour les copies, la saturation, l’annulation, l’historique et la stabilité
des deux UUID après abort et réexécution injectés.

Le contrat natif a réellement passé de17:50:54 à17:51:56UTC sur Mongo8.0.34 et
PyMongo4.19.0, avec un compte CRUD limité distinct du client de maintenance :
CRUD/rejeu/conflit, réponse après suppression, ETag, historique, **501 reçus purgés
avec leur registre** et **deux insertions invalides annulées sans révision**.
Le même essai rejoue C1/C2a et provoque un vrai WriteConflict112 sur la course
admin. Les deux conteneurs et les deux volumes de l’essai ont été supprimés.

Manifeste des50 sources exactes testées :
`de2e0a2162412f103855c447efcb30147f4d477ba81478914a5107ba7ddccba2`.
Le gate CI Mongo appelle désormais exercise_copy_service sur le vrai serveur,
en plus des contrats d’authentification et de film. La migration, les listes
publiques ciblées et les workflows Mongo complets restent à réaliser.
